"""Ask Claude to tailor the resume to a posting, retrying once on validation errors."""

import yaml

from . import llm
from .facts import Facts
from .schema import Tailored
from .validate import Report, validate

MAX_POSTING_CHARS = 30_000  # real postings are far shorter; this bounds scraped page dumps

SYSTEM = """\
You tailor a candidate's resume to one job posting. The candidate's verified \
facts are below in YAML. They are the only source of truth.

How to tailor:
- Experience: keep every entry and every bullet, ordered most relevant first.
- Projects: include every project, ordered by fit, each with all of its \
claims ordered most relevant first. The renderer fills one page and trims \
from the bottom, so ranking matters more than count.
- Each bullet restates exactly one claim and cites its id. You may tighten \
the wording, lead with what the posting cares about, and use the posting's \
terminology where it means the same thing. Keep each bullet under 220 \
characters.
- Never add a number, metric, technology, scale, or outcome that the cited \
claim or its entry's stack does not state. A reviewer's checker rejects \
bullets that do, so an honest gap is better than an embellished bullet.
- Skills: choose only from <skill_pool>, which lists what the candidate \
claims or has shown in a project or public GitHub repo. List only skills \
relevant to this posting, ranked from most to least important to the \
posting, in the categories from the facts (place uncategorized skills in \
the best fit). Don't pad rows with irrelevant skills: each row is cut from \
its end to fit one line. Order rows by relevance to the posting too. Don't \
list a skill another listed item already covers (e.g. PostgreSQL next to \
SQL (MySQL/PostgreSQL)).
- <company>, when given, is the employer's public profile. Use it only to \
judge what this employer values when ranking entries, claims and skills \
(e.g. its domain, product or size); it is never a source for a bullet.
- List posting requirements the facts don't support under `gaps`. A skill \
shown only by GitHub evidence supports the skills section, not a bullet.
- Keep the resume parseable by applicant tracking systems: no nested \
parentheticals, and no stray punctuation in skill lists. Keep standard \
acronyms recruiters search for (CNN, LSTM, AWS, API, SQL, REST, CI/CD, IMU) \
as they are; spell out only uncommon or domain-internal acronyms.

<facts>
{facts}
</facts>

<skill_pool>
{skill_pool}
</skill_pool>
"""


def tailor(  # noqa: PLR0913
    job_posting: str,
    facts: Facts,
    *,
    company: str | None = None,
    via: llm.Backend = "plan",
    effort: llm.Effort = "high",
    max_attempts: int = 2,
) -> tuple[Tailored, Report, str]:
    """Return the tailored content, its validation report, and the model that served it."""
    system = SYSTEM.format(
        facts=yaml.safe_dump(facts.raw, sort_keys=False), skill_pool=yaml.safe_dump(facts.skill_pool(), sort_keys=False)
    )
    context = f"<job_posting>\n{job_posting[:MAX_POSTING_CHARS]}\n</job_posting>"
    if company is not None:
        context += f"\n\n<company>\n{company}\n</company>"
    prompt = context

    for attempt in range(max_attempts):
        tailored, model = llm.call(system, prompt, Tailored, via=via, effort=effort)
        report = validate(tailored, facts)
        if report.ok or attempt == max_attempts - 1:
            return tailored, report, model
        prompt = (
            f"{context}\n\n"
            f"<previous_attempt>\n{tailored.model_dump_json(indent=1)}\n</previous_attempt>\n\n"
            "The checker rejected these items in your previous attempt. Fix them by "
            "staying closer to the cited claims, then return the full resume again:\n- " + "\n- ".join(report.errors)
        )
    msg = "unreachable"
    raise AssertionError(msg)
