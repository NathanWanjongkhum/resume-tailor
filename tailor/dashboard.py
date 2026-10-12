"""Review dashboard: `uv run python -m tailor dashboard`, then open http://localhost:8501."""

import difflib
import json
import re
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

# Streamlit runs this file as a script, so make the package importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor import control, db, github, llm, pipeline, profile, progress, triage
from tailor.facts import Facts

Job = dict[str, Any]

STATUS_LABELS = {
    "new": "Pending",
    "filtered": "Filtered out",
    "triaged": "Scored",
    "generated": "To review",
    "approved": "Approved",
    "applied": "Applied",
    "rejected": "Rejected",
    "error": "Error",
}
STATUS_COLORS = {"generated": "blue", "approved": "green", "applied": "violet", "rejected": "gray", "error": "red"}
STAGE_LABELS = {
    "ingest": "Read Gmail",
    "filter": "Filter",
    "fetch": "Fetch postings",
    "triage": "Score jobs",
    "generate": "Write resumes",
}
RUN_ICONS = {"done": "✅", "stopped": "⏹️", "failed": "❌", "interrupted": "⚠️", "running": "▶️"}
NOON = 12

st.set_page_config(page_title="Resume Tailor", page_icon=":material/description:", layout="wide")

_local = threading.local()


def db_conn() -> sqlite3.Connection:
    """This thread's database connection.

    Streamlit runs the page and its auto-refreshing fragments on different threads,
    and a sqlite3 connection may only be used by the thread that created it.
    """
    conn = getattr(_local, "conn", None)
    if not isinstance(conn, sqlite3.Connection):
        conn = _local.conn = db.connect()
    return conn


def flash(kind: str, text: str) -> None:
    """Show a message after the next rerun (st.rerun would otherwise discard it)."""
    st.session_state["flash"] = (kind, text)


def show_flash() -> None:
    if msg := st.session_state.pop("flash", None):
        {"success": st.success, "error": st.error, "info": st.info}[msg[0]](msg[1])


def ago(iso: str | None) -> float:
    if iso is None:
        return 0.0
    return (datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds()


def duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {secs:02d}s"


def local_time(iso: str | None) -> str:
    if iso is None:
        return ""
    return datetime.fromisoformat(iso).astimezone().strftime("%a %-I:%M %p")


def systemd_time(value: str) -> str:
    """'Mon 2026-10-12 08:02:10 PDT' -> 'Mon 8:02 AM'."""
    m = re.match(r"(\w{3}) \d{4}-\d{2}-\d{2} (\d{2}):(\d{2})", value)
    if m is None:
        return value
    day, hour, minute = m.group(1), int(m.group(2)), m.group(3)
    return f"{day} {hour % NOON or NOON}:{minute} {'AM' if hour < NOON else 'PM'}"


def md(text: object) -> str:
    """Text from emails or the model, safe to put in markdown: `$` would start LaTeX math."""
    return str(text).replace("$", "\\$")


def score(job: Job) -> int:
    return triage.total_score(job)


def all_jobs() -> list[Job]:
    return [dict(r) for r in db.jobs(db_conn())]


def safe_link(job: Job) -> str | None:
    """The job's link, only if it's http(s): links come from emails."""
    link = str(job["url"] or job["apply_url"] or "")
    return link if link.startswith(("https://", "http://")) else None


def generation_settings() -> tuple[llm.Backend, llm.Effort]:
    g = triage.load_prefs()["generate"]
    return llm.backend(g["backend"]), llm.effort(g.get("effort", "high"))


def write_resume(job: Job) -> None:
    with st.spinner(f"Writing a resume for {job['company'] or job['title']}… (about a minute)"):
        try:
            with pipeline.exclusive():
                via, effort = generation_settings()
                pipeline.generate_one(db_conn(), job["id"], Facts(), via, effort)
            st.session_state["selected_resume"] = job["id"]
            flash("success", f"Resume written for {job['company']}. It's in Resumes.")
        except pipeline.AlreadyRunningError:
            flash("error", "The pipeline is running; try again when it finishes.")
        except pipeline.JobError as e:
            flash("error", f"Couldn't write the resume: {e}")
    st.rerun()


def set_status(job: Job, status: str) -> None:
    db.update(db_conn(), job["id"], status=status)
    flash("success", f"{job['company']}: marked {STATUS_LABELS[status].lower()}.")
    st.rerun()


def launch(*, start_stage: str = "ingest", skip_ingest: bool = False) -> None:
    try:
        control.start(start_stage=start_stage, skip_ingest=skip_ingest)
        flash("success", f"Pipeline started from {STAGE_LABELS[start_stage]}.")
    except (OSError, subprocess.CalledProcessError) as e:
        flash("error", f"Couldn't start the pipeline: {e}")
    st.rerun()


# `mtime` is unused in the body but is part of the cache key, so a regenerated PDF
# gets a fresh preview.
@st.cache_data(show_spinner=False)
def page_images(pdf: str, mtime: float) -> list[bytes]:  # noqa: ARG001
    prefix = Path(pdf).with_suffix("")
    subprocess.run(["pdftoppm", "-r", "110", "-png", pdf, f"{prefix}-preview"], check=True)
    return [p.read_bytes() for p in sorted(prefix.parent.glob(f"{prefix.name}-preview*.png"))]


def report_sections(outdir: Path) -> dict[str, str]:
    """report.md split by its `## ` headings."""
    path = outdir / "report.md"
    parts = re.split(r"^## (.+)$", path.read_text() if path.exists() else "", flags=re.MULTILINE)
    return {parts[i].strip(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def score_badges(job: Job) -> str:
    if job["relevance"] is None:
        return ""
    fit, want, fresh = job["relevance"], job["desirability"], triage.recency(job["posted_at"])
    return f":blue-badge[Fit {fit}/5] :green-badge[Want {want}/5] :orange-badge[Fresh {fresh}/5]"


def status_badge(status: str) -> str:
    return f":{STATUS_COLORS.get(status, 'gray')}-badge[{STATUS_LABELS.get(status, status)}]"


def job_heading(job: Job) -> None:
    st.markdown(f"### {md(job['title'])}")
    pay = f" · {job['salary']}" if job["salary"] else ""
    st.caption(md(f"{job['company'] or 'Unknown company'} · {job['location'] or 'Location not listed'}{pay}"))


@st.fragment(run_every=3)
def status_line(*, link: bool) -> None:
    """One line on every page: live progress while running, else the last and next run."""
    conn = db_conn()
    progress.recover(conn, clean_partials=False)
    live = progress.live_run(conn)
    was_running = st.session_state.get("was_running", False)
    st.session_state["was_running"] = live is not None
    if was_running and live is None:
        st.rerun(scope="app")  # a run just ended: refresh everything
    if live is not None:
        done, total = live["stage_done"] or 0, live["stage_total"]
        where = f"{STAGE_LABELS.get(live['stage'], 'Starting')} {done} of {total if total is not None else '…'}"
        text = f":blue-badge[:material/sync: Running] {where}"
    else:
        last = progress.runs(conn, limit=1)
        timer = control.timer_state()
        parts = [f"Last run {last[0]['status']} {local_time(last[0]['finished_at'])}"] if last else []
        parts.append(f"next {systemd_time(timer.next_run)}" if timer.enabled else "daily run off")
        text = f":gray[{' · '.join(parts)}]"
    with st.container(horizontal=True, vertical_alignment="center", horizontal_alignment="right", gap="medium"):
        st.markdown(text, width="content")
        if link:
            st.page_link(PIPELINE_PAGE, label="Pipeline", icon=":material/arrow_forward:", width="content")


def resume_card(job: Job, *, selected: bool) -> None:
    with st.container(border=True):
        st.markdown(f"**{md(job['company'] or 'Unknown company')}**  \n{md(job['title'])}")
        failed = " :red-badge[Check failed]" if job["checks_ok"] == 0 else ""
        with st.container(horizontal=True, vertical_alignment="center", horizontal_alignment="distribute"):
            st.markdown(f"{score_badges(job)}{failed}", width="content")
            st.button(
                "Viewing" if selected else "Open",
                key=f"open-{job['id']}",
                icon=":material/visibility:" if selected else None,
                type="tertiary" if selected else "secondary",
                disabled=selected,
                on_click=lambda: st.session_state.update(selected_resume=job["id"]),
            )


def resume_actions(job: Job, pdf: Path) -> None:
    last_name = Facts().raw["contact"]["name"].split()[-1]
    filename = re.sub(r"[^A-Za-z0-9]+", "_", f"{last_name} Resume {job['company'] or ''}").strip("_")
    with st.container(horizontal=True, gap="small"):
        st.download_button(
            "Download PDF",
            pdf.read_bytes(),
            f"{filename}.pdf",
            "application/pdf",
            type="primary",
            icon=":material/download:",
        )
        if job["status"] != "applied" and st.button("Mark applied", icon=":material/send:"):
            set_status(job, "applied")
        if job["status"] == "generated" and st.button("Approve", icon=":material/check:"):
            set_status(job, "approved")
        if (link := safe_link(job)) is not None:
            st.link_button("Posting", link, icon=":material/open_in_new:")
        with st.popover("More", icon=":material/more_horiz:"):
            st.download_button("Download .tex", (pdf.parent / "resume.tex").read_bytes(), f"{filename}.tex")
            if st.button("Regenerate", help="Write a fresh version (about a minute, uses your plan)"):
                write_resume(job)
            if job["status"] != "rejected" and st.button("Reject"):
                set_status(job, "rejected")
            if job["status"] in {"approved", "applied", "rejected"} and st.button("Back to review"):
                set_status(job, "generated")


def why_tab(outdir: Path) -> None:
    tailored = json.loads((outdir / "tailored.json").read_text())
    st.markdown(md(tailored["fit_summary"]))
    matched, gaps = st.columns(2)
    with matched, st.container(border=True):
        st.markdown("**Covered**")
        st.markdown("\n".join(f"- {md(m)}" for m in tailored["matched_requirements"]) or "—")
    with gaps, st.container(border=True):
        st.markdown("**Not on the resume**")
        st.markdown("\n".join(f"- {md(g)}" for g in tailored["gaps"]) or "Nothing missing.")
    st.markdown("**Projects, in order**")
    st.markdown("\n".join(f"{i}. {md(p['reason'])}" for i, p in enumerate(tailored["projects"], 1)))


def checks_tab(outdir: Path) -> None:
    for heading, body in report_sections(outdir).items():
        if heading.startswith(("Checks", "Trimmed", "Skills added", "Wording changes")):
            st.markdown(f"**{heading}**")
            st.markdown(md(body) or "—")


def resume_detail(job: Job) -> None:
    job_heading(job)
    outdir = Path(job["outdir"])
    pdf = outdir / "resume.pdf"
    if not pdf.exists():
        st.warning("The resume files for this job are missing.")
        return
    if not job["posting_ok"]:
        st.info("Written from the email summary only; the full posting couldn't be fetched.")
    resume_actions(job, pdf)
    preview, why, checks, posting = st.tabs(["Resume", "Why it fits", "Checks & changes", "Posting"])
    with preview:
        for img in page_images(str(pdf), pdf.stat().st_mtime):
            st.image(img, width="stretch")
    with why:
        why_tab(outdir)
    with checks:
        checks_tab(outdir)
    with posting:
        st.text(job["posting"] or job["summary"] or "(none)")


def resumes_page() -> None:
    show_flash()
    jobs = [j for j in all_jobs() if j["outdir"]]
    if not jobs:
        st.info("No resumes yet. Run the pipeline, or pick a job in **Jobs** and write one.")
        return
    groups = {
        "To review": [j for j in jobs if j["status"] == "generated"],
        "Approved": [j for j in jobs if j["status"] == "approved"],
        "Applied": [j for j in jobs if j["status"] == "applied"],
        "Rejected": [j for j in jobs if j["status"] == "rejected"],
    }
    left, right = st.columns([1, 2.4], gap="large")
    with left:
        options = [name for name, items in groups.items() if items or name == "To review"]
        group = st.segmented_control(
            "Show",
            options,
            default="To review",
            format_func=lambda g: f"{g} {len(groups[g])}",
            label_visibility="collapsed",
        )
        shown = sorted(groups[group or "To review"], key=score, reverse=True)
        if st.session_state.get("selected_resume") not in {j["id"] for j in shown} and shown:
            st.session_state["selected_resume"] = shown[0]["id"]
        with st.container(height=780, border=False):
            if not shown:
                st.caption("Nothing here yet.")
            for job in shown:
                resume_card(job, selected=job["id"] == st.session_state.get("selected_resume"))
    with right:
        selected = next((j for j in shown if j["id"] == st.session_state.get("selected_resume")), None)
        if selected is None:
            st.caption("Pick a resume on the left.")
        else:
            resume_detail(selected)


def job_notes(job: Job) -> None:
    st.markdown(f"{status_badge(job['status'])} {score_badges(job)}")
    if job["triage"]:
        t = json.loads(job["triage"])
        st.markdown(md(t["reasons"]))
        if t.get("red_flags"):
            st.markdown("\n".join(f"- :orange[{md(flag)}]" for flag in t["red_flags"]))
    if job["filter_reason"]:
        st.caption(md(f"Filtered out: {job['filter_reason']}"))
    if job["error"]:
        st.error(job["error"])
    if not job["posting_ok"]:
        st.caption("Only the email summary is available for this posting.")


def job_detail(job: Job) -> None:
    job_heading(job)
    job_notes(job)
    with st.container(horizontal=True, gap="small"):
        if job["outdir"]:
            if st.button("Open resume", type="primary", icon=":material/description:"):
                st.session_state["selected_resume"] = job["id"]
                st.switch_page(RESUMES_PAGE)
        elif st.button(
            "Write resume",
            type="primary",
            icon=":material/edit_note:",
            disabled=pipeline.is_running(),
            help="About a minute; uses your Claude plan",
        ):
            write_resume(job)
        if (link := safe_link(job)) is not None:
            st.link_button("Posting", link, icon=":material/open_in_new:")
        if job["status"] not in {"rejected", "filtered"} and st.button("Not interested"):
            set_status(job, "rejected")
    with st.expander("Posting text"):
        st.text(job["posting"] or job["summary"] or "(none)")


JOB_VIEWS = {
    "Scored": lambda j: j["status"] == "triaged",
    "Has resume": lambda j: bool(j["outdir"]),
    "Filtered out": lambda j: j["status"] == "filtered",
    "All": lambda _j: True,
}


def jobs_page() -> None:
    show_flash()
    with st.container(horizontal=True, vertical_alignment="bottom", gap="medium"):
        view = st.segmented_control("Show", list(JOB_VIEWS), default="Scored") or "Scored"
        query = st.text_input("Search", placeholder="Company or role")
        min_score = st.slider("Minimum fit + want + fresh", 3, 15, 9, width=240, disabled=view == "Filtered out")
        with st.popover("Add by URL", icon=":material/add_link:"):
            url = st.text_input("Posting URL", placeholder="https://www.linkedin.com/jobs/view/…")
            if url and st.button("Add", type="primary"):
                flash("success", f"Added {pipeline.add_url(db_conn(), url)}; it's fetched and scored on the next run.")
                st.rerun()
    needle = query.lower().strip()
    shown = [
        j
        for j in all_jobs()
        if JOB_VIEWS[view](j)
        and (view == "Filtered out" or score(j) >= min_score)
        and needle in f"{j['company']} {j['title']}".lower()
    ]
    shown.sort(key=score, reverse=True)
    if not shown:
        st.info("No jobs match. Lower the minimum score or change the view.")
        return
    left, right = st.columns([1.6, 1], gap="large")
    with left:
        table = pd.DataFrame(
            [
                {
                    "Fit": j["relevance"],
                    "Want": j["desirability"],
                    "Fresh": triage.recency(j["posted_at"]),
                    "Company": j["company"],
                    "Role": j["title"],
                    "Location": j["location"],
                    **({"Status": STATUS_LABELS.get(j["status"], j["status"])} if view == "All" else {}),
                    "": "" if j["posting_ok"] else "summary only",
                }
                for j in shown
            ]
        )
        picked = st.dataframe(
            table,
            hide_index=True,
            height=760,
            on_select="rerun",
            selection_mode="single-row",
            column_config={
                "Fit": st.column_config.NumberColumn(format="%d", width="small"),
                "Want": st.column_config.NumberColumn(format="%d", width="small"),
                "Fresh": st.column_config.NumberColumn(format="%d", width="small"),
                "Role": st.column_config.TextColumn(width="medium"),
                "Location": st.column_config.TextColumn(width="small"),
            },
        )
    with right:
        rows = picked.selection.rows
        if rows:
            job_detail(shown[rows[0]])
        else:
            st.caption(f"{len(shown)} jobs. Select one to see why it scored that way, or to write a resume.")


def stage_strip(run: Job) -> None:
    args = run.get("args") or ""
    start = args.removeprefix("start=").split(" ")[0] or "ingest"
    first = progress.STAGES.index(start) if start in progress.STAGES else 0
    current = progress.STAGES.index(run["stage"]) if run["stage"] in progress.STAGES else -1
    seconds = json.loads(run["stage_seconds"] or "{}")
    for col, (i, name) in zip(st.columns(len(progress.STAGES)), enumerate(progress.STAGES), strict=True):
        with col, st.container(border=True):
            if i < first or (name == "ingest" and "skip_ingest" in args):
                st.caption(f":material/skip_next: {STAGE_LABELS[name]}  \nskipped")
            elif i < current or (i == current and run["status"] == "done"):
                st.caption(f":material/check_circle: {STAGE_LABELS[name]}  \n{duration(seconds.get(name, 0))}")
            elif i == current:
                state = "in progress" if run["status"] == "running" else run["status"]
                st.markdown(f"{RUN_ICONS.get(run['status'], '▶️')} **{STAGE_LABELS[name]}**  \n:gray[{state}]")
            else:
                st.caption(f":material/radio_button_unchecked: {STAGE_LABELS[name]}")


def live_controls(run: Job) -> None:
    run_id = int(run["id"])
    done, total = run["stage_done"] or 0, run["stage_total"]
    if total:
        elapsed = ago(run["stage_started_at"])
        eta = f" · about {duration(elapsed / done * (total - done))} left" if done else ""
        st.progress(min(done / total, 1.0), text=f"{STAGE_LABELS[run['stage']]}: {done} of {total}{eta}")
    else:
        st.progress(0.0, text=f"{STAGE_LABELS.get(run['stage'], 'Starting')}: counting…")
    if run["current"]:
        st.caption(md(f"Working on: {run['current']}"))
    # Buttons are keyed by run id: a click can only ever act on the run it was shown for.
    confirming = st.session_state.get("confirm_kill") == run_id
    with st.container(horizontal=True, gap="small"):
        if st.button("Stop after this item", key=f"stop-{run_id}", icon=":material/pause:"):
            st.toast(progress.stop(db_conn(), run_id=run_id, source="dashboard"))
        if not confirming and st.button("Stop now…", key=f"kill-{run_id}", icon=":material/stop:"):
            st.session_state["confirm_kill"] = run_id
            st.rerun(scope="fragment")
    if confirming:
        st.warning("Kill this run now? The job it's on is retried next run.")
        with st.container(horizontal=True, gap="small"):
            if st.button("Kill it", key=f"kill-yes-{run_id}", type="primary"):
                st.session_state.pop("confirm_kill", None)
                st.toast(progress.stop(db_conn(), now=True, run_id=run_id, source="dashboard"))
            if st.button("Cancel", key=f"kill-no-{run_id}"):
                st.session_state.pop("confirm_kill", None)
                st.rerun(scope="fragment")


def idle_controls() -> None:
    conn = db_conn()
    resume = progress.resume_stage(conn)
    last = progress.runs(conn, limit=1)
    if resume is not None and last:
        r = dict(last[0])
        stage_strip(r)
        st.warning(
            f"The last run {r['status']} during **{STAGE_LABELS[resume]}**. "
            "Work finished before that is saved; resuming picks up the rest."
        )
    with st.container(horizontal=True, gap="small"):
        if resume is not None and st.button(
            f"Resume from {STAGE_LABELS[resume]}", type="primary", icon=":material/play_arrow:"
        ):
            launch(start_stage=resume)
        if st.button(
            "Run now",
            type="primary" if resume is None else "secondary",
            icon=":material/play_arrow:",
            help="Read Gmail, then every stage",
        ):
            launch()
        if st.button("Run without Gmail", help="Process jobs already in the database"):
            launch(skip_ingest=True)


@st.fragment(run_every=2)
def run_panel() -> None:
    live = progress.live_run(db_conn())
    with st.container(border=True):
        if live is not None:
            run = dict(live)
            started = f"started {local_time(run['started_at'])} · {duration(ago(run['started_at']))}"
            st.markdown(f"**Run {run['id']}** · {started}")
            stage_strip(run)
            live_controls(run)
        else:
            st.markdown("**Run the pipeline**")
            st.caption("Reads new alert emails, fetches and scores the jobs, then writes resumes for the best ones.")
            idle_controls()


def schedule_card() -> None:
    with st.container(border=True):
        timer = control.timer_state()
        st.markdown("**Schedule**")
        on = st.toggle("Run every day at 8:00 AM", value=timer.enabled)
        if on != timer.enabled:
            flash("info", control.set_timer(on=on))
            st.rerun()
        prefs = triage.load_prefs()
        st.caption(
            f"Next: {systemd_time(timer.next_run) if timer.enabled else 'off'} · "
            f"up to {prefs['generate']['max_per_run']} resumes per run · "
            f"effort: {prefs['triage']['effort']} scoring, {prefs['generate'].get('effort', 'high')} writing"
        )


def failed_card() -> None:
    conn = db_conn()
    with st.container(border=True):
        errored = db.jobs(conn, "error")
        capped = conn.execute("SELECT COUNT(*) FROM jobs WHERE attempts >= ?", (db.MAX_ATTEMPTS,)).fetchone()[0]
        st.markdown("**Failed jobs**")
        if not errored and not capped:
            st.caption("None. Jobs that fail are retried automatically on the next run.")
            return
        st.caption(f"{len(errored)} waiting for retry · {capped} skipped after {db.MAX_ATTEMPTS} failed attempts")
        if st.button("Retry all now"):
            capped_sql = "UPDATE jobs SET status = 'error' WHERE attempts >= ? AND status = 'triaged'"
            conn.execute(capped_sql, (db.MAX_ATTEMPTS,))
            n = pipeline.retry_errors(conn, reset_attempts=True)
            flash("success", f"{n} jobs requeued for the next run.")
            st.rerun()


def run_note(run: Job) -> str:
    error = run["error"] or ""
    if m := re.match(r"(kill|stop after current item) requested from (\w+)", error):
        return f"{'killed' if m.group(1) == 'kill' else 'stopped'} from {m.group(2)}"
    if error.startswith("process ended without finishing"):
        return f"ended unexpectedly during {STAGE_LABELS.get(run['stage'], run['stage'])}"
    return str(error).split("\n", 1)[0][:120]


def history_table() -> None:
    st.markdown("**History**")
    history = [dict(r) for r in progress.runs(db_conn(), limit=20)]
    if not history:
        st.caption("No runs yet.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Run": r["id"],
                    "Result": f"{RUN_ICONS.get(r['status'], '')} {r['status']}",
                    "Started": local_time(r["started_at"]),
                    "Took": duration(ago(r["started_at"]) - ago(r["finished_at"])) if r["finished_at"] else "",
                    "By": r["trigger"],
                    "Done": ", ".join(f"{k} {v}" for k, v in json.loads(r["stats"] or "{}").items()),
                    "Note": run_note(r),
                }
                for r in history
            ]
        ),
        hide_index=True,
        width="stretch",
    )


def pipeline_page() -> None:
    show_flash()
    run_panel()
    schedule, failed = st.columns(2, gap="medium")
    with schedule:
        schedule_card()
    with failed:
        failed_card()
    history_table()


PROFILE_HELP = {
    "facts": "Every claim a resume may make. Claude only selects and rephrases these.",
    "preferences": "Gmail query, hard filters, scoring thresholds, and what you want and avoid.",
}


def upload_date(iso: str | None) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%b %-d, %Y %-I:%M %p") if iso is not None else "never"


def missing_profile() -> list[str]:
    return [kind for kind in profile.UPLOADABLE if profile.get(db_conn(), kind) is None]


def start_from_example(kind: str, editor_key: str) -> None:
    st.session_state[editor_key] = (profile.EXAMPLES / f"{kind}.yaml").read_text()


def profile_review(kind: str, current: str | None, draft: str) -> None:
    """Check a proposed version and save it only after the user confirms."""
    review_key = f"review-{kind}"

    def discard() -> None:
        st.session_state.pop(review_key, None)

    with st.container(border=True):
        st.markdown(f"**Review {kind}**")
        try:
            profile.check(kind, draft)
        except ValueError as e:
            st.error(f"This can't be saved: {e}")
            st.button("Back", key=f"back-{kind}", on_click=discard)
            return
        if draft == current:
            st.info("This is the same as what's stored; nothing to save.")
            st.button("Back", key=f"back-{kind}", on_click=discard)
            return
        st.markdown("\n".join(f"- {md(line)}" for line in profile.changes(kind, current, draft)))
        if kind == "facts" and "@example.com" in draft:
            st.warning(
                "These are the example facts for a fictional person. Replace them with yours before writing resumes."
            )
        if current is not None:
            diff = difflib.unified_diff(
                current.splitlines(), draft.splitlines(), "stored", "proposed", lineterm="", n=2
            )
            with st.expander("Line-by-line diff"):
                st.code("\n".join(diff), language="diff")
            st.caption(f"The stored {kind} will be replaced. It's kept under History, so you can restore it.")
        with st.container(horizontal=True, gap="small"):
            if st.button(
                f"Replace {kind}" if current is not None else f"Save {kind}", key=f"confirm-{kind}", type="primary"
            ):
                was_missing = missing_profile()
                profile.put(db_conn(), kind, draft)
                discard()
                if was_missing and not missing_profile():
                    flash("success", "Profile set up. Run the pipeline from **Pipeline** to find and tailor jobs.")
                    st.switch_page(RESUMES_PAGE)
                flash("success", f"Saved {kind}.")
                st.rerun()
            st.button("Discard", key=f"discard-{kind}", on_click=discard)


def edit_profile(kind: str, current: str | None, editor_key: str) -> None:
    if current is None:
        st.button(
            "Start from the example",
            key=f"example-{kind}",
            icon=":material/content_copy:",
            on_click=start_from_example,
            args=(kind, editor_key),
        )
    text = st.text_area(kind, current or "", height=420, key=editor_key, label_visibility="collapsed")
    if st.button("Review changes", key=f"review-edit-{kind}", type="primary", disabled=not text.strip()):
        st.session_state[f"review-{kind}"] = text
        st.rerun()


def upload_profile(kind: str, updated: str | None) -> None:
    upload = st.file_uploader(f"Upload {kind}.yaml", type=["yaml", "yml"], key=f"upload-{kind}-{updated}")
    if upload is not None and st.button("Review upload", key=f"review-upload-{kind}", type="primary"):
        try:
            st.session_state[f"review-{kind}"] = upload.getvalue().decode()
        except UnicodeDecodeError:
            st.error("That file isn't UTF-8 text.")
        else:
            st.rerun()


def profile_history(kind: str, older: list[sqlite3.Row]) -> None:
    for old in older:
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(f"Replaced {upload_date(old['replaced_at'])}")
            if st.button("Review restore", key=f"restore-{kind}-{old['id']}"):
                st.session_state[f"review-{kind}"] = old["body"]
                st.rerun()


def profile_card(kind: str) -> None:
    conn = db_conn()
    current, updated = profile.get(conn, kind), profile.updated_at(conn, kind)
    with st.container(border=True):
        st.markdown(f"**{kind.capitalize()}**")
        st.caption(f"{PROFILE_HELP[kind]} Last saved: {upload_date(updated)}.")
        if current is None:
            st.warning(
                f"No {kind} yet. Upload your file, or start from the example and replace its contents with yours."
            )
        if (draft := st.session_state.get(f"review-{kind}")) is not None:
            profile_review(kind, current, draft)
            return
        older = profile.history(conn, kind)
        modes = ["Edit", "Upload", *(["History"] if older else [])]
        mode = st.segmented_control(
            "Change", modes, default="Upload" if current is None else "Edit", key=f"mode-{kind}"
        )
        if mode == "Edit":
            edit_profile(kind, current, f"editor-{kind}-{updated}")  # a fresh editor after each save
        elif mode == "Upload":
            upload_profile(kind, updated)
        elif mode == "History":
            profile_history(kind, older)
        with st.container(horizontal=True, gap="small"):
            if current is not None:
                st.download_button("Download current", current, f"{kind}.yaml", "text/yaml", key=f"dl-{kind}")
            example = profile.EXAMPLES / f"{kind}.yaml"
            st.download_button("Download example", example.read_text(), f"{kind}.yaml", "text/yaml", key=f"ex-{kind}")


def github_card() -> None:
    conn = db_conn()
    evidence = profile.load("github", conn)
    with st.container(border=True):
        st.markdown("**GitHub skill evidence**")
        st.caption(
            "Skills your public repos show (languages and dependencies). They can be listed on a resume, "
            "never turned into bullets. Scanning uses the GitHub API, not Claude."
        )
        facts = profile.load("facts", conn) if profile.get(conn, "facts") is not None else None
        link = (facts or {}).get("contact", {}).get("github") or ""
        owner = link.rstrip("/").rsplit("/", 1)[-1]
        if evidence:
            st.markdown(f"{len(evidence)} repos scanned, last {upload_date(profile.updated_at(conn, 'github'))}.")
        else:
            st.warning("No evidence yet. Scan once your facts are saved with your GitHub link.")
        if st.button(
            "Scan GitHub repos",
            icon=":material/sync:",
            disabled=not owner,
            help=None if owner else "Add `contact.github` to your facts first",
        ):
            with st.spinner(f"Scanning github.com/{owner} (only repos changed since the last scan)..."):
                try:
                    fresh = github.refresh(owner)
                except (OSError, ValueError) as e:
                    st.error(f"Scan failed: {e}")
                    return
            added, removed = fresh.keys() - evidence.keys(), evidence.keys() - fresh.keys()
            flash("success", f"Scanned {len(fresh)} repos: {len(added)} new, {len(removed)} gone.")
            st.rerun()


def profile_page() -> None:
    show_flash()
    if missing := missing_profile():
        st.info(
            f"Set up your {' and '.join(missing)} to start: the other pages work once both are saved. "
            "Each change is checked and shown to you before it's saved."
        )
    facts_col, prefs_col = st.columns(2, gap="medium")
    with facts_col:
        profile_card("facts")
    with prefs_col:
        profile_card("preferences")
    github_card()


def needs_profile(page: Callable[[], None]) -> Callable[[], None]:
    """Wrap a page that can't work until the profile is saved."""

    def guarded() -> None:
        if missing := missing_profile():
            st.warning(
                f"Finish setting up your profile first: your {' and '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} missing. Resumes are written only from what you save there."
            )
            st.page_link(PROFILE_PAGE, label="Set up your profile", icon=":material/arrow_forward:")
            return
        page()

    return guarded


RESUMES_PAGE = st.Page(
    needs_profile(resumes_page), title="Resumes", icon=":material/description:", url_path="resumes", default=True
)
JOBS_PAGE = st.Page(needs_profile(jobs_page), title="Jobs", icon=":material/work:", url_path="jobs")
PIPELINE_PAGE = st.Page(needs_profile(pipeline_page), title="Pipeline", icon=":material/settings:", url_path="pipeline")
PROFILE_PAGE = st.Page(profile_page, title="Profile", icon=":material/person:", url_path="profile")

# Less empty space above the content than Streamlit's default.
st.html("<style>[data-testid='stMainBlockContainer'] { padding-top: 4rem; }</style>")
page = st.navigation([RESUMES_PAGE, JOBS_PAGE, PIPELINE_PAGE, PROFILE_PAGE], position="top")
status_line(link=page.title != PIPELINE_PAGE.title)
page.run()
