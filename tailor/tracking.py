"""Follow applications through replies in Gmail: interview, offer, hired, or not selected.

Free and deterministic: emails that mention a tracked job's company are read
(read-only, like ingest), sorted by phrase rules into a kind, matched to the job,
and recorded as events. A job's status only moves forward (applied -> interview ->
offer -> hired), so a stray confirmation email never undoes an interview; set it
back by hand on the dashboard if a reply was misread.
"""

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from . import db, sources
from .gmail import Message

log = logging.getLogger("tailor")

# Jobs whose replies are followed: ones with a resume you may have applied with, and
# applications already in progress (a later email can still move them on).
TRACKED = ("generated", "approved", "applied", "interview", "offer", "not_selected", "hired")
# Kinds of reply, and the status each one moves a job to (None: recorded only).
KIND_STATUS: dict[str, str | None] = {
    "received": "applied",
    "viewed": None,
    "interview": "interview",
    "not_selected": "not_selected",
    "offer": "offer",
    "hired": "hired",
}
KINDS = (*KIND_STATUS, "other", "dismissed")
# How far along each status is. A reply moves a job only to a later stage; turning
# you down after an offer isn't something an automated email gets to decide.
RANK = {"applied": 10, "interview": 20, "not_selected": 25, "offer": 30, "hired": 40}
ALERT_SENDERS = ("jobalerts-noreply@linkedin.com", "ali@hiring.cafe")
COMPANIES_PER_QUERY = 25  # Gmail search strings stay short enough for IMAP
BODY_CHARS = 20000


def _any(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(patterns), re.IGNORECASE)


# Checked in this order, against the subject and then each sentence: a rejection often
# thanks you for interviewing, and an interview invite often thanks you for applying.
RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "not_selected",
        _any(
            r"\bnot (?:to )?(?:be )?(?:mov|proceed|advanc|progress)\w* (?:forward|further|with your)",
            r"\b(?:decided|chosen|elected|opted) to (?:move|proceed|go) forward with (?:other|another)",
            r"\bmove forward with (?:other|another) (?:candidate|applicant)",
            r"\bpursue other (?:candidates|applicants)",
            r"\bother candidates whose",
            r"\b(?:have|has|were|was) not (?:been )?selected",
            r"\bposition has been filled",
            r"\bno longer (?:being )?consider",
            r"\bregret to (?:inform|let you know)",
            r"\bunable to (?:offer|move forward|proceed)",
            r"\bdecided not to (?:proceed|move forward)",
        ),
    ),
    (
        "hired",
        _any(
            r"\bwelcome (?:aboard|to the team)",
            r"\byour first day\b",
            r"\bnew hire (?:paperwork|orientation|onboarding)",
            r"\bonboarding (?:paperwork|documents|portal|tasks)",
        ),
    ),
    (
        "offer",
        _any(
            r"\b(?:pleased|delighted|excited|happy|thrilled) to (?:extend|offer)",
            r"\boffer letter\b",
            r"\bextend (?:you )?an offer",
            r"\b(?:job|employment|formal|verbal) offer\b",
            r"\boffer of employment",
        ),
    ),
    (
        "interview",
        _any(
            r"\binvit\w* you to (?:an? )?(?:interview|phone screen|chat|call|meet)",
            r"\b(?:schedule|book|set up)\b.{0,30}\b(?:interview|phone screen|call|chat)\b",
            r"\binterview (?:invitation|request|confirm\w*|scheduled|details)",
            r"\bphone screen\b",
            r"\byour availability\b",
            r"\b(?:online|coding|technical) (?:assessment|challenge)",
            r"\btake[- ]home\b",
            r"\bhackerrank\b|\bcodesignal\b|\bcalendly\.com\b",
        ),
    ),
    (
        "received",
        _any(
            r"\b(?:thank you|thanks) for (?:applying|your application|submitting)",
            r"\b(?:we(?:'ve| have)|has been) received your application",
            r"\byour application (?:was|has been|is) (?:sent|submitted|received)",
            r"\bapplication (?:received|submitted|confirmation)\b",
            r"\bsuccessfully (?:applied|submitted)",
        ),
    ),
    ("viewed", _any(r"\bviewed your application", r"\byour application was viewed")),
]
# Good news only counts when it's stated, not promised: "if selected, we'll reach out
# to schedule an interview" is a confirmation email.
GOOD_NEWS = frozenset({"hired", "offer", "interview"})
_HEDGED = re.compile(
    r"\b(?:if|should|may|might|once|whether|in case)\b|\breach out\b|\bbe in touch\b|\bcontact you\b", re.IGNORECASE
)
_SUBJECT_INTERVIEW = re.compile(r"\binterview", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_COMPANY_SUFFIX = re.compile(
    r"[,.]?\s+(?:inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|gmbh|lp|l\.p)\.?$", re.IGNORECASE
)


@dataclass
class Reply:
    kind: str
    evidence: str = ""


def classify(subject: str, body: str) -> Reply:
    """What a reply says about the application, from fixed phrase rules."""
    sentences = [subject, *(s.strip() for s in _SENTENCE.split(body[:BODY_CHARS]) if s.strip())]
    for kind, pattern in RULES:
        for sentence in sentences:
            if pattern.search(sentence) and not (kind in GOOD_NEWS and _HEDGED.search(sentence)):
                return Reply(kind, sentence[:300])
    if _SUBJECT_INTERVIEW.search(subject):
        return Reply("interview", subject[:300])
    return Reply("other")


def core_name(company: str | None) -> str:
    """The company name as people write it in emails: "Acme, Inc." -> "Acme"."""
    name = re.sub(r"\s*\([^)]*\)", "", company or "").strip()
    while (shorter := _COMPANY_SUFFIX.sub("", name).strip()) != name:
        name = shorter
    return name


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    marks = ", ".join("?" * len(TRACKED))
    sql = f"SELECT * FROM jobs WHERE status IN ({marks}) AND company IS NOT NULL"  # noqa: S608
    return [r for r in conn.execute(sql, TRACKED) if len(core_name(r["company"])) >= 2]  # noqa: PLR2004


def match(msg: Message, jobs: list[sqlite3.Row]) -> sqlite3.Row | None:
    """The tracked job a reply is about: its company named by the sender, subject or body."""
    addr = re.search(r"@([\w.-]+)", msg.sender)
    domain = _compact(addr.group(1)) if addr else ""
    display = msg.sender.split("<", 1)[0]
    text = f"{msg.subject}\n{msg.text[:BODY_CHARS]}"
    best: tuple[tuple[int, str], sqlite3.Row] | None = None
    for job in jobs:
        name = core_name(job["company"])
        # Case-sensitive, since company names are proper nouns: "Box" isn't "a box".
        named = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        by_domain = len(_compact(name)) >= 4 and _compact(name) in domain  # noqa: PLR2004
        if named.search(msg.subject) or named.search(display) or by_domain:
            score = 3
        elif named.search(msg.text[:BODY_CHARS]):
            score = 1
        else:
            continue
        if job["title"] and job["title"].lower() in text.lower():
            score += 2
        if job["status"] in RANK:
            score += 1
        key = (score, str(job["applied_at"] or job["updated_at"]))
        if best is None or key > best[0]:
            best = (key, job)
    return best[1] if best else None


def advance(conn: sqlite3.Connection, job: sqlite3.Row, status: str, at: str | None = None) -> bool:
    """Move a job to `status` if that's further along than where it is. Returns True if moved."""
    if RANK[status] <= RANK.get(job["status"], 0):
        return False
    fields: dict[str, object] = {"status": status}
    if not job["applied_at"]:
        fields["applied_at"] = at or db.now()
    db.update(conn, job["id"], **fields)
    return True


def set_status(conn: sqlite3.Connection, job_id: str, status: str) -> None:
    """A status set by hand (any direction), recorded in the job's history."""
    job = db.get_required(conn, job_id)
    fields: dict[str, object] = {"status": status}
    if status in RANK and not job["applied_at"]:
        fields["applied_at"] = db.now()
    db.update(conn, job_id, **fields)
    if status in RANK:
        db.add_event(conn, job_id, status, source="manual")


def record(conn: sqlite3.Connection, msg: Message, jobs: list[sqlite3.Row]) -> bool:
    """Read one email: record what it says and move its job on. Returns True if a status changed."""
    reply = classify(msg.subject, msg.text)
    at = sources.iso_utc(datetime.fromisoformat(msg.received_at)) if msg.received_at else None
    if reply.kind == "other":  # remembered only so it isn't read again
        db.add_event(conn, None, "other", source="email", message_id=msg.message_id, at=at)
        return False
    job = match(msg, jobs)
    db.add_event(
        conn,
        None if job is None else job["id"],
        reply.kind,
        source="email",
        message_id=msg.message_id,
        sender=msg.sender,
        subject=msg.subject,
        evidence=reply.evidence,
        at=at,
    )
    if job is None:
        log.info("track: %s reply matched no tracked job (%s)", reply.kind, msg.subject[:60])
        return False
    status = KIND_STATUS[reply.kind]
    moved = status is not None and advance(conn, job, status, at)
    log.info("track: %s %s for %s%s", reply.kind, msg.subject[:60], job["id"], f" -> {status}" if moved else "")
    return moved


def assign(conn: sqlite3.Connection, event_id: int, job_id: str) -> bool:
    """Attach an unmatched reply to a job, and apply it as if it had matched."""
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if event is None:
        msg = f"no event with id {event_id}"
        raise KeyError(msg)
    conn.execute("UPDATE events SET job_id = ? WHERE id = ?", (job_id, event_id))
    conn.commit()
    status = KIND_STATUS.get(event["kind"])
    return status is not None and advance(conn, db.get_required(conn, job_id), status, event["at"])


def dismiss(conn: sqlite3.Connection, event_id: int) -> None:
    """Drop an unmatched reply that isn't about any job (it stays read)."""
    conn.execute("UPDATE events SET kind = 'dismissed' WHERE id = ? AND job_id IS NULL", (event_id,))
    conn.commit()


def unmatched(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    sql = "SELECT * FROM events WHERE job_id IS NULL AND kind NOT IN ('other', 'dismissed') ORDER BY at DESC"
    return cast("list[sqlite3.Row]", conn.execute(sql).fetchall())


def queries(conn: sqlite3.Connection, jobs: list[sqlite3.Row], extra: str = "") -> list[str]:
    """Gmail searches for mail naming a tracked company, since the last reply read.

    The first search goes back to when the oldest tracked job was found; a day of
    overlap covers Gmail's date boundaries, and emails already read are skipped.
    """
    if not jobs:
        return []
    last = conn.execute("SELECT max(at) FROM events WHERE source = 'email'").fetchone()[0]
    since = datetime.fromisoformat(last or min(str(j["found_at"]) for j in jobs)) - timedelta(days=1)
    names = sorted({core_name(j["company"]).replace('"', "") for j in jobs})
    alerts = " OR ".join(ALERT_SENDERS)
    out = []
    for i in range(0, len(names), COMPANIES_PER_QUERY):
        terms = " OR ".join(f'"{n}"' for n in names[i : i + COMPANIES_PER_QUERY])
        # -from:me: All Mail includes what you sent, and your own "thanks for the interview" isn't news.
        out.append(f"({terms}) after:{since:%Y/%m/%d} -from:me -from:({alerts}) {extra}".strip())
    return out
