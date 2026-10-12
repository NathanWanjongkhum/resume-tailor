"""Produce one tailored resume: generate, check, compile, report."""

import difflib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import llm
from .facts import Facts
from .fit import fit
from .generate import tailor
from .paths import OUT, ROOT
from .schema import Tailored
from .validate import Report


@dataclass
class Result:
    name: str
    tailored: Tailored
    report: Report
    model: str
    header: str = ""
    fit_notes: list[str] = field(default_factory=list)


def to_resume(t: Tailored, facts: Facts) -> dict[str, Any]:
    """Template content for a tailored resume.

    Unknown experience/project ids are skipped here; the validator reports them.
    """
    return {
        "contact": facts.raw["contact"],
        "education": facts.raw["education"],
        "experience": [
            {**facts.experience[p.experience_id], "bullets": [b.text for b in p.bullets]}
            for p in t.experience
            if p.experience_id in facts.experience
        ],
        "projects": [
            {**facts.projects[p.project_id], "stack": p.stack, "bullets": [b.text for b in p.bullets]}
            for p in t.projects
            if p.project_id in facts.projects
        ],
        "skills": {line.category: line.items for line in t.skills if line.items},
    }


def build(resume: dict[str, Any], outdir: Path) -> tuple[dict[str, Any], list[str]]:
    """Compile resume.pdf into outdir, trimming to fit. Returns what was rendered and notes."""
    outdir.mkdir(parents=True, exist_ok=True)
    return fit(resume, outdir)


def produce(  # noqa: PLR0913
    posting: str,
    name: str,
    facts: Facts,
    *,
    company: str | None = None,
    via: llm.Backend = "plan",
    effort: llm.Effort = "high",
    header: str = "",
) -> tuple[Path, Report, str]:
    """Tailor, check, compile and report one resume. Returns (outdir, report, model)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    outdir = OUT / f"{datetime.now().astimezone():%Y%m%d-%H%M%S}-{slug}"
    t, rep, model = tailor(posting, facts, company=company, via=via, effort=effort)
    # Build in a .partial folder and rename when complete, so a run killed mid-build
    # never leaves a half-written resume that looks finished (recovery deletes these).
    work = outdir.with_name(outdir.name + ".partial")
    work.mkdir(parents=True, exist_ok=True)
    (work / "posting.txt").write_text(posting)
    if company is not None:
        (work / "company.txt").write_text(company)
    (work / "tailored.json").write_text(json.dumps(t.model_dump(), indent=2))
    _, notes = build(to_resume(t, facts), work)
    result = Result(name=name, tailored=t, report=rep, model=model, header=header, fit_notes=notes)
    (work / "report.md").write_text(render_report(result, facts))
    work.rename(outdir)
    return outdir, rep, model


def git_rev() -> str:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], check=False, cwd=ROOT, capture_output=True, text=True).stdout.strip()

    dirty = git("status", "--porcelain", "data", "templates", "tailor")
    return (git("rev-parse", "--short", "HEAD") or "uncommitted") + ("-dirty" if dirty else "")


def _github_only_skills(t: Tailored, facts: Facts) -> list[str]:
    """Listed skills whose only evidence is a GitHub repo, with that evidence."""
    pool = {k.lower(): v for k, v in facts.skill_pool().items()}
    out = []
    for skill in (s for line in t.skills for s in line.items):
        evidence = pool.get(skill.lower(), {}).get("evidence", [])
        if evidence and all(e.startswith("github ") for e in evidence):
            out.append(f"{skill}: {evidence[0]}")
    return out


def _wording_diff(t: Tailored, facts: Facts) -> list[str]:
    lines = []
    for b in [b for p in [*t.experience, *t.projects] for b in p.bullets]:
        claim = facts.claims.get(b.claim_id)
        if claim and claim.text != b.text:
            diff = difflib.ndiff(claim.text.split(), b.text.split())
            marks = {" ": "{}", "-": "~~{}~~", "+": "**{}**"}
            words = [marks[w[0]].format(w[2:]) for w in diff if w[0] in marks]
            lines.append(f"- `{b.claim_id}`: {' '.join(words)}")
    return lines


def render_report(r: Result, facts: Facts) -> str:
    t, rep = r.tailored, r.report
    stamp = f"{datetime.now().astimezone():%Y-%m-%d %H:%M}"
    lines = [f"# {r.name}", "", f"Generated {stamp} by `{r.model}`, facts/template rev `{git_rev()}`.", ""]
    if r.header:
        lines += [r.header, ""]
    lines += ["## Fit", "", t.fit_summary, ""]
    lines += [f"## Checks: {'PASS' if rep.ok else 'NEEDS REVIEW'}", ""]
    lines += [f"- ERROR: {e}" for e in rep.errors] + [f"- warning: {w}" for w in rep.warnings]
    if rep.ok and not rep.warnings:
        lines.append("- every bullet cites a claim and adds no numbers or technologies")
    lines += ["", "## Trimmed to fit one page", ""] + ([f"- {n}" for n in r.fit_notes] or ["- nothing"])
    if github_only := _github_only_skills(t, facts):
        lines += ["", "## Skills added from GitHub evidence", ""] + [f"- {s}" for s in github_only]
    lines += ["", "## Projects (ranked; see trims above)", ""]
    lines += [
        f"- **{facts.projects[p.project_id]['name']}**: {p.reason}"
        for p in t.projects
        if p.project_id in facts.projects
    ]
    lines += ["", "## Requirements matched", ""] + [f"- {m}" for m in t.matched_requirements]
    lines += ["", "## Gaps (not on the resume)", ""] + ([f"- {g}" for g in t.gaps] or ["- none"])
    lines += ["", "## Wording changes vs. facts", "", *_wording_diff(t, facts)]
    return "\n".join(lines) + "\n"
