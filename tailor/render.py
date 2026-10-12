"""Render resume content to .tex with Jinja and compile it to PDF."""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import jinja2

from .paths import ROOT

_TEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_TEX_RE = re.compile("|".join(re.escape(k) for k in _TEX_SPECIALS))


def tex_escape(value: object) -> str:
    # "--" is kept as-is so date ranges render as en dashes.
    return _TEX_RE.sub(lambda m: _TEX_SPECIALS[m.group()], str(value))


_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(ROOT / "templates"),
    block_start_string="((*",
    block_end_string="*))",
    variable_start_string="(((",
    variable_end_string=")))",
    comment_start_string="((#",
    comment_end_string="#))",
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=jinja2.StrictUndefined,
    # Output is LaTeX, not HTML: HTML autoescaping would corrupt it. Every interpolated
    # string goes through the `tex` filter in the template instead.
    autoescape=False,  # noqa: S701
)
_env.filters["tex"] = tex_escape


def render_tex(resume: dict[str, Any], template: str = "resume.tex.j2") -> str:
    return _env.get_template(template).render(r=resume)


def compile_pdf(tex_path: Path) -> Path | None:
    """Compile with Tectonic (setup.sh installs it into .bin/); None if it isn't installed."""
    local = ROOT / ".bin" / "tectonic"
    tectonic = str(local) if local.exists() else shutil.which("tectonic")
    if tectonic is None:
        return None
    proc = subprocess.run([tectonic, tex_path.name], check=False, cwd=tex_path.parent, capture_output=True, text=True)
    if proc.returncode != 0:
        msg = f"LaTeX failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}"
        raise RuntimeError(msg)
    return tex_path.with_suffix(".pdf")


def page_count(pdf: Path) -> int | None:
    if shutil.which("pdfinfo") is None:
        return None
    out = subprocess.run(["pdfinfo", str(pdf)], check=False, capture_output=True, text=True).stdout
    m = re.search(r"^Pages:\s+(\d+)", out, re.MULTILINE)
    return int(m.group(1)) if m else None
