"""SQLite job store: every stage reads and writes job rows here.

Job status flow::

    new -> filtered                      (failed a hard filter in preferences)
    new -> triaged -> generated -> approved -> applied
    any reviewable state -> rejected
    any -> error                         (see the `error` column; retried next run)
"""

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .paths import DATA

DB_PATH = DATA / "jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,        -- source:external id, e.g. linkedin:4475264262
    dedupe_key    TEXT UNIQUE,             -- normalized company|title, catches cross-source repeats
    source        TEXT NOT NULL,
    url           TEXT,                    -- where the posting text came from
    apply_url     TEXT,
    title         TEXT,
    company       TEXT,
    location      TEXT,
    salary        TEXT,
    summary       TEXT,                    -- the blurb from the alert email
    posting       TEXT,                    -- full posting text
    posting_ok    INTEGER DEFAULT 0,       -- 1 if the full posting was fetched
    company_info  TEXT,                    -- employer's public profile; '' if none, NULL if not looked up
    status        TEXT NOT NULL DEFAULT 'new',
    filter_reason TEXT,
    relevance     INTEGER,
    desirability  INTEGER,
    triage        TEXT,                    -- JSON: reasons, red flags, model
    outdir        TEXT,
    checks_ok     INTEGER,
    model         TEXT,
    error         TEXT,
    attempts      INTEGER DEFAULT 0,       -- failed generation attempts; capped by MAX_ATTEMPTS
    email_id      TEXT,
    posted_at     TEXT,                    -- UTC ISO; from the source, else the alert email's date
    found_at      TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS emails (
    message_id  TEXT PRIMARY KEY,
    sender      TEXT,
    subject     TEXT,
    received_at TEXT,
    jobs_found  INTEGER,
    seen_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    pid              INTEGER NOT NULL,
    trigger          TEXT NOT NULL,          -- timer | dashboard | cli
    args             TEXT,
    status           TEXT NOT NULL,          -- running | done | stopped | failed | interrupted
    stage            TEXT,
    stage_done       INTEGER DEFAULT 0,
    stage_total      INTEGER,                -- NULL while unknown (e.g. counting emails)
    stage_started_at TEXT,
    current          TEXT,                   -- what the stage is working on right now
    stats            TEXT,                   -- JSON: per-stage counts
    stage_seconds    TEXT,                   -- JSON: per-stage durations
    error            TEXT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT
);

CREATE TABLE IF NOT EXISTS profile (
    kind       TEXT PRIMARY KEY,  -- facts | preferences | github; see profile.py
    body       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    body        TEXT NOT NULL,   -- the version that was replaced
    replaced_at TEXT NOT NULL
);
"""

STATUSES = ["new", "filtered", "triaged", "generated", "approved", "applied", "rejected", "error"]
_JOBS_DDL = SCHEMA.split("CREATE TABLE IF NOT EXISTS emails", maxsplit=1)[0]
_COLUMN_DDL = dict(re.findall(r"^    (\w+)\s+([A-Z][^,\n]*?),?\s*(?:--.*)?$", _JOBS_DDL, re.MULTILINE))
COLUMNS = frozenset(_COLUMN_DDL)
MAX_ATTEMPTS = 2


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # the dashboard reads while the pipeline writes
    conn.executescript(SCHEMA)
    # Columns added after a database was created: add them in place.
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    for name in COLUMNS - existing:
        conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {_COLUMN_DDL[name]}")
    conn.commit()
    return conn


def dedupe_key(company: str | None, title: str | None) -> str | None:
    if company is None or title is None or not company or not title:
        return None

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    key = f"{norm(company)}|{norm(title)}".encode()
    return hashlib.sha1(key, usedforsecurity=False).hexdigest()[:16]


def _check_columns(fields: dict[str, object]) -> None:
    """Column names are interpolated into SQL, so only known columns are allowed."""
    if unknown := fields.keys() - COLUMNS:
        msg = f"unknown job columns: {sorted(unknown)}"
        raise ValueError(msg)


def add_job(conn: sqlite3.Connection, **fields: object) -> bool:
    """Insert a job unless its id or company+title is already known. Returns True if added."""
    company, title = fields.get("company"), fields.get("title")
    fields.setdefault(
        "dedupe_key",
        dedupe_key(company if isinstance(company, str) else None, title if isinstance(title, str) else None),
    )
    fields["found_at"] = fields["updated_at"] = now()
    _check_columns(fields)
    cols = ", ".join(fields)
    marks = ", ".join(f":{k}" for k in fields)
    cur = conn.execute(f"INSERT OR IGNORE INTO jobs ({cols}) VALUES ({marks})", fields)  # noqa: S608
    conn.commit()
    return cur.rowcount == 1


def update(conn: sqlite3.Connection, job_id: str, **fields: object) -> None:
    if "triage" in fields and not isinstance(fields["triage"], (str, type(None))):
        fields["triage"] = json.dumps(fields["triage"])
    fields["updated_at"] = now()
    _check_columns(fields)
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id = :_id", {**fields, "_id": job_id})  # noqa: S608
    conn.commit()


def get_required(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    if (row := get(conn, job_id)) is None:
        msg = f"no job with id {job_id!r}"
        raise KeyError(msg)
    return row


def jobs(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    if status is not None:
        cur = conn.execute("SELECT * FROM jobs WHERE status = ? ORDER BY found_at", (status,))
    else:
        cur = conn.execute("SELECT * FROM jobs ORDER BY found_at DESC")
    return cast("list[sqlite3.Row]", cur.fetchall())


def get(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return cast("sqlite3.Row | None", conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def email_seen(conn: sqlite3.Connection, message_id: str) -> bool:
    return conn.execute("SELECT 1 FROM emails WHERE message_id = ?", (message_id,)).fetchone() is not None


def mark_email(  # noqa: PLR0913
    conn: sqlite3.Connection, message_id: str, *, sender: str, subject: str, received_at: str, jobs_found: int
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO emails VALUES (?, ?, ?, ?, ?, ?)",
        (message_id, sender, subject, received_at, jobs_found, now()),
    )
    conn.commit()
