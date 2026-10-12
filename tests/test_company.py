from pathlib import Path

import pytest

from tailor import company, db, generate, pipeline
from tailor.facts import Facts

COMPANY_PAGE = """
<section data-test-id="about-us">
  <p class="break-words" data-test-id="about-us__description">
      Square builds tools for sellers &amp; their customers.
  </p>
  <dl>
    <div data-test-id="about-us__industry">
      <dt class="font-sans">
          Industry
      </dt>
      <dd class="font-sans">
                  Software Development
      </dd>
    </div>
    <div data-test-id="about-us__size">
      <dt class="font-sans">Company size</dt>
      <dd class="font-sans">

        1,001-5,000 employees
      </dd>
    </div>
  </dl>
</section>
"""


def test_parse_linkedin_company() -> None:
    assert company.parse_linkedin_company(COMPANY_PAGE) == (
        "Industry: Software Development\n"
        "Company size: 1,001-5,000 employees\n"
        "\n"
        "Square builds tools for sellers & their customers."
    )
    assert company.parse_linkedin_company("<html>authwall</html>") is None


def test_profile_follows_the_job_page_company_link(monkeypatch: pytest.MonkeyPatch) -> None:
    job_page = (
        '<a class="topcard__org-name-link" data-tracking-will-navigate '
        'href="https://www.linkedin.com/company/joinsquare?trk=public_jobs_topcard-org-name">Square</a>'
    )
    pages = {
        "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/123": job_page,
        "https://www.linkedin.com/company/joinsquare": COMPANY_PAGE,
    }
    monkeypatch.setattr(company, "http_get", lambda url: (url, pages[url]))
    assert (company.profile({"id": "linkedin:123", "source": "linkedin"}) or "").startswith("Industry: Software")
    assert company.profile({"id": "hiringcafe:abc", "source": "hiringcafe"}) is None


def test_company_profile_is_looked_up_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.add_job(conn, id="linkedin:1", source="linkedin", title="Engineer", company="Acme", posting="x")
    calls: list[str] = []

    def down(job: dict[str, str]) -> str:
        calls.append(job["id"])
        msg = "network down"
        raise OSError(msg)

    monkeypatch.setattr(company, "profile", down)
    assert pipeline.company_profile(conn, db.get_required(conn, "linkedin:1")) is None
    assert db.get_required(conn, "linkedin:1")["company_info"] is None  # a failure is retried later

    monkeypatch.setattr(company, "profile", lambda job: calls.append(job["id"]) or "Industry: Robots")
    assert pipeline.company_profile(conn, db.get_required(conn, "linkedin:1")) == "Industry: Robots"
    assert pipeline.company_profile(conn, db.get_required(conn, "linkedin:1")) == "Industry: Robots"
    assert len(calls) == 2  # the failed lookup and the first success; the second read the database


def test_writer_prompt_includes_company(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    def capture(_system: str, prompt: str, *_args: object, **_kwargs: object) -> None:
        prompts.append(prompt)
        raise RuntimeError

    monkeypatch.setattr(generate.llm, "call", capture)
    with pytest.raises(RuntimeError):
        generate.tailor("Build robots", Facts(), company="Industry: Robots")
    assert "<job_posting>\nBuild robots\n</job_posting>\n\n<company>\nIndustry: Robots\n</company>" in prompts[0]
