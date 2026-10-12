"""Turn job-alert emails into job leads, and leads into full posting text.

Each parser takes an email's text and returns leads: dicts of job fields
(id, source, title, company, ...). Fetchers fill in the full posting.
"""

import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any, ClassVar, cast, override

Lead = dict[str, Any]

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
MIN_POSTING_CHARS = 600  # shorter than this and we assume we got a login wall or a stub
HOST_DELAY = 3.0  # seconds between requests to one host; LinkedIn 429s on bursts
HTTP_TOO_MANY_REQUESTS = 429
CARD_FIELDS = ("title", "company", "location")
CLOSED = {"status": "filtered", "filter_reason": "posting closed"}
HTTP_GONE = frozenset({404, 410})
_AGO = re.compile(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago")
_AGO_DAYS = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}


def iso_utc(when: datetime) -> str:
    """The form posted_at is stored in, so dates compare correctly as strings."""
    return when.astimezone(UTC).isoformat(timespec="seconds")


class _Text(HTMLParser):
    BLOCK: ClassVar[frozenset[str]] = frozenset(
        {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}
    )
    SKIP: ClassVar[frozenset[str]] = frozenset(
        {"script", "style", "noscript", "svg", "head", "nav", "footer", "form", "button"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    @override
    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


def html_to_text(markup: str) -> str:
    parser = _Text()
    parser.feed(markup)
    lines = (re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in "".join(parser.out).splitlines())
    text = "\n".join(line for line in lines if line and line != "-")
    return re.sub(r"\n{3,}", "\n\n", text)


_last_hit: dict[str, float] = {}


def http_get(url: str, timeout: int = 30, retries: int = 2) -> tuple[str, str]:
    """Return (final url after redirects, body).

    Only http(s) URLs are fetched: links come from email content, and urllib would
    otherwise happily open file:// URLs. Requests to one host are spaced out, and
    HTTP 429 responses are retried with backoff.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        msg = f"refusing to fetch non-http(s) URL: {url[:100]}"
        raise ValueError(msg)
    host = parts.hostname or ""
    for attempt in range(retries + 1):
        wait = _last_hit.get(host, 0) + HOST_DELAY - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en"})  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                body = cast("bytes", r.read())
                return str(r.url), body.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code != HTTP_TOO_MANY_REQUESTS or attempt == retries:
                raise
            time.sleep(int(e.headers.get("Retry-After") or 0) or 15 * (attempt + 1))
    msg = "unreachable"
    raise AssertionError(msg)


def strip_tracking(url: str) -> str:
    """Drop utm_* query parameters, keeping every other parameter."""
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if not k.startswith("utm_")]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def hash_id(*parts: str | None) -> str:
    """Short stable id for sources without one (not a security hash)."""
    joined = "|".join(p or "" for p in parts).lower()
    return hashlib.sha1(joined.encode(), usedforsecurity=False).hexdigest()[:12]


LINKEDIN_JOB_ID = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")
_LI_VIEW = re.compile(r"View job:\s*\S*?/jobs/view/(\d+)\S*")
# Lines LinkedIn puts around a job card that aren't title/company/location.
_LI_NOISE = re.compile(
    r"https?://|^-{5,}|^<|^(see all|edit alert|manage alerts|new jobs|your job alert)", re.IGNORECASE
)
_LI_BADGE = re.compile(
    r"actively hiring|school alum|connection|apply with|easy apply|promoted|early applicant"
    r"|^\d+ (applicants|alumni)|^\$[\d.,]+[KkMm]?",
    re.IGNORECASE,
)


def parse_linkedin(text: str) -> list[Lead]:
    """Parse LinkedIn alert emails, both digests and single-job alerts.

    Job cards look like `Title / Company / Location / [badges...] / View job: <url>`.
    The job id is the key; the fetcher replaces these fields with the ones on
    LinkedIn's job page.
    """
    leads: dict[str, Lead] = {}
    prev_end = 0
    for m in _LI_VIEW.finditer(text):
        lines = [line.strip() for line in text[prev_end : m.start()].splitlines()]
        prev_end = m.end()
        lines = [line for line in lines if line and not _LI_NOISE.search(line)]
        salary = next((line for line in lines if line.startswith("$")), None)
        card = [line for line in lines if not _LI_BADGE.search(line)][-len(CARD_FIELDS) :]
        fields: Lead = dict(zip(CARD_FIELDS, card, strict=True)) if len(card) == len(CARD_FIELDS) else {}
        if salary is not None:
            fields["salary"] = salary
        leads.setdefault(m.group(1), fields)
    for job_id in LINKEDIN_JOB_ID.findall(text):
        leads.setdefault(job_id, {})
    return [
        {"id": f"linkedin:{jid}", "source": "linkedin", "url": f"https://www.linkedin.com/jobs/view/{jid}", **fields}
        for jid, fields in leads.items()
    ]


_HC_BLOCK = re.compile(r"^\s*•\s+(.+?)\n\s+Posted ([^\n]+)\n\s+(.+?)\n\s+Apply: (\S+)", re.MULTILINE)


def parse_hiringcafe(text: str) -> list[Lead]:
    """Parse `• Title — Company — Location / Posted <date> · <salary> / <summary> / Apply: <url>`.

    The company is sometimes missing (`• Title — Location`).
    """
    leads = []
    for header, posted, summary, apply_url in _HC_BLOCK.findall(text):
        parts = [p.strip() for p in header.split(" — ")]
        title = parts[0]
        company = parts[1] if len(parts) >= len(CARD_FIELDS) else None
        location = parts[-1] if len(parts) > 1 else None
        posted_parts = [p.strip() for p in posted.split(" · ")]
        try:
            posted_at = iso_utc(datetime.strptime(posted_parts[0], "%B %d, %Y").replace(tzinfo=UTC))
        except ValueError:
            posted_at = None
        leads.append(
            {
                "id": f"hiringcafe:{hash_id(title, company, location)}",
                "source": "hiringcafe",
                "title": title,
                "company": company,
                "location": location,
                "salary": posted_parts[1] if len(posted_parts) > 1 else None,
                "summary": summary.strip(),
                "apply_url": apply_url,
                "posted_at": posted_at,
            }
        )
    return leads


Parser = Callable[[str], list[Lead]]
PARSERS: dict[str, Parser] = {
    "jobalerts-noreply@linkedin.com": parse_linkedin,
    "jobs-noreply@linkedin.com": parse_linkedin,
    "ali@hiring.cafe": parse_hiringcafe,
}


def parser_for(sender: str) -> Parser | None:
    addr = re.search(r"[\w.+-]+@[\w.-]+", sender or "")
    return PARSERS.get(addr.group(0).lower()) if addr else None


def fetch_linkedin(job_id: str) -> Lead:
    """Read LinkedIn's public (logged-out) job page fragment."""
    _, page = http_get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}")

    def grab(pattern: str) -> str | None:
        m = re.search(pattern, page, re.DOTALL)
        return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else None

    desc = re.search(r"show-more-less-html__markup[^>]*>(.*?)</div>", page, re.DOTALL)
    criteria = re.findall(r'job-criteria-subheader">\s*(.*?)\s*</h3>\s*<span[^>]*>\s*(.*?)\s*</span>', page, re.DOTALL)
    header = "\n".join(f"{k.strip()}: {html.unescape(v.strip())}" for k, v in criteria)
    # Closed jobs keep their full description; only this banner says they're closed.
    if "closed-job" in page:
        return dict(CLOSED)
    posted_at = None
    if ago := _AGO.search(grab(r"posted-time-ago__text[^>]*>(.*?)</span>") or ""):
        days = int(ago.group(1)) * _AGO_DAYS[ago.group(2)]
        posted_at = iso_utc(datetime.now(UTC) - timedelta(days=days))
    return {
        "posted_at": posted_at,
        "title": grab(r"top-card-layout__title[^>]*>(.*?)</h2>"),
        "company": grab(r"topcard__org-name-link[^>]*>(.*?)</a>") or grab(r'topcard__flavor">(.*?)</span>'),
        "location": grab(r'topcard__flavor topcard__flavor--bullet">(.*?)</span>'),
        "posting": (header + "\n\n" + html_to_text(desc.group(1))).strip() if desc else None,
    }


def _json_ld_posting(page: str) -> dict[str, Any] | None:
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page, re.DOTALL):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _first(value: object) -> object:
    return value[0] if isinstance(value, list) and value else value


def _ld_name(org: object) -> str | None:
    org = _first(org)
    if isinstance(org, str):
        return org
    name = org.get("name") if isinstance(org, dict) else None
    return name if isinstance(name, str) else None


def _ld_place(location: object) -> str | None:
    loc = _first(location)
    addr = _first(loc.get("address")) if isinstance(loc, dict) else None
    if isinstance(addr, str):
        return addr
    if not isinstance(addr, dict):
        return None
    parts: list[str] = [p for p in (addr.get("addressLocality"), addr.get("addressRegion")) if isinstance(p, str) and p]
    return ", ".join(parts) if parts else None


def _ld_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def fetch_generic(url: str) -> Lead:
    """Fetch any careers/ATS page.

    Prefers schema.org JobPosting JSON-LD (Greenhouse, Lever, Ashby and most career
    sites embed it), else the page's visible text. Bot-protected pages (hiring.cafe
    is behind Cloudflare) are not worked around: we keep the resolved URL and let
    the caller fall back to the email summary.
    """
    try:
        final_url, page = http_get(url)
    except urllib.error.HTTPError as e:
        return dict(CLOSED) if e.code in HTTP_GONE else {"url": e.url}
    out: Lead = {"url": final_url}
    if (ld := _json_ld_posting(page)) is not None:
        if (until := _ld_date(ld.get("validThrough"))) is not None and until < datetime.now(UTC):
            return dict(CLOSED)
        posted = _ld_date(ld.get("datePosted"))
        out["posted_at"] = iso_utc(posted) if posted else None
        title, desc = ld.get("title"), ld.get("description")
        place = _ld_place(ld.get("jobLocation"))
        out |= {
            "title": title if isinstance(title, str) else None,
            "company": _ld_name(ld.get("hiringOrganization")),
            "location": place or ("Remote" if ld.get("jobLocationType") == "TELECOMMUTE" else None),
            "posting": html_to_text(html.unescape(desc)) if isinstance(desc, str) else None,
        }
    else:
        out["posting"] = html_to_text(page)
    return out


def fetch_posting(job: Lead) -> Lead:
    """Fields to update on the job, including posting_ok."""
    if job["source"] == "linkedin":
        got = fetch_linkedin(job["id"].split(":", 1)[1])
    else:
        got = fetch_generic(job.get("apply_url") or job["url"])
    if got.get("status") == "filtered":
        return got
    got = {k: v for k, v in got.items() if v}
    # The alert email's date is an upper bound on when the job was posted; keep the earlier.
    if job.get("posted_at") and got.get("posted_at"):
        got["posted_at"] = min(job["posted_at"], got["posted_at"])
    if "url" in got:
        got["url"] = strip_tracking(got["url"])
    ok = len(got.get("posting") or "") >= MIN_POSTING_CHARS
    if not ok:  # keep whatever the email said so triage still has something
        got["posting"] = "\n".join(filter(None, [got.get("posting"), job.get("summary")]))
    # LinkedIn's job page is authoritative; for other sources the email is (ATS pages
    # often have generic titles), and scraped fields only fill what the email lacked.
    email_fields = {k: job[k] for k in CARD_FIELDS if job.get(k)}
    merged = {**email_fields, **got} if job["source"] == "linkedin" else {**got, **email_fields}
    return {**merged, "posting_ok": int(ok)}


def still_open(job: Lead) -> bool | None:
    """Whether the posting still takes applications; None if the page couldn't be read."""
    try:
        got = fetch_posting(job)
    except urllib.error.HTTPError as e:
        return False if e.code in HTTP_GONE else None
    except (OSError, ValueError):
        return None
    return bool(got.get("status") != "filtered")
