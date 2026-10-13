"""Pipeline stages.

Each stage reads jobs in one status and moves them on, committing each job as it
goes, so any stage can be rerun alone and a stopped or crashed run resumes where it
left off::

    ingest   Gmail alert emails -> new jobs (deduped)
    track    Gmail replies about applications -> applied | interview | offer | hired | not_selected
    filter   new jobs failing the hard filters in preferences -> filtered (no requests spent)
    fetch    new jobs without a posting -> full posting text (or the email summary)
    triage   new jobs -> filtered | triaged (with scores), best-ranked first, capped per run
    recheck  paused postings (up, but not taking applications) -> back where they were | filtered
    generate best triaged jobs still open -> generated (tailored resume in out/)

Postings found not taking applications at any step are paused rather than dropped,
since some reopen; only a removed or expired posting counts as closed.
"""

import fcntl
import logging
import sqlite3
import subprocess
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import anthropic

from . import company, db, gmail, llm, sources, tracking, triage
from .facts import Facts
from .output import produce
from .paths import DATA
from .progress import STAGES, Progress, StopRequestedError, Tracker, install_stop_handler, recover

log = logging.getLogger("tailor")
LOCK = DATA / ".pipeline.lock"

# Per-job failures are isolated so one bad page, email or model call doesn't stop the
# run (and then every later run, since the job would stay queued). These are the
# failures a job can hit: network and files, parsing untrusted HTML/JSON/email,
# model calls on either backend, LaTeX, and the database.
JobError = (
    OSError,
    ValueError,
    TypeError,
    AttributeError,
    LookupError,
    RuntimeError,
    subprocess.SubprocessError,
    anthropic.APIError,
    sqlite3.Error,
)
REVIEWED = frozenset({"approved", "rejected", *tracking.RANK})
PENDING_DAYS = 30  # default for tracking.pending_days
GMAIL_STAGES = frozenset({"ingest", "track"})  # skipped by --skip-ingest; failures don't stop the run


class AlreadyRunningError(RuntimeError):
    pass


@contextmanager
def exclusive() -> Generator[None]:
    """Hold the pipeline lock, so the timer and the dashboard never run it twice at once."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            msg = "the pipeline is already running"
            raise AlreadyRunningError(msg) from e
        yield


def is_running() -> bool:
    if not LOCK.exists():
        return False
    with LOCK.open() as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def ingest(conn: sqlite3.Connection, prefs: triage.Prefs, facts: Facts, p: Progress | None = None) -> int:
    p = p or Progress()
    p.stage("ingest", None)
    added = 0
    on_total = cast("Callable[[int], None]", lambda n: p.stage("ingest", n))
    query = prefs["gmail"]["query"]
    # After the first ingest, only search since the newest email already read. A day of
    # overlap covers Gmail's date boundaries; emails already read are skipped below.
    if last := conn.execute("SELECT max(received_at) FROM emails").fetchone()[0]:
        query += f" after:{datetime.fromisoformat(last) - timedelta(days=1):%Y/%m/%d}"
    for msg in gmail.messages(query, facts.raw["contact"]["email"], on_total=on_total):
        p.item(msg.subject)
        if not db.email_seen(conn, msg.message_id):
            added += ingest_text(conn, msg)
        p.advance()
    return added


def ingest_text(conn: sqlite3.Connection, msg: gmail.Message) -> int:
    """Parse one email. Also used to ingest saved emails for testing."""
    parse = sources.parser_for(msg.sender)
    leads = parse(msg.text) if parse else []
    received = sources.iso_utc(datetime.fromisoformat(msg.received_at)) if msg.received_at else None
    leads = [{"posted_at": received, **{k: v for k, v in lead.items() if v is not None}} for lead in leads]
    added = sum(db.add_job(conn, **lead, email_id=msg.message_id) for lead in leads)
    db.mark_email(
        conn,
        msg.message_id,
        sender=msg.sender,
        subject=msg.subject,
        received_at=msg.received_at,
        jobs_found=len(leads),
    )
    log.info("%s: %d leads, %d new (%s)", msg.sender, len(leads), added, msg.subject[:60])
    return added


def track(conn: sqlite3.Connection, prefs: triage.Prefs, facts: Facts, p: Progress | None = None) -> int:
    """Read replies about tracked jobs from Gmail and move each job on. Returns status changes."""
    p = p or Progress()
    p.stage("track", None)
    jobs = tracking.candidates(conn)
    extra = str(prefs.get("tracking", {}).get("query") or "")
    moved = 0
    for query in tracking.queries(conn, jobs, extra):
        on_total = cast("Callable[[int], None]", lambda n: p.stage("track", n))
        for msg in gmail.messages(query, facts.raw["contact"]["email"], on_total=on_total):
            p.item(msg.subject)
            if not db.event_seen(conn, msg.message_id):
                moved += tracking.record(conn, msg, jobs)
            p.advance()
    return moved


def pause(conn: sqlite3.Connection, row: sqlite3.Row, **fields: object) -> None:
    """Park a job whose posting is up but not taking applications, to recheck later."""
    if row["status"] != "paused":
        fields |= {"paused_from": row["status"], "paused_at": db.now()}
    db.update(conn, row["id"], **fields, status="paused")
    log.info("%s (%s) isn't taking applications; paused", row["id"], row["status"])


def recheck(conn: sqlite3.Connection, prefs: triage.Prefs, p: Progress | None = None) -> int:
    """Revisit paused postings: reopened ones go back where they were. Returns how many reopened."""
    p = p or Progress()
    rows = db.jobs(conn, "paused")
    p.stage("recheck", len(rows))
    days = int(prefs.get("tracking", {}).get("pending_days", PENDING_DAYS))
    reopened = 0
    for row in rows:
        p.item(f"taking applications again? {_label(row)}")
        reopened += recheck_one(conn, row, days) == "open"
        p.advance()
    return reopened


def recheck_one(conn: sqlite3.Connection, row: sqlite3.Row, days: int = PENDING_DAYS) -> sources.PostingState | None:
    """Look at one paused posting again and act on what it says."""
    state = sources.posting_state(dict(row))
    if state == "open":
        db.update(conn, row["id"], status=row["paused_from"] or "new", paused_from=None, paused_at=None)
        log.info("recheck: %s is taking applications again", row["id"])
    elif state == "closed":
        db.update(conn, row["id"], **sources.CLOSED)
    elif state == "paused" and (row["paused_at"] or "") < (datetime.now(UTC) - timedelta(days=days)).isoformat():
        db.update(conn, row["id"], status="filtered", filter_reason=f"not taking applications for {days}+ days")
    return state


def add_url(conn: sqlite3.Connection, url: str) -> str:
    """Add a single posting by URL (LinkedIn job link or any careers page)."""
    if m := sources.LINKEDIN_JOB_ID.search(url):
        job_id = m.group(1)
        job = {"id": f"linkedin:{job_id}", "source": "linkedin", "url": f"https://www.linkedin.com/jobs/view/{job_id}"}
    else:
        job = {"id": f"manual:{sources.hash_id(url)}", "source": "manual", "url": url, "apply_url": url}
    db.add_job(conn, **job)
    return job["id"]


def prefilter(conn: sqlite3.Connection, prefs: triage.Prefs, p: Progress | None = None) -> int:
    """Apply the free hard filters before spending requests on fetching postings.

    triage_new filters again, since fetching can fill in a missing location.
    """
    p = p or Progress()
    rows = db.jobs(conn, "new")
    p.stage("filter", len(rows))
    n = 0
    for row in rows:
        if (reason := triage.hard_filter(dict(row), prefs)) is not None:
            db.update(conn, row["id"], status="filtered", filter_reason=reason)
            n += 1
        p.advance()
    return n


def _label(row: sqlite3.Row) -> str:
    return f"{row['company'] or '?'}: {row['title'] or row['id']}"


def fetch(conn: sqlite3.Connection, p: Progress | None = None) -> int:
    p = p or Progress()
    rows = [r for r in db.jobs(conn, "new") if not r["posting"]]
    p.stage("fetch", len(rows))
    done = 0
    for row in rows:
        p.item(_label(row))
        error = None
        try:
            fields = sources.fetch_posting(dict(row))
        except JobError as e:
            log.warning("fetch %s failed: %s", row["id"], e)
            fields, error = {"posting": row["summary"], "posting_ok": 0}, f"fetch: {e}"
        if fields.get("posting") in {None, ""}:
            # Closed posting or unreadable page, and no email summary to fall back on.
            # Without this the job would be refetched on every run.
            fields |= {"status": "filtered", "filter_reason": error or "no posting text available"}
        if fields.get("status") == sources.PAUSED:
            pause(conn, row, **{k: v for k, v in fields.items() if k != "status"}, error=error)
        else:
            db.update(conn, row["id"], **fields, error=error)
        done += 1
        p.advance()
    return done


def triage_new(conn: sqlite3.Connection, prefs: triage.Prefs, facts: Facts, p: Progress | None = None) -> int:
    p = p or Progress()
    survivors = []
    for row in db.jobs(conn, "new"):
        if (reason := triage.hard_filter(dict(row), prefs)) is not None:
            db.update(conn, row["id"], status="filtered", filter_reason=reason)
        elif row["posting"] or row["summary"]:
            survivors.append(dict(row))
    cap = prefs["triage"].get("max_per_run")
    ranked = triage.rank(survivors, facts)
    if cap is not None and len(ranked) > cap:
        log.info("triage: scoring the top %d of %d; the rest wait for the next run", cap, len(ranked))
        ranked = ranked[:cap]
    survivors = ranked
    p.stage("triage", len(survivors))
    scored = 0
    try:
        for scores, model in triage.score(survivors, facts, prefs, p):
            for job_id, s in scores.items():
                db.update(
                    conn,
                    job_id,
                    status="triaged",
                    relevance=s.relevance,
                    desirability=s.desirability,
                    triage={"reasons": s.reasons, "red_flags": s.red_flags, "model": model},
                    error=None,
                )
            scored += len(scores)  # jobs the model skipped stay 'new' for the next run
            p.advance(len(scores))
    except JobError:  # earlier batches are saved; the rest stay 'new' for the next run
        log.exception("triage stopped after %d jobs", scored)
    return scored


def queue(conn: sqlite3.Connection, prefs: triage.Prefs) -> list[sqlite3.Row]:
    """Triaged jobs that qualify for a resume, best first."""
    t, g = prefs["triage"], prefs["generate"]
    cur = conn.execute(
        """SELECT * FROM jobs WHERE status = 'triaged' AND relevance >= ? AND desirability >= ?
           AND (posting_ok = 1 OR ? = 0) AND attempts < ?""",
        (t["min_relevance"], t["min_desirability"], int(g.get("require_full_posting", True)), db.MAX_ATTEMPTS),
    )
    return sorted(cast("list[sqlite3.Row]", cur.fetchall()), key=triage.total_score, reverse=True)


def company_profile(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """The employer's public profile, looked up once per job and kept in the database."""
    if (cached := cast("str | None", row["company_info"])) is not None:
        return cached or None
    try:
        info = company.profile(dict(row))
    except JobError as e:  # a missing profile shouldn't cost the resume; try again next time
        log.warning("company profile for %s failed: %s", row["id"], e)
        return None
    db.update(conn, row["id"], company_info=info or "")
    return info


def generate_one(
    conn: sqlite3.Connection, job_id: str, facts: Facts, via: llm.Backend = "plan", effort: llm.Effort = "high"
) -> Path:
    row = db.get_required(conn, job_id)
    header = (
        f"**{row['title']}** at **{row['company'] or 'unknown company'}**, {row['location'] or ''}  \n"
        f"Posting: {row['url'] or row['apply_url']}  \n"
        f"Triage: relevance {row['relevance']}, desirability {row['desirability']}"
    )
    if not row["posting_ok"]:
        header += "  \n**Posting incomplete:** generated from the email summary only."
    posting = f"Title: {row['title']}\nCompany: {row['company']}\nLocation: {row['location']}\n\n{row['posting']}"
    profile = company_profile(conn, row)
    name = f"{row['company'] or row['source']} {row['title']}"
    # Regenerating a job you've already approved, applied to or rejected keeps that status.
    reviewed = row["status"] in REVIEWED
    try:
        outdir, rep, model = produce(posting, name, facts, company=profile, via=via, effort=effort, header=header)
    except JobError as e:
        failed = {} if reviewed else {"status": "error"}
        db.update(conn, job_id, **failed, error=f"generate: {e}", attempts=(row["attempts"] or 0) + 1)
        raise
    done = {} if reviewed else {"status": "generated"}
    db.update(conn, job_id, **done, outdir=str(outdir), checks_ok=int(rep.ok), model=model, error=None)
    return outdir


def close_awaiting(conn: sqlite3.Connection, p: Progress) -> int:
    """Set aside resumes you haven't applied with yet whose postings closed or paused since.

    New-grad postings often close within days, before the resume is reviewed.
    """
    rows = [*db.jobs(conn, "generated"), *db.jobs(conn, "approved")]
    p.stage("generate", len(rows))
    closed = 0
    for row in rows:
        p.item(f"still open? {_label(row)}")
        state = sources.posting_state(dict(row))
        if state == "closed":
            db.update(conn, row["id"], **sources.CLOSED)
            log.info("generate: %s (%s) closed before you applied", row["id"], row["status"])
        elif state == "paused":
            pause(conn, row)
        closed += state in {"closed", "paused"}
        p.advance()
    return closed


def generate(conn: sqlite3.Connection, prefs: triage.Prefs, facts: Facts, p: Progress | None = None) -> int:
    p = p or Progress()
    via = llm.backend(prefs["generate"]["backend"])
    effort = llm.effort(prefs["generate"].get("effort", "high"))
    cap = prefs["generate"]["max_per_run"]
    close_awaiting(conn, p)
    rows = queue(conn, prefs)
    p.stage("generate", min(cap, len(rows)))
    tried = done = 0
    # Walk down the ranking until `cap` jobs went to Claude, skipping postings that closed
    # since they were fetched (a free page check, so a closed job never costs a call),
    # and ones triaged before a hard filter was added or changed.
    for row in rows:
        if tried == cap:
            break
        p.item(_label(row))
        if (reason := triage.hard_filter(dict(row), prefs)) is not None:
            db.update(conn, row["id"], status="filtered", filter_reason=reason)
            continue
        state = sources.posting_state(dict(row))
        if state == "closed":
            db.update(conn, row["id"], **sources.CLOSED)
            log.info("generate: %s closed since it was fetched; skipped", row["id"])
            continue
        if state == "paused":
            pause(conn, row)
            continue
        if state is None:  # couldn't read the page: try again next run rather than guess
            log.warning("generate: couldn't check whether %s is still open; skipped this run", row["id"])
            continue
        tried += 1
        try:
            generate_one(conn, row["id"], facts, via, effort)
            done += 1
        except JobError:
            log.exception("generate %s failed", row["id"])
        p.advance()
    return done


def retry_errors(conn: sqlite3.Connection, *, reset_attempts: bool = False) -> int:
    """Send errored jobs back to the stage they failed in."""
    attempts = ", attempts = 0" if reset_attempts else ""
    n = conn.execute(f"UPDATE jobs SET status = 'triaged'{attempts} WHERE status = 'error' AND relevance IS NOT NULL")  # noqa: S608
    m = conn.execute(f"UPDATE jobs SET status = 'new'{attempts} WHERE status = 'error' AND relevance IS NULL")  # noqa: S608
    conn.commit()
    return n.rowcount + m.rowcount


def run(*, start: str = "ingest", skip_ingest: bool = False, trigger: str = "cli") -> dict[str, Any]:
    """Run the stages from `start` onward, tracked in the `runs` table.

    SIGTERM stops the run after the current item (status `stopped`); anything
    unexpected ends it as `failed`. Either way the next run continues from there.
    """
    if start not in STAGES:
        msg = f"start must be one of {STAGES}, not {start!r}"
        raise ValueError(msg)
    with exclusive():
        conn, prefs, facts = db.connect(), triage.load_prefs(), Facts()
        for note in recover(conn, clean_partials=True):  # we hold the lock, so nothing else is writing
            log.info("recovery: %s", note)
        retry_errors(conn)  # generation failures stay capped by MAX_ATTEMPTS (see queue())
        install_stop_handler()
        args = f"start={start}" + (" skip_ingest" if skip_ingest else "")
        t = Tracker(conn, trigger=trigger, args=args)
        stages: dict[str, Callable[[], object]] = {
            "ingest": lambda: ingest(conn, prefs, facts, t),
            "track": lambda: track(conn, prefs, facts, t),
            "filter": lambda: prefilter(conn, prefs, t),
            "fetch": lambda: fetch(conn, t),
            "triage": lambda: triage_new(conn, prefs, facts, t),
            "recheck": lambda: recheck(conn, prefs, t),
            "generate": lambda: generate(conn, prefs, facts, t),
        }
        try:
            for name in STAGES[STAGES.index(start) :]:
                if name in GMAIL_STAGES and skip_ingest:
                    continue
                try:
                    t.record(name, stages[name]())
                except JobError as e:
                    if name not in GMAIL_STAGES:
                        raise
                    # e.g. no app password yet: still process what's already queued
                    log.exception("%s failed", name)
                    t.record(f"{name}_error", str(e).splitlines()[0])
        except (StopRequestedError, KeyboardInterrupt):
            log.info("run stopped on request: %s", t.stats)
            t.finish("stopped")
        except BaseException as e:
            t.finish("failed", error=f"{type(e).__name__}: {e}")
            raise
        else:
            log.info("run: %s", t.stats)
            t.finish("done")
        return t.stats
