"""Decide which jobs deserve a tailored resume.

1. Hard filters from the preferences, in code (free).
2. Survivors are ranked in code (free) by how many of the candidate's skills the
   posting mentions, and only the top `triage.max_per_run` go to Claude; the rest
   stay queued and compete again next run.
3. Claude scores those in batches, 1-5, for relevance (how many of the posting's
   required qualifications the facts meet) and desirability (fit to `wants` / `avoid`).
4. Recency (1-5, from when the job was posted) is computed in code when ranking,
   since it changes every day.
"""

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import llm, profile
from .facts import Facts
from .progress import Progress
from .validate import term_re

Prefs = dict[str, Any]
Job = dict[str, Any]
POSTING_CHARS = 4000  # per job in a triage batch; postings are long and mostly boilerplate


def load_prefs() -> Prefs:
    return profile.load("preferences")


def hard_filter(job: Job, prefs: Prefs) -> str | None:
    """Reason the job is excluded, or None to keep it."""
    f = prefs["hard_filters"]
    title, company, location = job.get("title") or "", job.get("company") or "", job.get("location") or ""
    for pat in f.get("title_exclude", []):
        if re.search(pat, title, re.IGNORECASE):
            return f"title matches {pat!r}"
    for pat in f.get("company_exclude", []):
        if re.search(pat, company, re.IGNORECASE):
            return f"company matches {pat!r}"
    places = f.get("location_include", [])
    if places and location and not any(re.search(p, location, re.IGNORECASE) for p in places):
        return f"location {location!r} not in location_include"
    most = f.get("max_years_required")
    if most is not None and (years := years_required(job.get("posting") or "")) is not None and years > most:
        return f"requires {years}+ years of experience"
    return None


_NUMBER = r"\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten"
_WORDS = {w: i for i, w in enumerate(_NUMBER.split("|")[1:], 1)}
# "2+ years", "2-6 years", "2 to 6 years", "at least two years", "minimum of 3 yrs",
# followed closely by "experience", which skips "a 4-year degree" or "founded 20 years ago".
_YEARS = re.compile(
    rf"\b({_NUMBER})\s*\+?\s*(?:(?:[-\u2013]|to)\s*(?:{_NUMBER})\s*)?\+?\s*(?:years?|yrs?)\b[^.\n]{{0,60}}?\bexperience",
    re.IGNORECASE,
)
# Lines that only prefer the years, or offer them in place of a degree, don't require them.
_OPTIONAL = re.compile(r"\b(prefer\w*|nice to have|bonus|a plus|ideally|in lieu|if associate)", re.IGNORECASE)


def years_required(posting: str) -> int | None:
    """Fewest years of experience the posting asks for, or None if it names none.

    The fewest, since postings for several levels list each level's years.
    """
    found = [
        int(n) if n.isdigit() else _WORDS[n.lower()]
        for line in posting.splitlines()
        if not _OPTIONAL.search(line)
        for n in _YEARS.findall(line)
    ]
    return min(found, default=None)


# Free signals for ranking. On the first 81 scored full postings, ordering by these put
# 8 of the 15 that qualified for a resume in the top 24 (random: about 4); a plain count
# of skill matches put 6, since long senior postings mention the most technologies.
SOFTWARE_TITLE = re.compile(
    r"\b(software|developer|engineer|programmer|full[- ]?stack|backend|frontend|web)\b", re.IGNORECASE
)
YEARS_3_PLUS = re.compile(r"\b([3-9]|1\d)\+?\s*(?:-\s*\d+\s*)?(?:years|yrs)", re.IGNORECASE)
ENTRY_LEVEL = re.compile(
    r"\b(entry[- ]level|new grad|graduate|junior|jr\.?|early career|associate|recent grad)", re.IGNORECASE
)


# Days since posting at or under which a job gets 5, 4, 3, 2 for recency; older gets 1.
RECENCY_DAYS = (2, 5, 10, 20)


def recency(posted_at: str | None, now: datetime | None = None) -> int:
    """1-5: how recently the job was posted. Unknown dates count as the oldest."""
    if posted_at is None or not posted_at:
        return 1
    age = ((now or datetime.now(UTC)) - datetime.fromisoformat(posted_at)).total_seconds() / 86400
    return 5 - next((i for i, days in enumerate(RECENCY_DAYS) if age <= days), len(RECENCY_DAYS))


def total_score(job: Job | sqlite3.Row) -> int:
    """Relevance + desirability + recency (3-15); the generation order."""
    return int(job["relevance"] or 0) + int(job["desirability"] or 0) + recency(job["posted_at"])


def rank(jobs: list[Job], facts: Facts) -> list[Job]:
    """Jobs most worth a model call first.

    Full postings come before email summaries, which can't be generated automatically
    (`require_full_posting`) and are too short to rank. Then: a software title, no
    3+ years required, entry-level wording, skill matches, and newest first.
    """
    patterns = [term_re(s) for s in facts.skill_pool() if len(s) > 1]

    def key(job: Job) -> tuple[bool, bool, bool, bool, int, str]:
        title, posting = job.get("title") or "", job.get("posting") or job.get("summary") or ""
        return (
            bool(job.get("posting_ok")),
            bool(SOFTWARE_TITLE.search(title)),
            not YEARS_3_PLUS.search(posting),
            bool(ENTRY_LEVEL.search(f"{title}\n{posting[:1500]}")),
            sum(1 for p in patterns if p.search(f"{title}\n{posting}")),
            str(job.get("posted_at") or ""),
        )

    return sorted(jobs, key=key, reverse=True)


class Score(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    relevance: int = Field(description="1-5: how many of the posting's required qualifications the candidate meets")
    desirability: int = Field(description="1-5: how well the job matches the candidate's wants and avoids")
    reasons: str = Field(description="one or two sentences covering both scores")
    red_flags: list[str] = Field(
        description="e.g. years of experience required, clearance, unpaid, vague recruiter repost"
    )


class Scores(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scores: list[Score]


SYSTEM = """\
You triage job postings for one candidate. For each job, score relevance \
(how many of the posting's required qualifications does the candidate meet, \
counting each minimum years of experience, degree and must-have skill? 3 or \
more only if they meet most of them; preferred or nice-to-have items don't \
count against them) and desirability (does it match what the candidate \
wants and avoids?), 1-5 each. Be calibrated: 5 means apply today, 3 is a coin flip, \
1 is a waste of time. Postings marked INCOMPLETE are only an email \
summary, so judge them on what's there and note the uncertainty.

<candidate>
{candidate}
</candidate>

<wants>
{wants}
</wants>

<avoid>
{avoid}
</avoid>
"""


def _candidate_summary(facts: Facts) -> str:
    lines = [f"Education: {e['degree']}, {e['school']} ({e['dates']})" for e in facts.raw["education"]]
    lines += [
        f"Experience: {e['title']} at {e['org']} ({e['dates']}); stack: {', '.join(e['stack'])}"
        for e in facts.raw["experience"]
    ]
    lines += [f"Project: {p['name']}; stack: {', '.join(p['stack'])}" for p in facts.raw["projects"]]
    lines.append("Skills with evidence: " + ", ".join(facts.skill_pool()))
    return "\n".join(lines)


def score(
    jobs: list[Job], facts: Facts, prefs: Prefs, progress: Progress | None = None
) -> Iterator[tuple[dict[str, Score], str]]:
    """Score jobs in batches, yielding (scores keyed by job id, model label) per batch.

    Yielding per batch lets the caller save each batch's results before the next
    call, so one failed call doesn't throw away the batches before it.
    """
    t = prefs["triage"]
    system = SYSTEM.format(candidate=_candidate_summary(facts), wants=prefs["wants"], avoid=prefs["avoid"])
    progress = progress or Progress()
    size = t["batch_size"]
    for i in range(0, len(jobs), size):
        batch = jobs[i : i + size]
        progress.item(
            f"batch {i // size + 1} of {-(-len(jobs) // size)}: "
            + ", ".join(f"{j.get('company') or '?'}" for j in batch)
        )
        prompt = "\n\n".join(
            f'<job id="{j["id"]}">\nTitle: {j.get("title")}\nCompany: {j.get("company")}\n'
            f"Location: {j.get('location')}\nSalary: {j.get('salary') or 'not listed'}\n"
            f"{'' if j.get('posting_ok') else 'INCOMPLETE (email summary only)'}\n"
            f"{(j.get('posting') or j.get('summary') or '')[:POSTING_CHARS]}\n</job>"
            for j in batch
        )
        prompt += f"\n\nScore all {len(batch)} jobs, using each job's id exactly."
        out, model = llm.call(
            system,
            prompt,
            Scores,
            via=llm.backend(t["backend"]),
            model=t["model"],
            effort=llm.effort(t["effort"]),
        )
        ids = {j["id"] for j in batch}
        yield {s.job_id: s for s in out.scores if s.job_id in ids}, model
