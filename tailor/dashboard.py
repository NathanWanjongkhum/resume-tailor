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
from functools import partial
from pathlib import Path
from typing import Any

import pandas as pd
import ruamel.yaml
import streamlit as st
import yaml
from code_editor import code_editor
from pandas.io.formats.style import Styler
from streamlit.delta_generator import DeltaGenerator

# Streamlit runs this file as a script, so make the package importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor import (
    control,
    db,
    github,
    letter,
    llm,
    pipeline,
    preview,
    profile,
    profile_edit,
    profile_forms,
    progress,
    tracking,
    triage,
)
from tailor.facts import Facts
from tailor.paths import OUT

Job = dict[str, Any]

STATUS_LABELS = {
    "new": "New",
    "filtered": "Filtered out",
    "triaged": "Scored",
    "generated": "To review",
    "approved": "Approved",
    "paused": "Pending",
    "applied": "Applied",
    "interview": "Interviewing",
    "offer": "Offer",
    "hired": "Hired",
    "not_selected": "Not selected",
    "rejected": "Rejected",
    "error": "Error",
}
STATUS_COLORS = {
    "new": "gray",
    "filtered": "gray",
    "triaged": "blue",
    "generated": "orange",
    "approved": "green",
    "paused": "orange",
    "applied": "violet",
    "interview": "blue",
    "offer": "green",
    "hired": "green",
    "not_selected": "gray",
    "rejected": "gray",
    "error": "red",
}
STATUS_ICONS = {
    "new": "schedule",
    "filtered": "filter_alt_off",
    "triaged": "insights",
    "generated": "rate_review",
    "approved": "thumb_up",
    "paused": "pause_circle",
    "applied": "send",
    "interview": "forum",
    "offer": "handshake",
    "hired": "celebration",
    "not_selected": "do_not_disturb_on",
    "rejected": "block",
    "error": "error",
}
# What each kind of reply in Gmail reads as, on an application's history.
EVENT_LABELS = {
    "received": "Application received",
    "viewed": "Application viewed",
    "interview": "Interview",
    "not_selected": "Not selected",
    "offer": "Offer",
    "hired": "Hired",
    "applied": "Marked applied",
}
APPLICATION_STATUSES = ("applied", "interview", "offer", "hired", "not_selected")
STAGE_LABELS = {
    "ingest": "Read Gmail",
    "track": "Check replies",
    "filter": "Filter",
    "fetch": "Fetch postings",
    "triage": "Score jobs",
    "recheck": "Recheck pending",
    "generate": "Write resumes",
}
RUN_MARKS = {
    "done": ":green[:material/check_circle:]",
    "stopped": ":gray[:material/stop_circle:]",
    "failed": ":red[:material/error:]",
    "interrupted": ":orange[:material/warning:]",
    "running": ":blue[:material/sync:]",
}
# Table cells can't hold markdown, so dataframes signal with cell colors (text, background).
CELL_COLORS = {
    "green": ("#166534", "#dcfce7"),
    "orange": ("#9a3412", "#ffedd5"),
    "red": ("#991b1b", "#fee2e2"),
    "gray": ("#4b5563", "#f3f4f6"),
}
RUN_COLORS = {"done": "green", "stopped": "gray", "failed": "red", "interrupted": "orange", "running": "gray"}
GOOD, OK = 4, 3
ROW_PX = 35
MAX_TABLE_PX = 760
MAX_UNSCROLLED_CARDS = 5
# A letter page fitted to the frame's width, plus the viewer's toolbar.
PDF_ASPECT = "17 / 23"
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


def write_letter(job: Job) -> None:
    with st.spinner(f"Writing a cover letter for {job['company'] or job['title']}…"):
        try:
            with pipeline.exclusive():
                via, effort = generation_settings()
                pipeline.write_letter(db_conn(), job["id"], Facts(), via, effort)
            flash("success", f"Cover letter written for {job['company']}. It's in the Cover letter tab.")
        except pipeline.AlreadyRunningError:
            flash("error", "The pipeline is running; try again when it finishes.")
        except pipeline.JobError as e:
            flash("error", f"Couldn't write the cover letter: {e}")
    st.rerun()


def set_status(job: Job, status: str) -> None:
    tracking.set_status(db_conn(), job["id"], status)
    flash("success", f"{job['company']}: marked {STATUS_LABELS[status].lower()}.")
    st.rerun()


def launch(*, start_stage: str = "ingest", skip_ingest: bool = False) -> None:
    try:
        control.start(start_stage=start_stage, skip_ingest=skip_ingest)
        flash("success", f"Pipeline started from {STAGE_LABELS[start_stage]}.")
    except (OSError, subprocess.CalledProcessError) as e:
        flash("error", f"Couldn't start the pipeline: {e}")
    st.rerun()


# Once per dashboard process: resumes are never rewritten in place, so a copy only goes
# stale when its out/ folder is deleted by hand.
@st.cache_resource(show_spinner=False)
def prune_previews() -> None:
    preview.prune(OUT.glob("*/resume.pdf"))


def pdf_viewer(pdf: Path) -> None:
    """The browser's own PDF viewer, so links, zoom and text search work.

    st.iframe can't be used: it sandboxes the frame, and Chrome refuses to show a PDF in a
    sandboxed frame. The URL is our own static path, never input from a job.

    Both browsers' viewers open a clicked link in the whole tab, and the page can't intercept
    that (the viewer is another origin, so no navigate event fires); middle-click opens a new tab.
    """
    url = preview.publish(pdf)
    st.markdown(
        f'<iframe src="{url}" title="Resume PDF" style="width:100%;aspect-ratio:{PDF_ASPECT};border:0"></iframe>',
        unsafe_allow_html=True,
    )


def report_sections(outdir: Path) -> dict[str, str]:
    """report.md split by its `## ` headings."""
    path = outdir / "report.md"
    parts = re.split(r"^## (.+)$", path.read_text() if path.exists() else "", flags=re.MULTILINE)
    return {parts[i].strip(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def level_color(value: int) -> str:
    """A 1-5 score's color, so a weak score stands out from a strong one."""
    return "green" if value >= GOOD else "orange" if value == OK else "red"


def cell_style(color: str | None) -> str:
    if color is None:
        return ""
    text, background = CELL_COLORS[color]
    return f"color: {text}; background-color: {background}"


def score_badges(job: Job) -> str:
    if job["relevance"] is None:
        return ""
    scores = {"Fit": job["relevance"], "Want": job["desirability"], "Fresh": triage.recency(job["posted_at"])}
    return " ".join(f":{level_color(value)}-badge[{name} {value}/5]" for name, value in scores.items())


def status_badge(status: str) -> str:
    icon = f":material/{STATUS_ICONS[status]}: " if status in STATUS_ICONS else ""
    return f":{STATUS_COLORS.get(status, 'gray')}-badge[{icon}{STATUS_LABELS.get(status, status)}]"


def empty_state(icon: str, color: str, title: str, body: str) -> DeltaGenerator:
    """A centered card for an empty view that says why it's empty and what to do next.

    Returns a centered row for the caller's next-step links.
    """
    with st.container(border=True, horizontal_alignment="center", gap="small"):
        st.markdown(f"## :{color}[:material/{icon}:]", width="content")
        st.markdown(f"**{title}**", width="content")
        st.caption(body, width="content")
        return st.container(horizontal=True, horizontal_alignment="center", gap="medium")


def job_heading(job: Job) -> None:
    st.markdown(f"### {md(job['title'])}")
    pay = f" · {job['salary']}" if job["salary"] else ""
    where = short_location(job["location"]) or "Location not listed"
    st.caption(md(f"{job['company'] or 'Unknown company'} · {where}{pay}"))


@st.fragment(run_every=3)
def status_line() -> None:
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
        mark = RUN_MARKS.get(last[0]["status"], "") if last else ""
        text = f"{mark} :gray[{' · '.join(parts)}]"
    with st.container(
        horizontal=True, vertical_alignment="center", horizontal_alignment="right", gap="medium", key="status-line"
    ):
        st.markdown(text, width="content")


def resume_card(job: Job, *, selected: bool) -> None:
    # The keys give the card CSS classes: the selected one is highlighted, and the others'
    # buttons are stretched invisibly over the whole card so the card itself is clickable.
    with st.container(border=True, key=f"{'picked' if selected else 'card'}-{job['id']}"):
        st.markdown(f"**{md(job['company'] or 'Unknown company')}**  \n{md(job['title'])}")
        failed = " :red-badge[Check failed]" if job["checks_ok"] == 0 else ""
        asks = " :blue-badge[:material/mail: Cover letter]" if letter.asks_for_letter(job["posting"] or "") else ""
        st.markdown(f"{score_badges(job)}{failed}{asks}")
        if not selected:
            st.button(
                f"Open {job['company'] or 'resume'}",
                key=f"open-{job['id']}",
                on_click=lambda: st.session_state.update(selected_resume=job["id"]),
            )


def download_name(job: Job, what: str) -> str:
    last_name = Facts().raw["contact"]["name"].split()[-1]
    return re.sub(r"[^A-Za-z0-9]+", "_", f"{last_name} {what} {job['company'] or ''}").strip("_")


def resume_actions(job: Job, pdf: Path) -> None:
    filename = download_name(job, "Resume")
    with st.container(horizontal=True, gap="small"):
        st.download_button(
            "Download PDF",
            pdf.read_bytes(),
            f"{filename}.pdf",
            "application/pdf",
            type="primary",
            icon=":material/download:",
        )
        if job["status"] not in APPLICATION_STATUSES and st.button("Mark applied", icon=":material/send:"):
            set_status(job, "applied")
        if job["status"] == "generated" and st.button("Approve", icon=":material/check:"):
            set_status(job, "approved")
        if (link := safe_link(job)) is not None:
            st.link_button("Open posting", link, icon=":material/open_in_new:")
        if job["status"] != "rejected" and st.button("Reject", icon=f":material/{STATUS_ICONS['rejected']}:"):
            set_status(job, "rejected")
        with st.popover("More", icon=":material/more_horiz:"):
            st.download_button("Download .tex", (pdf.parent / "resume.tex").read_bytes(), f"{filename}.tex")
            if st.button("Regenerate", help="Write a fresh version (about a minute, uses your plan)"):
                write_resume(job)
            if job["status"] in pipeline.REVIEWED and st.button("Back to review"):
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


# Streamlit has no copy button. This one reads the text from the page rather than having it
# pasted into the script, so nothing from a job ends up in JavaScript. Styled like st.button.
COPY_LETTER = """<button id="letter-copy" type="button" style="font:inherit;color:#1f2937;background:#fff;
border:1px solid #e5e7eb;border-radius:0.5rem;min-height:2.5rem;padding:0.25rem 0.75rem;cursor:pointer">Copy</button>
<script>
document.getElementById("letter-copy").onclick = async (event) => {
  const text = document.querySelector(".st-key-letter-text code").innerText;
  await navigator.clipboard.writeText(text);
  event.target.textContent = "Copied";
  setTimeout(() => { event.target.textContent = "Copy"; }, 1500);
};
</script>"""


def cover_letter_tab(job: Job, outdir: Path) -> None:
    asks = letter.asks_for_letter(job["posting"] or "")
    saved = letter.load(outdir)
    busy = pipeline.is_running()
    if saved is None:
        st.markdown("The posting asks for a cover letter." if asks else "The posting doesn't mention a cover letter.")
        if st.button(
            "Write cover letter",
            type="primary" if asks else "secondary",
            icon=":material/edit_note:",
            disabled=busy,
            help="Under a minute; uses your Claude plan",
        ):
            write_letter(job)
        return
    for error in saved["errors"]:
        st.error(md(error))
    for warning in saved["warnings"]:
        st.warning(md(warning))
    text = (outdir / f"{letter.NAME}.txt").read_text()
    with st.container(horizontal=True, gap="small"):
        st.download_button(
            "Download .txt",
            text,
            f"{download_name(job, 'Cover Letter')}.txt",
            "text/plain",
            type="primary",
            icon=":material/download:",
            key="letter-txt",
        )
        st.html(COPY_LETTER, width="content", unsafe_allow_javascript=True)
        if st.button("Rewrite", disabled=busy, help="Write a fresh version (uses your plan)", key="letter-rewrite"):
            write_letter(job)
    with st.container(key="letter-text"):
        st.code(text, language=None, wrap_lines=True)


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
    resume_tab, cover, why, checks, posting = st.tabs(
        ["Resume", "Cover letter", "Why it fits", "Checks & changes", "Posting"]
    )
    with resume_tab:
        pdf_viewer(pdf)
    with cover:
        cover_letter_tab(job, outdir)
    with why:
        why_tab(outdir)
    with checks:
        checks_tab(outdir)
    with posting:
        st.text(job["posting"] or job["summary"] or "(none)")


def next_run_note() -> str:
    timer = control.timer_state()
    if not timer.enabled:
        return "The daily run is off, so new resumes come only from runs you start."
    cap = triage.load_prefs()["generate"]["max_per_run"]
    return f"The next run, {systemd_time(timer.next_run)}, writes up to {cap} for the best new jobs."


def caught_up(all_scored: list[Job], *, any_resumes: bool) -> None:
    """The Resumes page with nothing waiting for review."""
    waiting = [j for j in all_scored if j["status"] == "triaged"]
    if any_resumes:
        title, icon, color = "All caught up", "task_alt", "green"
        body = "No resumes are waiting for review."
    else:
        title, icon, color = "No resumes yet", "description", "blue"
        body = "Resumes appear here once the pipeline writes them."
    if waiting:
        body += f" {len(waiting)} scored jobs don't have one yet."
    with empty_state(icon, color, title, f"{body} {next_run_note()}"):
        if waiting:
            st.page_link(JOBS_PAGE, label="Pick a scored job", icon=":material/work:", width="content")
        st.page_link(PIPELINE_PAGE, label="Run the pipeline", icon=":material/play_arrow:", width="content")


def resumes_page() -> None:
    show_flash()
    prune_previews()
    everything = all_jobs()
    jobs = [j for j in everything if j["outdir"]]
    groups = {
        "To review": [j for j in jobs if j["status"] == "generated"],
        "Approved": [j for j in jobs if j["status"] == "approved"],
        "Pending": [j for j in jobs if j["status"] == "paused"],
        "Applied": [j for j in jobs if j["status"] in APPLICATION_STATUSES],
        "Rejected": [j for j in jobs if j["status"] == "rejected"],
    }
    options = [name for name, items in groups.items() if items or name == "To review"]
    if len(options) > 1:
        group = st.segmented_control(
            "Show",
            options,
            default="To review",
            required=True,
            wrap=True,
            format_func=lambda g: f"{g} {len(groups[g])}",
            label_visibility="collapsed",
        )
    else:
        group = "To review"
    shown = sorted(groups[group or "To review"], key=score, reverse=True)
    if not shown:
        caught_up(everything, any_resumes=bool(jobs))
        return
    if st.session_state.get("selected_resume") not in {j["id"] for j in shown}:
        st.session_state["selected_resume"] = shown[0]["id"]
    left, right = st.columns([1, 2.4], gap="large")
    with left, st.container(height=780 if len(shown) > MAX_UNSCROLLED_CARDS else "content", border=False):
        for job in shown:
            resume_card(job, selected=job["id"] == st.session_state["selected_resume"])
    with right:
        resume_detail(next(j for j in shown if j["id"] == st.session_state["selected_resume"]))


def job_notes(job: Job) -> None:
    email_only = "" if job["posting_ok"] else " :orange-badge[:material/mail: Email summary only]"
    st.markdown(f"{status_badge(job['status'])} {score_badges(job)}{email_only}")
    if job["triage"]:
        t = json.loads(job["triage"])
        st.markdown(md(t["reasons"]))
        if t.get("red_flags"):
            st.markdown("\n".join(f"- :orange[{md(flag)}]" for flag in t["red_flags"]))
    if job["filter_reason"]:
        st.caption(md(f"Filtered out: {job['filter_reason']}"))
    if job["error"]:
        st.error(job["error"])


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
            st.link_button("Open posting", link, icon=":material/open_in_new:")
        if job["status"] not in {"rejected", "filtered"} and st.button("Not interested", icon=":material/block:"):
            set_status(job, "rejected")
    with st.expander("Posting text"):
        st.text(job["posting"] or job["summary"] or "(none)")


JOB_VIEWS = {
    "Scored": lambda j: j["status"] == "triaged",
    "Has resume": lambda j: bool(j["outdir"]),
    "Pending": lambda j: j["status"] == "paused",
    "Filtered out": lambda j: j["status"] == "filtered",
    "All": lambda _j: True,
}


def jobs_page() -> None:
    show_flash()
    with st.container(horizontal=True, vertical_alignment="bottom", gap="medium"):
        view = (
            st.segmented_control(
                "Show", list(JOB_VIEWS), default="Scored", required=True, wrap=True, label_visibility="collapsed"
            )
            or "Scored"
        )
        query = st.text_input("Search", placeholder="Search company or role", label_visibility="collapsed")
        min_score = st.slider(
            "Minimum fit + want + fresh",
            3,
            15,
            9,
            format="Fit + want + fresh ≥ %d",
            width=240,
            disabled=view == "Filtered out",
            label_visibility="collapsed",
        )
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
        hint = "Lower the minimum score, clear the search, or change the view."
        with empty_state("search_off", "gray", "No jobs match", hint):
            st.page_link(PIPELINE_PAGE, label="Find new jobs", icon=":material/play_arrow:", width="content")
        return
    left, right = st.columns([2, 1], gap="large")
    with left:
        picked = st.dataframe(
            jobs_table(shown, with_status=view == "All"),
            hide_index=True,
            height=min(MAX_TABLE_PX, ROW_PX * (len(shown) + 1) + 3),
            on_select="rerun",
            selection_mode="single-row",
            column_config={
                "Fit": st.column_config.NumberColumn(width=48, help="How well you match the role, 1-5"),
                "Want": st.column_config.NumberColumn(width=56, help="How much it matches your wants, 1-5"),
                "Fresh": st.column_config.NumberColumn(width=56, help="How recently it was posted, 1-5"),
                "Company": st.column_config.TextColumn(width=140),
                "Role": st.column_config.TextColumn(width=260),
                "Location": st.column_config.TextColumn(width=130),
                "Posting": st.column_config.TextColumn(width=96, help="Email only: the full posting wasn't fetched"),
            },
        )
    with right:
        rows = picked.selection.rows
        if rows:
            job_detail(shown[rows[0]])
        else:
            body = "Select a row to see why it scored that way, or to write a resume for it."
            empty_state("left_click", "blue", f"{len(shown)} jobs", body)


US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
}  # fmt: skip


def short_location(location: str | None) -> str:
    """LinkedIn spells out the country and state on nearly every job; shorten them so the city fits."""
    if location is None:
        return ""
    location = re.sub(r",\s*United States$", "", location).replace("United States", "US")
    city, _, state = location.rpartition(", ")
    return f"{city}, {US_STATES[state]}" if city and state in US_STATES else location


def jobs_table(jobs: list[Job], *, with_status: bool) -> Styler:
    """The jobs as a styled table: scores colored by level, so weak ones stand out."""
    table = pd.DataFrame(
        [
            {
                "Fit": j["relevance"],
                "Want": j["desirability"],
                "Fresh": triage.recency(j["posted_at"]),
                "Company": j["company"],
                "Role": j["title"],
                "Location": short_location(j["location"]),
                **({"Status": STATUS_LABELS.get(j["status"], j["status"])} if with_status else {}),
                "Posting": "" if j["posting_ok"] else "Email only",
            }
            for j in jobs
        ]
    ).astype({"Fit": "Int64", "Want": "Int64", "Fresh": "Int64"})
    scores = ["Fit", "Want", "Fresh"]
    styled = table.style.map(lambda v: cell_style(None if pd.isna(v) else level_color(int(v))), subset=scores)
    styled = styled.map(lambda v: cell_style("orange" if v else None), subset=["Posting"])
    styled.format(na_rep="", subset=scores)
    return styled


def stage_strip(run: Job) -> None:
    args = run.get("args") or ""
    start = args.removeprefix("start=").split(" ")[0] or "ingest"
    first = progress.STAGES.index(start) if start in progress.STAGES else 0
    current = progress.STAGES.index(run["stage"]) if run["stage"] in progress.STAGES else -1
    seconds = json.loads(run["stage_seconds"] or "{}")
    for col, (i, name) in zip(st.columns(len(progress.STAGES)), enumerate(progress.STAGES), strict=True):
        with col, st.container(border=True):
            if i < first or (name in pipeline.GMAIL_STAGES and "skip_ingest" in args):
                st.caption(f":material/skip_next: {STAGE_LABELS[name]}  \nskipped")
            elif i < current or (i == current and run["status"] == "done"):
                st.caption(f":green[:material/check_circle:] {STAGE_LABELS[name]}  \n{duration(seconds.get(name, 0))}")
            elif i == current:
                state = "in progress" if run["status"] == "running" else run["status"]
                st.markdown(f"{RUN_MARKS.get(run['status'], '')} **{STAGE_LABELS[name]}**  \n:gray[{state}]")
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
            st.caption("Reads new alert emails, fetches and scores the jobs, then writes resumes for the best ones.")
            idle_controls()


def schedule_card() -> None:
    with st.container(border=True, height="stretch"):
        timer = control.timer_state()
        on = st.toggle("Run every day at 8:00 AM", value=timer.enabled)
        if on != timer.enabled:
            flash("info", control.set_timer(on=on))
            st.rerun()
        prefs = triage.load_prefs()
        next_run = systemd_time(timer.next_run) if timer.enabled else ":orange[:material/pause_circle: off]"
        st.caption(
            f"Next: {next_run} · "
            f"up to {prefs['generate']['max_per_run']} resumes per run · "
            f"effort: {prefs['triage']['effort']} scoring, {prefs['generate'].get('effort', 'high')} writing"
        )


def failed_card() -> None:
    conn = db_conn()
    with st.container(border=True, height="stretch"):
        errored = db.jobs(conn, "error")
        capped = conn.execute("SELECT COUNT(*) FROM jobs WHERE attempts >= ?", (db.MAX_ATTEMPTS,)).fetchone()[0]
        if not errored and not capped:
            st.caption(":green[:material/check_circle: No failed jobs.] Jobs that fail are retried on the next run.")
            return
        st.caption(
            f":red[:material/error: {len(errored)} failed jobs waiting for retry] · "
            f"{capped} skipped after {db.MAX_ATTEMPTS} failed attempts"
        )
        if st.button("Retry all now", icon=":material/replay:"):
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
    history = [dict(r) for r in progress.runs(db_conn(), limit=20)]
    if not history:
        st.caption("No runs yet. Runs started here or by the daily timer are listed here.")
        return
    table = pd.DataFrame(
        [
            {
                "Run": r["id"],
                "Result": r["status"],
                "Started": local_time(r["started_at"]),
                "Took": duration(ago(r["started_at"]) - ago(r["finished_at"])) if r["finished_at"] else "",
                "By": r["trigger"],
                "Done": ", ".join(f"{k} {v}" for k, v in json.loads(r["stats"] or "{}").items()),
                "Note": run_note(r),
            }
            for r in history
        ]
    )
    st.dataframe(
        table.style.map(lambda v: cell_style(RUN_COLORS.get(v)), subset=["Result"]),
        hide_index=True,
        width="stretch",
        column_config={
            "Run": st.column_config.NumberColumn(width=56),
            "Result": st.column_config.TextColumn(width=104),
            "Started": st.column_config.TextColumn(width=120),
            "Took": st.column_config.TextColumn(width=80),
            "By": st.column_config.TextColumn(width=96),
            "Done": st.column_config.TextColumn(width="large"),
            "Note": st.column_config.TextColumn(width="large"),
        },
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


# Ace options: YAML wants two-space soft tabs; wrapping keeps long regexes and queries readable;
# line numbers match the "Line N" in problem messages.
EDITOR_OPTIONS = {
    "tabSize": 2,
    "useSoftTabs": True,
    "showPrintMargin": False,
    "wrap": True,
    "showLineNumbers": True,
    "showFoldWidgets": True,
}


def bump(key: str) -> None:
    st.session_state[key] = st.session_state.get(key, 0) + 1


def load_draft(kind: str, text: str, *, reshape: bool = True) -> None:
    """Point the YAML editors (inline and expanded) and the form at `text`.

    The editor only reads its starting text when it mounts, so bumping the version
    remounts it with the new text. The form's widgets restart from the draft unless
    the change came from one of them (`reshape=False`), which keeps the focus there.
    """
    st.session_state[f"draft-{kind}"] = st.session_state[f"base-{kind}"] = text
    bump(f"draft-version-{kind}")
    if reshape:
        bump(f"form-version-{kind}")


def sync_draft(kind: str) -> None:
    load_draft(kind, st.session_state[f"draft-{kind}"])


def yaml_problems(kind: str, text: str) -> list[str]:
    """What would stop `text` from being saved, with line numbers where the parser has them."""
    if not text.strip():
        return []
    try:
        profile.check(kind, text)
    except ValueError as e:
        err = e.__cause__
        if not isinstance(err, yaml.MarkedYAMLError) or err.problem_mark is None:
            return [str(e)]
        problem = f"Line {err.problem_mark.line + 1}: {err.problem}"
        # For an unclosed bracket or quote the parser gives up lines later, so say where it opened.
        if err.context_mark is not None and err.context_mark.line != err.problem_mark.line:
            problem += f" ({err.context} that starts on line {err.context_mark.line + 1})"
        return [problem]
    return []


def yaml_editor(kind: str, *, where: str, height: str) -> list[str]:
    """A YAML editor over this kind's draft: highlighting, folding, auto-indent, find and replace.

    Ace's YAML mode marks syntax errors in the gutter as you type; below the editor go the
    problems `profile.check` finds, which also cover missing fields. Returns those problems.
    """
    key = f"ace-{kind}-{where}-{st.session_state.get(f'draft-version-{kind}', 0)}"
    response = code_editor(
        st.session_state[f"base-{kind}"],
        lang="yaml",
        height=height,
        response_mode=["debounce", "blur"],
        options=EDITOR_OPTIONS,
        # Word completion would turn Enter at the end of a word into an unwanted completion, and the
        # component's default top scroll margin leaves the gutter a line out of step with the text.
        props={
            "placeholder": f"Paste or write your {kind} here",
            "scrollMargin": [0, 0, 0, 0],
            "enableLiveAutocompletion": False,
            "enableSnippets": False,
        },
        key=key,
    )
    # The component keeps returning its last event, so only a new event id is an edit.
    if response["id"] and response["id"] != st.session_state.get(f"seen-{key}"):
        st.session_state[f"seen-{key}"] = response["id"]
        st.session_state[f"draft-{kind}"] = response["text"]
        bump(f"form-version-{kind}")
    problems = yaml_problems(kind, st.session_state[f"draft-{kind}"])
    for problem in problems:
        st.caption(f":red[:material/error:] {md(problem).replace('[', '\\[').replace(']', '\\]')}")
    return problems


def expanded_editor(kind: str) -> None:
    yaml_editor(kind, where="expanded", height="640px")
    if st.button("Done", key=f"expanded-done-{kind}", type="primary"):
        sync_draft(kind)
        st.rerun()


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


def start_from_example(kind: str) -> None:
    st.button(
        "Start from the example",
        key=f"example-{kind}",
        icon=":material/content_copy:",
        on_click=load_draft,
        args=(kind, (profile.EXAMPLES / f"{kind}.yaml").read_text()),
    )


def yaml_mode(kind: str, current: str | None) -> None:
    if current is None:
        start_from_example(kind)
    yaml_editor(kind, where="inline", height="420px")
    if st.button("Expand", key=f"expand-{kind}", icon=":material/open_in_full:"):
        sync_draft(kind)  # start the expanded editor from the latest edits
        # Closing with the X keeps the edits too: sync the inline editor either way.
        dialog = st.dialog(f"Edit {kind}", width="large", on_dismiss=partial(sync_draft, kind))
        dialog(expanded_editor)(kind)


def form_edit(kind: str, edit: profile_forms.Edit, reshape: bool) -> None:  # noqa: FBT001
    """Apply one form edit to the draft."""
    try:
        doc = profile_edit.parse(st.session_state[f"draft-{kind}"])
        edit(doc)
        text = profile_edit.dump(doc)
    except (ValueError, KeyError, IndexError) as e:
        flash("error", f"Couldn't make that change: {e}")
        return
    load_draft(kind, text, reshape=reshape)


def form_mode(kind: str) -> None:
    draft = st.session_state[f"draft-{kind}"]
    if not draft.strip():
        st.caption(f"No {kind} yet. Start from the example and replace its contents with yours, or upload a file.")
        start_from_example(kind)
        return
    try:
        doc = profile_edit.parse(draft)
    except (yaml.YAMLError, ruamel.yaml.YAMLError, TypeError):
        st.warning("The draft isn't valid YAML, so it can't be shown as a form. Fix it under **YAML**.")
        return
    form = profile_forms.Form(kind, st.session_state.get(f"form-version-{kind}", 0), partial(form_edit, kind))
    conn = db_conn()
    if kind == "facts":
        evidence = profile.load("github", conn)
        github_skills = sorted({s for info in evidence.values() for s in info["skills"]}, key=str.lower)
        profile_forms.facts_form(form, doc, github_skills)
    else:
        stored = profile.load("preferences", conn) if profile.get(conn, "preferences") is not None else None
        profile_forms.preferences_form(form, doc, stored, all_jobs())


def draft_bar(kind: str, current: str | None) -> None:
    """Unsaved edits, from the form or the YAML editor, and the way to save them."""
    draft = st.session_state[f"draft-{kind}"]
    if not draft.strip() or draft == current:
        return
    problems = yaml_problems(kind, draft)
    with st.container(border=True, horizontal=True, vertical_alignment="center", gap="small"):
        if problems:
            st.markdown(f":red[:material/error:] **Unsaved changes can't be saved yet:** {md(problems[0])}")
        else:
            n = len(profile.changes(kind, current, draft)) if current is not None else 0
            st.markdown(f":orange[:material/edit:] **Unsaved changes**{f' ({n})' if n else ''}")
        st.space("stretch")
        if st.button("Review and save", key=f"review-edit-{kind}", type="primary", disabled=bool(problems)):
            st.session_state[f"review-{kind}"] = draft
            st.rerun()
        st.button("Discard", key=f"discard-draft-{kind}", on_click=load_draft, args=(kind, current or ""))


def upload_profile(kind: str, updated: str | None) -> None:
    upload = st.file_uploader(
        f"Upload {kind}.yaml", type=["yaml", "yml"], key=f"upload-{kind}-{updated}", label_visibility="collapsed"
    )
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
        saved = ":green-badge[:material/check: Saved]" if current is not None else ":orange-badge[Missing]"
        st.markdown(f"**{kind.capitalize()}** {saved}")
        st.caption(f"{PROFILE_HELP[kind]} Last saved: {upload_date(updated)}.")
        if current is None:
            st.warning(
                f"No {kind} yet. Upload your file, or start from the example and replace its contents with yours."
            )
        if st.session_state.get(f"draft-of-{kind}", "unset") != updated:
            # First visit, or a save: start the editors from the stored copy.
            st.session_state[f"draft-of-{kind}"] = updated
            load_draft(kind, current or "")
        if (draft := st.session_state.get(f"review-{kind}")) is not None:
            profile_review(kind, current, draft)
            return
        draft_bar(kind, current)
        older = profile.history(conn, kind)
        modes = ["Form", "YAML", "Upload", *(["History"] if older else [])]
        mode = st.segmented_control(
            "Change",
            modes,
            default="Form",
            key=f"mode-{kind}",
            label_visibility="collapsed",
        )
        if mode == "Form":
            form_mode(kind)
        elif mode == "YAML":
            yaml_mode(kind, current)
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
    facts, prefs, evidence = st.tabs(["Facts", "Preferences", "GitHub"], key="profile-tabs")
    with facts:
        profile_card("facts")
    with prefs:
        profile_card("preferences")
    with evidence:
        github_card()


def day(iso: str | None) -> str:
    return "" if iso is None else datetime.fromisoformat(iso).astimezone().strftime("%b %-d")


def application_history(job: Job) -> None:
    events = db.events(db_conn(), job["id"])
    if not events:
        st.caption("No replies yet. Gmail is checked for emails naming the company on every run.")
    for e in events:
        how = ":material/mail:" if e["source"] == "email" else ":material/edit:"
        st.markdown(f"{how} **{EVENT_LABELS.get(e['kind'], e['kind'])}** :gray[· {day(e['at'])}]")
        if e["subject"]:
            st.caption(md(f"{e['sender']}: {e['subject']}"))
        if e["evidence"] and e["evidence"] != e["subject"]:
            st.caption(md(f"“{e['evidence']}”"))


def application_detail(job: Job) -> None:
    job_heading(job)
    applied = f" :gray[applied {day(job['applied_at'])}]" if job["applied_at"] else ""
    st.markdown(f"{status_badge(job['status'])}{applied}")
    with st.container(horizontal=True, gap="small"):
        if job["outdir"] and st.button("Resume", icon=":material/description:", key=f"resume-{job['id']}"):
            st.session_state["selected_resume"] = job["id"]
            st.switch_page(RESUMES_PAGE)
        if (link := safe_link(job)) is not None:
            st.link_button("Open posting", link, icon=":material/open_in_new:")
        with st.popover("Set status", icon=":material/edit:"):
            st.caption("Replies only move an application forward. Fix a misread one here.")
            for status in APPLICATION_STATUSES:
                if status != job["status"] and st.button(
                    STATUS_LABELS[status], icon=f":material/{STATUS_ICONS[status]}:", key=f"set-{status}-{job['id']}"
                ):
                    set_status(job, status)
    st.markdown("**History**")
    application_history(job)


def pending_detail(job: Job) -> None:
    job_heading(job)
    was = STATUS_LABELS.get(job["paused_from"] or "new", job["paused_from"])
    days = triage.load_prefs().get("tracking", {}).get("pending_days", pipeline.PENDING_DAYS)
    st.markdown(
        f"{status_badge('paused')} :gray[not taking applications since {day(job['paused_at'])}; was {was.lower()}]"
    )
    st.caption(
        f"The posting is still up but closed to applications. Each run checks it again: if it reopens, "
        f"it goes back to {was.lower()}; if it's removed, or still paused after {days} days, it's filtered out."
    )
    with st.container(horizontal=True, gap="small"):
        if st.button("Check now", icon=":material/refresh:", key=f"recheck-{job['id']}"):
            with st.spinner("Checking the posting…"):
                state = pipeline.recheck_one(db_conn(), db.get_required(db_conn(), job["id"]), int(days))
            outcome = {"open": "it's taking applications again", "closed": "it's been removed"}
            flash("info", f"{job['company']}: {outcome.get(state or '', 'still not taking applications')}.")
            st.rerun()
        if (link := safe_link(job)) is not None:
            st.link_button("Open posting", link, icon=":material/open_in_new:")
        if st.button("Stop watching", icon=":material/visibility_off:", key=f"unwatch-{job['id']}"):
            db.update(db_conn(), job["id"], status="filtered", filter_reason="stopped watching while paused")
            flash("success", f"{job['company']}: no longer watched.")
            st.rerun()


def unmatched_replies(events: list[sqlite3.Row], jobs: list[Job]) -> None:
    st.caption(
        "Replies that sounded like news about an application, but didn't name a company you're tracking. "
        "Attach each to its job, or dismiss it."
    )
    choices = {j["id"]: f"{j['company']}: {j['title']}" for j in jobs if j["status"] in tracking.TRACKED}
    for e in events:
        with st.container(border=True):
            st.markdown(f"**{EVENT_LABELS.get(e['kind'], e['kind'])}** :gray[· {day(e['at'])}]")
            st.caption(md(f"{e['sender']}: {e['subject']}"))
            if e["evidence"]:
                st.caption(md(f"“{e['evidence']}”"))
            with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
                job_id = st.selectbox(
                    "Job",
                    list(choices),
                    format_func=lambda i: md(choices[i]),
                    index=None,
                    placeholder="Which job is it about?",
                    key=f"assign-{e['id']}",
                    label_visibility="collapsed",
                )
                if st.button("Attach", key=f"attach-{e['id']}", type="primary", disabled=job_id is None):
                    tracking.assign(db_conn(), e["id"], str(job_id))
                    flash("success", f"Attached to {choices[str(job_id)]}.")
                    st.rerun()
                if st.button("Dismiss", key=f"dismiss-{e['id']}"):
                    tracking.dismiss(db_conn(), e["id"])
                    st.rerun()


def latest(event: sqlite3.Row | None) -> str:
    return "" if event is None else f"{EVENT_LABELS.get(event['kind'], event['kind'])}, {day(event['at'])}"


def applications_table(jobs: list[Job]) -> pd.DataFrame:
    sql = "SELECT job_id, kind, max(at) AS at FROM events WHERE job_id IS NOT NULL AND kind != 'other' GROUP BY job_id"
    last = {r["job_id"]: r for r in db_conn().execute(sql)}
    # Jobs marked applied before applied_at existed: their first reply is the best date there is.
    sql = "SELECT job_id, min(at) AS at FROM events WHERE job_id IS NOT NULL GROUP BY job_id"
    first = {r["job_id"]: r["at"] for r in db_conn().execute(sql)}
    return pd.DataFrame(
        [
            {
                "Company": j["company"],
                "Role": j["title"],
                "Status": STATUS_LABELS.get(j["status"], j["status"]),
                "Applied": day(j["applied_at"] or first.get(j["id"])),
                "Since": day(j["paused_at"]),
                "Latest": latest(last.get(j["id"])),
            }
            for j in jobs
        ]
    )


APPLICATION_VIEWS: dict[str, tuple[str, ...]] = {
    "Active": ("applied", "interview", "offer"),
    "Pending postings": ("paused",),
    "Closed": ("hired", "not_selected"),
}


def applications_page() -> None:
    show_flash()
    everything = all_jobs()
    unmatched = tracking.unmatched(db_conn())
    groups = {name: [j for j in everything if j["status"] in statuses] for name, statuses in APPLICATION_VIEWS.items()}
    counts = [f"{STATUS_LABELS[s]} **{sum(j['status'] == s for j in everything)}**" for s in APPLICATION_STATUSES]
    st.markdown(f":gray[{' · '.join(counts)}]")
    options = [*groups, *(["Unmatched replies"] if unmatched else [])]
    view = (
        st.segmented_control(
            "Show",
            options,
            default="Active",
            required=True,
            format_func=lambda g: f"{g} {len(groups[g]) if g in groups else len(unmatched)}",
            label_visibility="collapsed",
        )
        or "Active"
    )
    if view == "Unmatched replies":
        unmatched_replies(unmatched, everything)
        return
    shown = sorted(groups[view], key=lambda j: str(j["applied_at"] or j["paused_at"] or j["updated_at"]), reverse=True)
    if not shown:
        bodies = {
            "Active": "Mark a resume applied, or let a confirmation email do it; replies move it on from there.",
            "Pending postings": "Postings that are up but not taking applications land here and are rechecked.",
            "Closed": "Applications that ended in a hire or a no show up here.",
        }
        empty_state("inbox", "gray", f"Nothing {view.lower()}", bodies[view])
        return
    left, right = st.columns([2, 1], gap="large")
    with left:
        hidden = ["Applied", "Latest"] if view == "Pending postings" else ["Since"]
        picked = st.dataframe(
            applications_table(shown).drop(columns=hidden),
            hide_index=True,
            height=min(MAX_TABLE_PX, ROW_PX * (len(shown) + 1) + 3),
            on_select="rerun",
            selection_mode="single-row",
            key=f"applications-{view}",
        )
    with right:
        if not (rows := picked.selection.rows):
            empty_state("left_click", "blue", f"{len(shown)} jobs", "Select a row to see its history.")
        elif view == "Pending postings":
            pending_detail(shown[rows[0]])
        else:
            application_detail(shown[rows[0]])


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
APPLICATIONS_PAGE = st.Page(
    needs_profile(applications_page), title="Applications", icon=":material/send:", url_path="applications"
)
PIPELINE_PAGE = st.Page(needs_profile(pipeline_page), title="Pipeline", icon=":material/settings:", url_path="pipeline")
PROFILE_PAGE = st.Page(profile_page, title="Profile", icon=":material/person:", url_path="profile")

# Less empty space above the content than Streamlit's default, the status line up in the
# navigation bar when there's room beside the links, and a highlight for the
# selected resume card (its container key starts with "picked-").
PAGE_CSS = """<style>
[data-testid='stMainBlockContainer'] { padding-top: 4rem; }
@media (min-width: 1000px) {
  [class*='st-key-status-line'] { position: fixed; top: 1.05rem; right: 1.5rem; width: auto; z-index: 999991; }
}
[class*='st-key-picked-'] { border-color: #2563eb; background-color: #eff6ff; box-shadow: inset 3px 0 0 #2563eb; }
[class*='st-key-card-'] { position: relative; transition: border-color 0.1s, background-color 0.1s; }
[class*='st-key-card-']:hover { border-color: #93c5fd; background-color: #f8fafc; }
[class*='st-key-card-']:has(button:focus-visible) { outline: 2px solid #2563eb; outline-offset: 2px; }
[class*='st-key-open-'] { position: absolute; inset: 0; z-index: 1; width: auto; margin: 0; }
[class*='st-key-open-'] .stButton, [class*='st-key-open-'] button { width: 100%; height: 100%; }
[class*='st-key-open-'] button { opacity: 0; cursor: pointer; }
</style>"""
st.html(PAGE_CSS)
page = st.navigation([RESUMES_PAGE, JOBS_PAGE, APPLICATIONS_PAGE, PIPELINE_PAGE, PROFILE_PAGE], position="top")
status_line()
page.run()
