import sqlite3
from pathlib import Path

import pytest

from tailor import db, gmail, tracking


@pytest.mark.parametrize(
    ("subject", "body", "kind"),
    [
        ("Thank you for applying to Acme", "We received your application. If selected, we'll be in touch.", "received"),
        (
            "Your application to Acme",
            "Thanks for applying! Our team will reach out to schedule an interview if there's a fit.",
            "received",
        ),
        ("Acme: next steps", "We'd like to invite you to a phone screen. Please share your availability.", "interview"),
        ("Interview with Acme", "See you Tuesday.", "interview"),
        (
            "Your application to Acme",
            "Thank you for interviewing with us. Unfortunately, we have decided to move forward with other candidates.",
            "not_selected",
        ),
        ("Update", "We regret to inform you that we are unable to offer you a position.", "not_selected"),
        ("Acme offer", "We are pleased to extend an offer for the Software Engineer role.", "offer"),
        ("Welcome to the team!", "Your first day is Monday.", "hired"),
        ("Acme newsletter", "Read about our new product launch.", "other"),
    ],
)
def test_classify_replies(subject: str, body: str, kind: str) -> None:
    assert tracking.classify(subject, body).kind == kind


def test_core_name() -> None:
    assert tracking.core_name("Acme, Inc.") == "Acme"
    assert tracking.core_name("Northwind Traders LLC (US)") == "Northwind Traders"


def reply(conn: sqlite3.Connection, n: int, sender: str, subject: str, body: str) -> bool:
    msg = gmail.Message(f"<m{n}>", sender, subject, f"2026-10-0{n}T10:00:00+00:00", body)
    return tracking.record(conn, msg, tracking.candidates(conn))


def test_replies_move_applications_forward_only(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.add_job(conn, id="t:1", source="test", title="Software Engineer", company="Acme, Inc.", status="approved")
    db.add_job(conn, id="t:2", source="test", title="Data Engineer", company="Globex", status="applied")
    assert reply(conn, 1, "Acme Recruiting <no-reply@acme.greenhouse.io>", "Thanks", "Thank you for applying.")
    assert db.get_required(conn, "t:1")["status"] == "applied"
    assert db.get_required(conn, "t:1")["applied_at"] == "2026-10-01T10:00:00+00:00"
    assert reply(conn, 2, "jobs@ashbyhq.com", "Acme: phone screen", "We'd like to schedule a phone screen.")
    assert db.get_required(conn, "t:1")["status"] == "interview"
    assert not reply(conn, 3, "Acme <hr@acme.com>", "Thank you for applying", "Thank you for your application.")
    assert db.get_required(conn, "t:1")["status"] == "interview"  # a late confirmation doesn't undo it
    assert not reply(conn, 4, "news@acme.com", "Acme newsletter", "Our new product.")
    assert db.event_seen(conn, "<m4>")
    assert [e["kind"] for e in db.events(conn, "t:1")] == ["received", "interview", "received"]
    assert db.get_required(conn, "t:2")["status"] == "applied"


def test_unmatched_reply_can_be_assigned(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.add_job(conn, id="t:1", source="test", title="Dev", company="Globex", status="applied")
    assert not reply(conn, 1, "hr@initech.com", "Update", "We regret to inform you that the role was filled.")
    (event,) = tracking.unmatched(conn)
    assert tracking.assign(conn, event["id"], "t:1")
    assert db.get_required(conn, "t:1")["status"] == "not_selected"
    assert tracking.unmatched(conn) == []


def test_title_breaks_a_tie_between_jobs_at_one_company(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.add_job(conn, id="t:1", source="test", title="Backend Engineer", company="Acme", status="applied")
    db.add_job(conn, id="t:2", source="test", title="Frontend Engineer", company="Acme", status="applied")
    reply(conn, 1, "hr@acme.com", "Frontend Engineer interview", "Please share your availability.")
    assert [db.get_required(conn, i)["status"] for i in ("t:1", "t:2")] == ["applied", "interview"]


def test_manual_status_is_recorded(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.add_job(conn, id="t:1", source="test", title="Dev", company="Acme", status="generated")
    tracking.set_status(conn, "t:1", "offer")
    row = db.get_required(conn, "t:1")
    assert row["status"] == "offer"
    assert row["applied_at"] is not None
    assert [(e["kind"], e["source"]) for e in db.events(conn, "t:1")] == [("offer", "manual")]


def test_tracking_queries_name_each_company(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.add_job(conn, id="t:1", source="test", title="Dev", company='Acme "Labs", Inc.', status="applied")
    db.add_job(conn, id="t:2", source="test", title="Dev", company="Globex", status="triaged")
    (query,) = tracking.queries(conn, tracking.candidates(conn))
    assert query.startswith('("Acme Labs") after:')
    assert "-from:me -from:(jobalerts-noreply@linkedin.com OR ali@hiring.cafe)" in query
