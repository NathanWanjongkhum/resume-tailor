"""What the employer does, from its public LinkedIn page, so the writer can rank for it.

Only LinkedIn jobs are covered: their job page links the company's page, which LinkedIn
serves logged out with an About text and a few labeled facts. Other sources have no
reliable link to the company.
"""

import html
import re

from .sources import Lead, http_get

MAX_CHARS = 3_000  # an About text is a paragraph or two; this bounds odd pages
FIELDS = ("Industry", "Company size", "Headquarters", "Type", "Founded", "Specialties")
_COMPANY_URL = re.compile(r'topcard__org-name-link[^>]*href="(https://www\.linkedin\.com/company/[^"?/]+)')


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_linkedin_company(page: str) -> str | None:
    """`Industry: ...` lines, then the About text; None if the page has neither."""
    found = ((f, re.search(rf"<dt[^>]*>\s*{f}\s*</dt>\s*<dd[^>]*>(.*?)</dd>", page, re.DOTALL)) for f in FIELDS)
    lines = [f"{field}: {_text(m.group(1))}" for field, m in found if m]
    if m := re.search(r'data-test-id="about-us__description"[^>]*>(.*?)</p>', page, re.DOTALL):
        about = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        lines.append(f"\n{about}")
    return "\n".join(lines).strip()[:MAX_CHARS] or None


def profile(job: Lead) -> str | None:
    """The company's public profile as plain text, or None when there isn't one to read."""
    if job["source"] != "linkedin":
        return None
    _, page = http_get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job['id'].split(':', 1)[1]}")
    if not (m := _COMPANY_URL.search(page)):
        return None
    _, page = http_get(m.group(1))
    return parse_linkedin_company(page)
