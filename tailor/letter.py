"""Cover letters for postings that ask for one.

The letter is the user's own template (PARAGRAPHS). Code fills in everything it can:
the contact block, date, greeting, education and sign-off. Claude writes only the four
short phrases the template leaves open, from a trimmed posting, the employer profile and
the resume it already tailored for this job. It never sees the full facts, so the call
stays small and has nothing else to work on. The phrases go through the same number and
technology checks as resume bullets.
"""

import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import llm
from .facts import Facts
from .schema import Tailored
from .validate import Report, numbers, tech_terms

POSTING_CHARS = 6_000  # what the employer does and the role's focus are near the top
TOP_PROJECTS = 2  # the resume's best-ranked projects are enough to pick strengths from
NAME = "cover_letter"

# Any mention counts unless the same sentence declines it: "no cover letter", "cover letter
# not required". "Cover letter (optional)" still counts: a letter helps there.
_ASKS = re.compile(r"cover[- ]?letter|letter of (?:interest|intent|motivation)|motivation(?:al)? letter", re.IGNORECASE)
_DECLINED_BEFORE = re.compile(r"\b(?:no|without|don'?t|do not)\b[^.\n]{0,30}$", re.IGNORECASE)
_DECLINED_AFTER = re.compile(r"^[^.\n]{0,20}\b(?:not (?:required|needed|necessary)|unnecessary)", re.IGNORECASE)
_NEAR = 40

PARAGRAPHS = (
    (
        "Good day! I saw your job post and {employer} {hook} really stood out to me, "
        "and I'd love to be part of your team."
    ),
    (
        "After graduating from {school} with {degree}, I've decided to follow my passion to {industry}. "
        "I bring {bring}, and I've always enjoyed {enjoy}. "
        "Based on your job ad, I believe I could be a strong match."
    ),
    "Thank you for your time and consideration. I look forward to hearing from you.",
)
CLOSING = "Sincerely,"
_ASCII = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "--", "\u2014": "--"})


# The "passion" line names a broad field, never the employer's niche ("digital identity
# software"), so the model picks from this list instead of writing its own.
Industry = Literal[
    "software engineering",
    "web development",
    "cloud computing",
    "data and machine learning",
    "cybersecurity",
    "developer tools",
    "healthcare technology",
    "financial technology",
    "education technology",
    "consumer apps",
    "e-commerce",
    "gaming",
    "robotics",
    "aerospace",
    "energy and climate",
    "public service",
]


class Phrases(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hook: str = Field(description="what about this employer or role stood out; from <job_posting> or <company>")
    industry: Industry = Field(description="the broad field this role is in; 'software engineering' if none fits")
    bring: str = Field(description="2-3 strengths the posting asks for that <resume> shows, as one noun phrase")
    enjoy: str = Field(description="an activity central to this role that <resume> shows the candidate doing")


# Longest each phrase may be, so the letter stays short and each sentence reads.
MAX_CHARS = {"hook": 100, "bring": 110, "enjoy": 80}

SYSTEM = """\
You fill the four [blanks] in a candidate's cover letter for one job. The rest \
of the letter is fixed:

{template}

Rules:
- Each blank is one short phrase that reads naturally in its sentence: \
lowercase unless it starts with a proper noun, no ending punctuation, no \
quotes. Limits: {limits}.
- industry is the broad field from the list in the output schema, not the \
employer's niche.
- hook comes from <job_posting> and <company>. Make it \
specific to this employer (its product, mission, customers or a project the \
posting names), never praise that would fit any company, and don't repeat the \
company's name.
- bring and enjoy come only from <resume>, the candidate's checked resume for \
this job: name the two or three things the posting asks for most that the \
resume shows, not a list of every match. A checker \
rejects any number or technology that <resume> doesn't contain.
- Sound like a person: plain, warm and direct, no buzzwords.
"""


def asks_for_letter(posting: str) -> bool:
    """Whether the posting asks for a cover letter, read in code so no call is spent."""
    return any(
        not _DECLINED_BEFORE.search(posting[max(0, m.start() - _NEAR) : m.start()])
        and not _DECLINED_AFTER.search(posting[m.end() : m.end() + _NEAR])
        for m in _ASKS.finditer(posting)
    )


def long_date(day: date) -> str:
    """13th October 2026, as the template writes it."""
    n = day.day
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")  # noqa: PLR2004
    return f"{n}{suffix} {day:%B %Y}"


def _degree(facts: Facts) -> tuple[str, str]:
    """(school, 'a Bachelor of ... and a minor in ...') from the first education entry."""
    edu = facts.raw["education"][0]
    school = " ".join(str(edu["school"]).split())
    # "from the University of Washington", but "from Example State University"
    if re.match(r"(university|college|institute|school) of\b", school, re.IGNORECASE):
        school = f"the {school}"
    degree = re.sub(r",\s*minor in\s+", " and a minor in ", " ".join(edu["degree"].split()), flags=re.IGNORECASE)
    return school, f"{'an' if degree[:1].lower() in 'aeiou' else 'a'} {degree}"


def _fixed(facts: Facts, employer: str | None) -> dict[str, str]:
    school, degree = _degree(facts)
    return {"employer": "your company's" if employer is None else f"{employer}'s", "school": school, "degree": degree}


def compose(facts: Facts, phrases: Phrases, employer: str | None, day: date) -> dict[str, Any]:
    """The finished letter as template content."""
    contact = facts.raw["contact"]
    address = str(contact.get("address") or contact.get("location") or "").splitlines()
    filled = {k: _clean(v) for k, v in phrases.model_dump().items()}
    return {
        "name": contact["name"],
        "lines": [*[a.strip() for a in address if a.strip()], contact.get("phone"), contact.get("email")],
        "date": long_date(day),
        "greeting": "Dear Hiring Team," if employer is None else f"Dear {employer} Hiring Team,",
        "paragraphs": [p.format(**_fixed(facts, employer), **filled) for p in PARAGRAPHS],
        "closing": CLOSING,
    }


def as_text(letter: dict[str, Any]) -> str:
    """Plain text for pasting into an application form."""
    head = "\n".join([letter["name"], *filter(None, letter["lines"])])
    body = [letter["date"], letter["greeting"], *letter["paragraphs"], f"{letter['closing']}\n{letter['name']}"]
    return "\n\n".join([head, *body]) + "\n"


def _clean(phrase: str) -> str:
    # Typographic quotes and dashes can come out garbled in application forms; ASCII pastes anywhere.
    phrase = phrase.translate(_ASCII)
    return " ".join(phrase.split()).strip(" .,;:\"'")


def resume_context(t: Tailored, facts: Facts) -> str:
    """The tailored resume's strongest content, compactly: the only evidence the writer gets."""
    lines = []
    for pick in t.experience:
        if (e := facts.experience.get(pick.experience_id)) is not None:
            lines.append(f"{e['title']} at {e['org']} (stack: {', '.join(e['stack'])})")
            lines += [f"- {b.text}" for b in pick.bullets]
    for pick in t.projects[:TOP_PROJECTS]:
        if (p := facts.projects.get(pick.project_id)) is not None:
            lines.append(f"Project {p['name']} (stack: {', '.join(pick.stack)})")
            lines += [f"- {b.text}" for b in pick.bullets]
    lines.append("Skills: " + "; ".join(f"{s.category}: {', '.join(s.items)}" for s in t.skills if s.items))
    lines.append("Posting requirements it covers: " + "; ".join(t.matched_requirements))
    return "\n".join(lines)


def check(phrases: Phrases, *, about_job: str, about_candidate: str, facts: Facts) -> Report:
    """Errors for phrases that are too long, malformed or not backed by their sources."""
    rep = Report()
    sources = {"hook": about_job, "bring": about_candidate, "enjoy": about_candidate}
    vocab = tech_terms(facts)
    for key, source in sources.items():
        text = getattr(phrases, key)
        phrase = _clean(text)
        if not phrase:
            rep.errors.append(f"{key}: empty")
        if len(phrase) > MAX_CHARS[key]:
            rep.errors.append(f"{key}: {len(phrase)} chars, over the {MAX_CHARS[key]} limit")
        if re.search(r"[\[\]{}\n]", text):
            rep.errors.append(f"{key}: contains brackets or a line break")
        if extra := numbers(phrase) - numbers(source):
            rep.errors.append(f"{key}: numbers not in its source: {sorted(extra)}")
        rep.errors += [
            f"{key}: mentions {term!r}, which its source does not"
            for term, pattern in vocab
            if pattern.search(phrase) and not pattern.search(source)
        ]
    return rep


def ask(  # noqa: PLR0913
    posting: str,
    resume: str,
    facts: Facts,
    *,
    employer: str | None,
    company: str | None = None,
    via: llm.Backend = "plan",
    effort: llm.Effort = "high",
    max_attempts: int = 2,
) -> tuple[Phrases, Report, str]:
    """The four phrases, their check report, and the model that served them."""
    blanks = {k: f"[{k}]" for k in Phrases.model_fields}
    template = "\n\n".join(p.format(**_fixed(facts, employer), **blanks) for p in PARAGRAPHS)
    limits = ", ".join(f"{k} {n} characters" for k, n in MAX_CHARS.items())
    system = SYSTEM.format(template=template, limits=limits)
    excerpt = posting[:POSTING_CHARS]
    context = f"<job_posting>\n{excerpt}\n</job_posting>\n\n<resume>\n{resume}\n</resume>"
    if company is not None:
        context += f"\n\n<company>\n{company}\n</company>"
    about_job = f"{excerpt}\n{company or ''}"
    prompt = context
    for attempt in range(max_attempts):
        phrases, model = llm.call(system, prompt, Phrases, via=via, effort=effort)
        report = check(phrases, about_job=about_job, about_candidate=resume, facts=facts)
        if report.ok or attempt == max_attempts - 1:
            return phrases, report, model
        prompt = (
            f"{context}\n\n<previous_attempt>\n{phrases.model_dump_json(indent=1)}\n</previous_attempt>\n\n"
            "The checker rejected your previous attempt. Fix these and return all four phrases again:\n- "
            + "\n- ".join(report.errors)
        )
    msg = "unreachable"
    raise AssertionError(msg)


def write(  # noqa: PLR0913
    outdir: Path,
    facts: Facts,
    *,
    employer: str | None,
    via: llm.Backend = "plan",
    effort: llm.Effort = "high",
    today: date | None = None,
) -> Report:
    """Write cover_letter.{txt,json} next to the resume in outdir.

    Reads the posting, company profile and tailored resume that output.produce saved there.
    """
    company_file = outdir / "company.txt"
    employer = None if employer is None or not employer.strip() else employer.strip()
    tailored = Tailored.model_validate_json((outdir / "tailored.json").read_text())
    phrases, rep, model = ask(
        (outdir / "posting.txt").read_text(),
        resume_context(tailored, facts),
        facts,
        employer=employer,
        company=company_file.read_text() if company_file.exists() else None,
        via=via,
        effort=effort,
    )
    letter = compose(facts, phrases, employer, today or datetime.now().astimezone().date())
    # Built aside and moved in, the JSON last, so a half-written letter never looks finished.
    work = outdir / f".{NAME}.partial"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    (work / f"{NAME}.txt").write_text(as_text(letter))
    saved = {"phrases": phrases.model_dump(), "model": model, "errors": rep.errors, "warnings": rep.warnings}
    (work / f"{NAME}.json").write_text(json.dumps(saved, indent=2))
    for ext in ("txt", "json"):
        (work / f"{NAME}.{ext}").replace(outdir / f"{NAME}.{ext}")
    # Letters written before it was text only; a rewrite mustn't leave the old wording beside it.
    for ext in ("tex", "pdf"):
        (outdir / f"{NAME}.{ext}").unlink(missing_ok=True)
    shutil.rmtree(work, ignore_errors=True)
    return rep


def load(outdir: Path) -> dict[str, Any] | None:
    """The saved letter's phrases, model and check results, or None if there's no letter."""
    path = outdir / f"{NAME}.json"
    return json.loads(path.read_text()) if path.exists() else None
