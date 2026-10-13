import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tailor import db, pipeline, progress, sources, triage
from tailor.facts import Facts
from tailor.gmail import _date
from tailor.schema import Bullet, ExperiencePick, ProjectPick, SkillLine, Tailored
from tailor.sources import _ld_name, _ld_place, http_get, parse_hiringcafe, parse_linkedin, parser_for, strip_tracking
from tailor.triage import hard_filter, load_prefs
from tailor.validate import validate

FIX = Path(__file__).parent / "fixtures"


def test_linkedin_digest() -> None:
    leads = parse_linkedin((FIX / "linkedin_digest.txt").read_text())
    assert [lead["id"] for lead in leads] == ["linkedin:4475264262", "linkedin:4475261276", "linkedin:4477610158"]
    assert leads[0]["company"] == "TalentHop"


def test_hiringcafe_digest() -> None:
    leads = parse_hiringcafe((FIX / "hiringcafe_digest.txt").read_text())
    assert [lead["company"] for lead in leads] == ["Milsoft Utility Solutions", "CareTria", None]
    assert leads[1]["salary"] == "$60,000 – $90,000 / year"
    assert leads[0]["posted_at"] == "2026-10-06T00:00:00+00:00"


def test_parser_routing() -> None:
    assert parser_for("LinkedIn <jobalerts-noreply@linkedin.com>") is parse_linkedin
    assert parser_for("someone@example.com") is None


def test_hard_filters() -> None:
    prefs = load_prefs()
    assert hard_filter({"title": "Senior Software Engineer", "location": "Remote"}, prefs) is not None
    assert hard_filter({"title": "Mid-Level Full Stack Developer", "location": "United States"}, prefs) is not None
    assert hard_filter({"title": "Junior Developer", "location": "Abilene, Texas, United States"}, prefs) is not None
    assert hard_filter({"title": "Software Engineer, New Grad", "location": "United States"}, prefs) is None
    assert hard_filter({"title": "Software Engineer I", "location": "Portland, OR"}, prefs) is None


def test_validator_catches_embellishment() -> None:
    def bullet(claim_id: str, text: str) -> Bullet:
        return Bullet(claim_id=claim_id, text=text)

    t = Tailored(
        fit_summary="",
        matched_requirements=[],
        gaps=[],
        experience=[
            ExperiencePick(
                experience_id="northwind",
                bullets=[
                    bullet(
                        "nw-platform",
                        "Built a full-stack React and Firebase marketing platform for clients and contractors.",
                    ),
                    bullet(
                        "nw-messaging",
                        "Shipped real-time chat on Cloud Firestore for 10,000 users with Redis caching.",
                    ),
                ],
            )
        ],
        projects=[
            ProjectPick(
                project_id="img-pipeline",
                reason="",
                stack=["Java", "Kubernetes"],
                bullets=[
                    bullet("ip-bench", "Benchmarked Lambda versions via CloudWatch, cutting cost 40%."),
                ],
            )
        ],
        skills=[SkillLine(category="Tools", items=["Docker", "Terraform"])],
    )
    errors = " | ".join(validate(t, Facts()).errors)
    for expected in ["10,000", "Redis", "Kubernetes", "40%", "Terraform"]:
        assert expected in errors
    assert "nw-platform" not in errors


def test_linkedin_single_alert_badges() -> None:
    text = """Your job alert for Data Analyst in Seattle
Manage alerts: https://www.linkedin.com/comm/jobs/alerts?x=1
Data Analyst, MFN RCX
Amazon
Bellevue, WA
This company is actively hiring
View job: https://www.linkedin.com/comm/jobs/view/4460136400/?trk=a
---------------------------------------------------------
Energy Resource Planning Acquisition Analyst - Anaplan
Puget Sound Energy
Bellevue, WA
14 school alumni
$115K-$130K / year
View job: https://www.linkedin.com/comm/jobs/view/4475144900/?trk=b
---------------------------------------------------------
See all jobs on LinkedIn:  https://www.linkedin.com/comm/jobs/search-results/?k=1
New jobs from your other alerts
<strong class="font-bold">Full-stack Developer</strong> jobs in Portland
Full Stack Developer
The Cadmus Group
United States
View job: https://www.linkedin.com/comm/jobs/view/4477867700/?trk=c
"""
    leads = {lead["id"]: lead for lead in parse_linkedin(text)}
    assert leads["linkedin:4460136400"]["company"] == "Amazon"
    assert leads["linkedin:4475144900"]["title"].startswith("Energy Resource")
    assert leads["linkedin:4475144900"]["salary"] == "$115K-$130K / year"
    assert leads["linkedin:4477867700"] == {
        **leads["linkedin:4477867700"],
        "title": "Full Stack Developer",
        "company": "The Cadmus Group",
        "location": "United States",
    }


def test_hiringcafe_header_forms() -> None:
    text = (
        "  • Data Analyst — Acme — Remote\n    Posted Oct 1\n    Summary one.\n    Apply: https://x.test/1\n\n"
        "  • BI Analyst — United States\n    Posted Oct 2\n    Summary two.\n    Apply: https://x.test/2\n\n"
        "  • Lone Title\n    Posted Oct 3\n    Summary three.\n    Apply: https://x.test/3\n"
    )
    got = [(lead["title"], lead["company"], lead["location"]) for lead in parse_hiringcafe(text)]
    assert got == [
        ("Data Analyst", "Acme", "Remote"),
        ("BI Analyst", None, "United States"),
        ("Lone Title", None, None),
    ]


def test_strip_tracking_keeps_other_params() -> None:
    url = "https://jobs.test/view?utm_source=mail&id=42&utm_term=x#top"
    assert strip_tracking(url) == "https://jobs.test/view?id=42#top"


def test_http_get_refuses_non_http() -> None:
    with pytest.raises(ValueError, match="non-http"):
        http_get("file:///etc/passwd")


def test_db_rejects_unknown_columns(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    assert db.add_job(conn, id="t:1", source="test", title="Engineer", company="Acme")
    assert not db.add_job(conn, id="t:2", source="test", title="Engineer", company="ACME")  # dedupe_key
    with pytest.raises(ValueError, match="unknown job columns"):
        db.update(conn, "t:1", **{"status = 'x' --": 1})


def test_validator_allows_stack_numbers_and_prose() -> None:
    t = Tailored(
        fit_summary="",
        matched_requirements=[],
        gaps=[],
        experience=[],
        projects=[
            ProjectPick(
                project_id="img-pipeline",
                reason="",
                stack=["Java"],
                bullets=[
                    Bullet(
                        claim_id="ip-pipeline",
                        text="Built the rest of an event-driven pipeline on Amazon S3 and AWS Lambda to go live fast.",
                    )
                ],
            )
        ],
        skills=[],
    )
    assert validate(t, Facts()).errors == []


def test_json_ld_shapes() -> None:
    assert _ld_place({"address": "Seattle, WA"}) == "Seattle, WA"
    assert _ld_place([{"address": {"addressLocality": "Tacoma", "addressRegion": "WA"}}]) == "Tacoma, WA"
    assert _ld_place({"address": None}) is None
    assert _ld_name([{"name": "Acme"}]) == "Acme"
    assert _ld_name("Acme") == "Acme"
    assert _ld_name(None) is None


def test_bad_email_date() -> None:
    assert _date("garbage") == ""
    assert _date(None) == ""
    assert _date("Sat, 10 Oct 2026 14:10:02 +0000").startswith("2026-10-10")


def test_regenerate_keeps_reviewed_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.add_job(conn, id="t:1", source="test", title="Engineer", company="Acme", posting="x", status="applied")

    def fail(*_args: object, **_kwargs: object) -> None:
        msg = "model down"
        raise RuntimeError(msg)

    monkeypatch.setattr(pipeline, "produce", fail)
    with pytest.raises(RuntimeError):
        pipeline.generate_one(conn, "t:1", Facts())
    row = db.get_required(conn, "t:1")
    assert row["status"] == "applied"
    assert row["attempts"] == 1


@pytest.fixture
def tmp_db(tmp_path: Path, example_profile: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Also point the pipeline's lock and output folder at the test's temp dir."""
    monkeypatch.setattr(pipeline, "LOCK", tmp_path / ".lock")
    monkeypatch.setattr(progress, "OUT", tmp_path)
    return example_profile


def test_recover_marks_dead_runs_and_cleans_partials(tmp_db: Path) -> None:
    conn = db.connect()
    conn.execute(
        "INSERT INTO runs (pid, trigger, status, stage, current, started_at) "
        "VALUES (999999999, 'timer', 'running', 'triage', 'batch 3 of 14', '2026-10-11T08:00:00+00:00')"
    )
    conn.commit()
    (tmp_db.parent / "20261011-acme.partial").mkdir()
    notes = progress.recover(conn, clean_partials=True)
    run = progress.runs(conn)[0]
    assert run["status"] == "interrupted"
    assert "during triage" in run["error"]
    assert progress.resume_stage(conn) == "triage"
    assert not (tmp_db.parent / "20261011-acme.partial").exists()
    assert len(notes) == 2


@pytest.mark.usefixtures("tmp_db")
def test_stop_request_ends_run_cleanly_and_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fetch(_conn: object, p: progress.Progress) -> int:
        p.stage("fetch", 3)
        p.item("job 1")
        p.advance()
        progress.request_stop()  # what SIGTERM does
        p.item("job 2")  # raises StopRequestedError
        return 1

    monkeypatch.setattr(pipeline, "fetch", fake_fetch)
    stats = pipeline.run(skip_ingest=True)
    conn = db.connect()
    run = progress.runs(conn)[0]
    assert run["status"] == "stopped"
    assert (run["stage"], run["stage_done"], run["stage_total"]) == ("fetch", 1, 3)
    assert stats == {"filter": 0}
    assert progress.resume_stage(conn) == "fetch"


@pytest.mark.usefixtures("tmp_db")
def test_failed_stage_marks_run_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object) -> int:
        msg = "boom"
        raise ZeroDivisionError(msg)

    monkeypatch.setattr(pipeline, "triage_new", broken)
    with pytest.raises(ZeroDivisionError):
        pipeline.run(start="triage")
    run = progress.runs(db.connect())[0]
    assert run["status"] == "failed"
    assert "boom" in run["error"]


@pytest.mark.usefixtures("tmp_db")
def test_stop_only_targets_the_run_it_names() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "tailor-fake-run"])
    try:
        conn = db.connect()
        conn.execute(
            "INSERT INTO runs (pid, trigger, status, stage, started_at) VALUES (?, 'test', 'running', 'generate', ?)",
            (proc.pid, db.now()),
        )
        conn.commit()
        live = progress.live_run(conn)
        assert live is not None
        stale = int(live["id"]) - 1
        assert "nothing was stopped" in progress.stop(conn, now=True, run_id=stale, source="test")
        assert proc.poll() is None  # still alive
        assert "killed" in progress.stop(conn, now=True, run_id=int(live["id"]), source="test")
        proc.wait(timeout=5)
        run = progress.runs(conn)[0]
        assert run["status"] == "interrupted"
        assert "kill requested from test" in run["error"]
    finally:
        proc.kill()


def test_triage_scores_only_the_best_ranked_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.add_job(conn, id="t:summary", source="test", title="Dev", company="A", summary="Python AWS", posting_ok=0)
    db.add_job(conn, id="t:weak", source="test", title="Dev", company="B", posting="Cashier duties", posting_ok=1)
    db.add_job(conn, id="t:strong", source="test", title="Dev", company="C", posting="Python, AWS, SQL", posting_ok=1)
    sent: list[str] = []

    def fake_score(jobs: list[dict[str, object]], *_args: object) -> Iterator[tuple[dict[str, object], str]]:
        sent.extend(str(j["id"]) for j in jobs)
        yield {}, "fake"

    monkeypatch.setattr(triage, "score", fake_score)
    prefs = load_prefs()
    prefs["triage"]["max_per_run"] = 2
    pipeline.triage_new(conn, prefs, Facts())
    assert sent == ["t:strong", "t:weak"]  # full postings first; the summary-only job waits
    assert db.get_required(conn, "t:summary")["status"] == "new"


def test_recency_buckets() -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    ages = {"2026-10-19T00:00:00+00:00": 5, "2026-10-16T00:00:00+00:00": 4, "2026-10-12T00:00:00+00:00": 3}
    ages |= {"2026-10-01T00:00:00+00:00": 2, "2026-09-01T00:00:00+00:00": 1}
    assert {posted: triage.recency(posted, now) for posted in ages} == ages
    assert triage.recency(None, now) == 1


def test_years_required() -> None:
    assert triage.years_required("- 2 to 6 years of experience in robotics") == 2
    assert triage.years_required("Minimum of three years experience developing apps") == 3
    assert triage.years_required("- Consultant II: 1+ years of experience\n- Sr.: 3+ years of experience") == 1
    assert triage.years_required("2+ years of experience with Rust preferred") is None
    assert triage.years_required("In lieu of degree, 4 years of experience") is None
    assert triage.years_required("A 4-year degree. Founded 20 years ago.") is None
    prefs = load_prefs()
    job = {"title": "Software Engineer", "location": "Remote", "posting": "- 2+ years of professional experience"}
    assert hard_filter(job, prefs) == "requires 2+ years of experience"
    assert hard_filter({**job, "posting": "- 1+ years of experience"}, prefs) == "requires 1+ years of experience"
    assert hard_filter({**job, "posting": "- 0-2 years of experience"}, prefs) is None


def test_generate_sets_aside_awaiting_jobs_whose_posting_closed_or_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    for n, status in [(1, "generated"), (2, "approved"), (3, "applied"), (4, "approved")]:
        db.add_job(conn, id=f"t:{n}", source="test", title=f"Dev {n}", company="A", posting="x", posting_ok=1)
        db.update(conn, f"t:{n}", status=status)
    states = {"t:1": "closed", "t:2": "open", "t:4": "paused"}
    monkeypatch.setattr(sources, "posting_state", lambda job: states[job["id"]])
    pipeline.generate(conn, load_prefs(), Facts())
    got = [db.get_required(conn, f"t:{n}")["status"] for n in (1, 2, 3, 4)]
    assert got == ["filtered", "approved", "applied", "paused"]
    assert db.get_required(conn, "t:4")["paused_from"] == "approved"


def test_linkedin_banner_pauses_rather_than_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    page = '<h2 class="top-card-layout__title">Dev</h2><figure class="closed-job">No longer accepting applications'
    monkeypatch.setattr(sources, "http_get", lambda _url: ("", page))
    job = {"id": "linkedin:1", "source": "linkedin"}
    assert sources.fetch_posting(job)["status"] == sources.PAUSED
    assert sources.posting_state(job) == "paused"


def test_generic_page_not_accepting_applications_is_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    page = "<html><body><h1>Software Engineer</h1><p>We are not currently accepting applications.</p></body></html>"
    monkeypatch.setattr(sources, "http_get", lambda url: (url, page))
    assert sources.posting_state({"id": "manual:1", "source": "manual", "url": "https://example.com/j"}) == "paused"


def test_fetch_pauses_new_jobs_and_keeps_the_posting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.add_job(conn, id="t:1", source="test", title="Dev", company="A", url="https://example.com/j")
    fields = {"posting": "x" * 700, "posting_ok": 1, "status": sources.PAUSED}
    monkeypatch.setattr(sources, "fetch_posting", lambda _job: dict(fields))
    pipeline.fetch(conn)
    row = db.get_required(conn, "t:1")
    assert (row["status"], row["paused_from"], row["posting_ok"]) == ("paused", "new", 1)


def test_generate_skips_closed_postings_and_fills_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    for n, rel in [(1, 5), (2, 4), (3, 3), (4, 3)]:
        db.add_job(conn, id=f"t:{n}", source="test", title=f"Dev {n}", company="A", posting="x", posting_ok=1)
        db.update(conn, f"t:{n}", status="triaged", relevance=rel, desirability=3)
    states = {"t:1": "closed", "t:2": "paused"}
    monkeypatch.setattr(sources, "posting_state", lambda job: states.get(job["id"], "open"))
    made: list[str] = []
    monkeypatch.setattr(pipeline, "generate_one", lambda _conn, job_id, *_args: made.append(job_id))
    prefs = load_prefs()
    prefs["generate"]["max_per_run"] = 2
    assert pipeline.generate(conn, prefs, Facts()) == 2
    assert made == ["t:3", "t:4"]  # the best job closed and the next paused, so the next two were written
    assert db.get_required(conn, "t:1")["filter_reason"] == "posting closed"
    assert db.get_required(conn, "t:2")["status"] == "paused"


def test_recheck_reopens_closes_and_gives_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    for n in range(1, 5):
        db.add_job(conn, id=f"t:{n}", source="test", title=f"Dev {n}", company="A", posting="x", posting_ok=1)
        db.update(conn, f"t:{n}", status="paused", paused_from="triaged", paused_at=db.now())
    db.update(conn, "t:4", paused_at="2026-01-01T00:00:00+00:00")
    states = {"t:1": "open", "t:2": "closed", "t:3": "paused", "t:4": "paused"}
    monkeypatch.setattr(sources, "posting_state", lambda job: states[job["id"]])
    assert pipeline.recheck(conn, load_prefs()) == 1
    rows = [db.get_required(conn, f"t:{n}") for n in range(1, 5)]
    assert [r["status"] for r in rows] == ["triaged", "filtered", "paused", "filtered"]
    assert rows[0]["paused_at"] is None
    assert rows[3]["filter_reason"] == "not taking applications for 30+ days"
