"""Fit tailored content to the page by compiling and measuring, not guessing.

The model ranks everything (skills within each row, projects, bullets within
each project) most relevant first; this module keeps as much as fits:
  - each skill row is cut from its end until it fits on one line
  - project bullets are cut from the lowest-ranked project until the PDF is one page
"""

import copy
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .render import compile_pdf, page_count, render_tex

MAX_ROUNDS = 40
MIN_PROJECT_BULLETS = 2  # a project with fewer is dropped rather than shown thin


def _wrapped_rows(pdf: Path, categories: list[str]) -> list[str]:
    """Categories whose skill row wrapped onto a second line."""
    if shutil.which("pdftotext") is None:
        msg = "pdftotext not found; install poppler-utils (sudo dnf install poppler-utils)"
        raise RuntimeError(msg)
    text = subprocess.run(["pdftotext", "-raw", str(pdf), "-"], check=False, capture_output=True, text=True).stdout
    m = re.search(r"Technical Skills\n(.*?)\nProjects\n", text, re.DOTALL)
    if m is None:
        return []
    wrapped: list[str] = []
    current: str | None = None
    for line in filter(None, (raw.strip() for raw in m.group(1).splitlines())):
        label = next((c for c in categories if line.startswith(f"{c}:")), None)
        if label is not None:
            current = label
        elif current is not None and current not in wrapped:
            wrapped.append(current)
    return wrapped


def fit(resume: dict[str, Any], outdir: Path) -> tuple[dict[str, Any], list[str]]:
    """Compile resume into outdir/resume.pdf, trimming until it fits.

    Returns the content actually rendered and notes on what was trimmed.
    """
    resume = copy.deepcopy(resume)
    notes: list[str] = []
    tex = outdir / "resume.tex"

    for _ in range(MAX_ROUNDS):
        tex.write_text(render_tex(resume))
        pdf = compile_pdf(tex)
        if pdf is None:
            return resume, ["no LaTeX engine found; only resume.tex was written, nothing was fitted"]

        wrapped = [c for c in _wrapped_rows(pdf, list(resume["skills"])) if len(resume["skills"][c]) > 1]
        if wrapped:
            for cat in wrapped:
                dropped = resume["skills"][cat].pop()
                notes.append(f"skills/{cat}: dropped {dropped!r} to keep the row on one line")
            continue

        pages = page_count(pdf)
        if pages is None:
            notes.append("couldn't count pages (pdfinfo missing?); one-page fit not checked")
        elif pages > 1:
            if not resume["projects"]:
                notes.append("still over one page with no projects left")
                return resume, notes
            last = resume["projects"][-1]
            if len(last["bullets"]) > MIN_PROJECT_BULLETS:
                last["bullets"].pop()
                notes.append(f"{last['name']}: dropped its lowest-ranked bullet to fit one page")
            else:
                resume["projects"].pop()
                notes.append(f"{last['name']}: dropped the project to fit one page")
            continue

        return resume, notes
    msg = f"could not fit the resume in {MAX_ROUNDS} rounds"
    raise RuntimeError(msg)
