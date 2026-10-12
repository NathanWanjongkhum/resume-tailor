"""Run tracking: live progress for the dashboard, graceful stops, and crash recovery.

A run is one row in the `runs` table, updated as the pipeline works. Stopping:

- SIGTERM (dashboard "Stop", `tailor stop`, `systemctl --user stop`) sets a flag; the
  pipeline finishes the item it's on, then ends the run as `stopped`.
- SIGKILL ("Stop now") ends it immediately; the row is left `running` with a dead pid,
  and `recover()` marks it `interrupted` on the next run or dashboard refresh.

Nothing is lost either way: every stage commits each job as it finishes, and each
stage only picks up work that's still pending, so the next run resumes where this
one stopped.
"""

import contextlib
import json
import logging
import os
import shutil
import signal
import sqlite3
import threading
import time
from pathlib import Path
from types import FrameType
from typing import Any, cast, override

from . import db
from .paths import OUT

log = logging.getLogger("tailor")

STAGES = ("ingest", "filter", "fetch", "triage", "generate")

_stop = threading.Event()


class StopRequestedError(Exception):
    """Raised between items once a stop was requested."""


def request_stop() -> None:
    """Make the pipeline stop before its next item (what SIGTERM triggers)."""
    _stop.set()


def _on_sigterm(_signum: int, _frame: FrameType | None) -> None:
    request_stop()


def install_stop_handler() -> None:
    _stop.clear()
    signal.signal(signal.SIGTERM, _on_sigterm)


class Progress:
    """No-op progress sink, so stages can run without a tracked run (tests, single stages)."""

    def stage(self, name: str, total: int | None) -> None:
        pass

    def item(self, label: str) -> None:  # noqa: ARG002
        """Call before each unit of work; raises StopRequestedError if a stop was requested."""
        if _stop.is_set():
            raise StopRequestedError

    def advance(self, n: int = 1) -> None:
        pass


class Tracker(Progress):
    """Writes progress to the `runs` row for this process."""

    def __init__(self, conn: sqlite3.Connection, trigger: str, args: str) -> None:
        self.conn = conn
        cur = conn.execute(
            "INSERT INTO runs (pid, trigger, args, status, started_at, stats, stage_seconds) "
            "VALUES (?, ?, ?, 'running', ?, '{}', '{}')",
            (os.getpid(), trigger, args, db.now()),
        )
        conn.commit()
        self.id = cast("int", cur.lastrowid)
        self.stats: dict[str, Any] = {}
        self.seconds: dict[str, float] = {}
        self._stage: str | None = None
        self._stage_t0 = time.monotonic()

    def _set(self, **fields: object) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), self.id))  # noqa: S608
        self.conn.commit()

    def _close_stage(self) -> None:
        if self._stage is not None:
            self.seconds[self._stage] = round(time.monotonic() - self._stage_t0, 1)

    @override
    def stage(self, name: str, total: int | None) -> None:
        self._close_stage()
        self._stage, self._stage_t0 = name, time.monotonic()
        self._set(
            stage=name,
            stage_done=0,
            stage_total=total,
            stage_started_at=db.now(),
            current=None,
            stage_seconds=json.dumps(self.seconds),
        )

    @override
    def item(self, label: str) -> None:
        super().item(label)
        self._set(current=label[:200])

    @override
    def advance(self, n: int = 1) -> None:
        self.conn.execute("UPDATE runs SET stage_done = stage_done + ? WHERE id = ?", (n, self.id))
        self.conn.commit()

    def record(self, key: str, value: object) -> None:
        self.stats[key] = value
        self._set(stats=json.dumps(self.stats))

    def finish(self, status: str, error: str | None = None) -> None:
        self._close_stage()
        if error is None:  # keep a "stop requested from ..." note written by stop()
            row = self.conn.execute("SELECT error FROM runs WHERE id = ?", (self.id,)).fetchone()
            error = str(row["error"]) if row is not None and row["error"] is not None else None
        self._set(
            status=status,
            error=error,
            current=None,
            finished_at=db.now(),
            stats=json.dumps(self.stats),
            stage_seconds=json.dumps(self.seconds),
        )


def pid_alive(pid: int) -> bool:
    """True if `pid` is a live tailor process (guards against pid reuse)."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"tailor" in cmdline


def runs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return cast("list[sqlite3.Row]", conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall())


def live_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    for row in conn.execute("SELECT * FROM runs WHERE status = 'running' ORDER BY id DESC"):
        if pid_alive(row["pid"]):
            return cast("sqlite3.Row", row)
    return None


def recover(conn: sqlite3.Connection, *, clean_partials: bool) -> list[str]:
    """Close out runs whose process died, and optionally delete half-written resumes.

    Only pass clean_partials=True when no run can be live (i.e. while holding the
    pipeline lock), since a live run writes into its .partial folder.
    """
    notes = []
    for row in conn.execute("SELECT * FROM runs WHERE status = 'running'").fetchall():
        if not pid_alive(row["pid"]):
            where = f" during {row['stage']}" if row["stage"] else ""
            current = f" (working on: {row['current']})" if row["current"] else ""
            error = f"process ended without finishing{where}{current}: killed, crashed, or the machine shut down"
            conn.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ?, error = ? WHERE id = ?",
                (db.now(), error, row["id"]),
            )
            notes.append(f"run {row['id']} marked interrupted{where}")
    conn.commit()
    if clean_partials:
        for partial in OUT.glob("*.partial"):
            shutil.rmtree(partial, ignore_errors=True)
            notes.append(f"removed unfinished output {partial.name}")
    return notes


def resume_stage(conn: sqlite3.Connection) -> str | None:
    """Stage the most recent run stopped in, if it didn't finish."""
    row = conn.execute("SELECT status, stage FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if row and row["status"] in {"stopped", "failed", "interrupted"} and row["stage"] in STAGES:
        return str(row["stage"])
    return None


def _descendants(pid: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        children.setdefault(int(fields[1]), []).append(int(stat.parent.name))
    out: list[int] = []
    todo = [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            out.append(child)
            todo.append(child)
    return out


def stop(conn: sqlite3.Connection, *, now: bool = False, run_id: int | None = None, source: str = "cli") -> str:
    """Ask the live run to stop after its current item, or kill it (and claude) now.

    `run_id` makes the request target one specific run: if that run isn't the live
    one any more, nothing is signalled. `source` is recorded on the run and logged.
    """
    run = live_run(conn)
    if run is None:
        return "no pipeline run is active"
    if run_id is not None and run["id"] != run_id:
        return f"run {run_id} is no longer running; nothing was stopped"
    pid = int(run["pid"])
    how = "kill" if now else "stop after current item"
    log.warning("run %s: %s requested from %s", run["id"], how, source)
    conn.execute("UPDATE runs SET error = ? WHERE id = ?", (f"{how} requested from {source} at {db.now()}", run["id"]))
    conn.commit()
    if not now:
        os.kill(pid, signal.SIGTERM)
        return f"run {run['id']} will stop after its current item"
    for p in [*reversed(_descendants(pid)), pid]:
        with contextlib.suppress(ProcessLookupError):
            os.kill(p, signal.SIGKILL)
    conn.execute(
        "UPDATE runs SET status = 'interrupted', finished_at = ? WHERE id = ?",
        (db.now(), run["id"]),
    )
    conn.commit()
    return f"run {run['id']} killed"
