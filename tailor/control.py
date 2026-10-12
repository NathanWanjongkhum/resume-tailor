"""Start, stop and schedule pipeline runs from the dashboard.

Runs started here go through `systemd-run --user`, so they keep going if the
dashboard restarts and their logs land in the journal, like the timer's runs.
"""

import shutil
import subprocess
import sys
from dataclasses import dataclass

from .paths import ROOT

TIMER = "resume-tailor.timer"
MANUAL_UNIT = "resume-tailor-manual"


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", "--user", *args], check=False, capture_output=True, text=True)


def start(*, start_stage: str = "ingest", skip_ingest: bool = False) -> None:
    """Launch a run in the background, as a transient systemd user unit when possible."""
    cmd = [sys.executable, "-m", "tailor", "run", "--trigger", "dashboard", "--from", start_stage]
    if skip_ingest:
        cmd.append("--skip-ingest")
    if shutil.which("systemd-run") is None:
        subprocess.Popen(cmd, cwd=ROOT, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    _systemctl("reset-failed", f"{MANUAL_UNIT}.service")  # clear a failed unit from an earlier run
    subprocess.run(
        [
            "systemd-run",
            "--user",
            f"--unit={MANUAL_UNIT}",
            "--description=Resume tailor pipeline (started from the dashboard)",
            "--collect",
            # SIGTERM goes to the pipeline only, which stops after its current item;
            # the claude subprocess is left to finish that item.
            "--property=KillMode=mixed",
            "--property=TimeoutStopSec=300",
            f"--working-directory={ROOT}",
            *cmd,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@dataclass
class TimerState:
    enabled: bool
    next_run: str
    last_run: str


def timer_state() -> TimerState:
    props = dict(
        line.split("=", 1)
        for line in _systemctl(
            "show",
            TIMER,
            "-p",
            "UnitFileState",
            "-p",
            "NextElapseUSecRealtime",
            "-p",
            "LastTriggerUSec",
        ).stdout.splitlines()
        if "=" in line
    )
    return TimerState(
        enabled=props.get("UnitFileState") == "enabled",
        next_run=props.get("NextElapseUSecRealtime", "") or "not scheduled",
        last_run=props.get("LastTriggerUSec", "") or "never",
    )


def set_timer(*, on: bool) -> str:
    result = _systemctl("enable" if on else "disable", "--now", TIMER)
    return (result.stderr or result.stdout).strip() or ("timer on" if on else "timer off")
