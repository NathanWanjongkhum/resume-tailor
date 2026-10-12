"""Serve resume PDFs to the dashboard so the browser's own viewer shows them, links and all.

Streamlit serves only files under `tailor/static/` (with `server.enableStaticServing`) and
rejects symlinks that lead out of it, so each PDF is copied in. Copies are named by content
hash: a regenerated resume gets a new URL, so neither the browser cache nor an older copy
can show stale content, and a copy is never rewritten while it is being read.
"""

import hashlib
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"
# Fit the page to the frame's width and hide the sidebar: Chrome's viewer reads the first
# two, Firefox's (pdf.js) the last two.
VIEW = "#view=FitH&navpanes=0&zoom=page-width&pagemode=none"


def digest(pdf: Path) -> str:
    return hashlib.sha256(pdf.read_bytes()).hexdigest()[:32]


def publish(pdf: Path, static: Path = STATIC) -> str:
    """Copy pdf into static (once per content) and return the URL the dashboard embeds."""
    name = f"{digest(pdf)}.pdf"
    target = static / name
    if not target.exists():
        static.mkdir(exist_ok=True)
        # Write, then rename into place, so a request never reads a half-written file.
        fd, tmp = tempfile.mkstemp(dir=static, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(pdf.read_bytes())
        Path(tmp).replace(target)
    return f"app/static/{name}{VIEW}"


def prune(current: Iterable[Path], static: Path = STATIC) -> None:
    """Delete copies whose resume no longer exists, so they stop being served."""
    keep = {f"{digest(pdf)}.pdf" for pdf in current if pdf.exists()}
    for path in static.glob("*.pdf"):
        if path.name not in keep:
            path.unlink(missing_ok=True)
