import json
from datetime import date
from pathlib import Path

import pytest

from tailor import db, letter, pipeline
from tailor.facts import Facts
from tailor.letter import Phrases
from tailor.schema import Bullet, ExperiencePick, ProjectPick, SkillLine, Tailored
from tailor.validate import Report

GOOD = Phrases(
    hook="mission to make home repairs painless",
    industry="consumer apps",
    bring="full-stack React and Firebase experience",
    enjoy="building tools that clients and contractors rely on",
)


def tailored() -> Tailored:
    return Tailored(
        fit_summary="",
        matched_requirements=["React"],
        gaps=[],
        experience=[
            ExperiencePick(
                experience_id="northwind",
                bullets=[Bullet(claim_id="nw-platform", text="Built a booking platform using React and Firebase.")],
            )
        ],
        projects=[
            ProjectPick(
                project_id="img-pipeline",
                reason="",
                stack=["AWS Lambda"],
                bullets=[Bullet(claim_id="ip-pipeline", text="Designed an event-driven pipeline on AWS Lambda.")],
            )
        ],
        skills=[SkillLine(category="Languages", items=["TypeScript"])],
    )


def test_asks_for_letter() -> None:
    assert letter.asks_for_letter("Required Documents - Resume - Letter of Interest - 3 References")
    assert letter.asks_for_letter("Please attach a cover letter. No phone calls, please.")
    assert letter.asks_for_letter("Cover letter (optional)")
    assert not letter.asks_for_letter("No cover letter needed, just apply.")
    assert not letter.asks_for_letter("A cover letter is not required.")
    assert not letter.asks_for_letter("We build letterpress tools. Cover the on-call rotation.")


def test_long_date() -> None:
    days = [1, 2, 3, 4, 11, 12, 13, 21, 22, 23]
    assert [letter.long_date(date(2026, 10, d)).split()[0] for d in days] == [
        "1st",
        "2nd",
        "3rd",
        "4th",
        "11th",
        "12th",
        "13th",
        "21st",
        "22nd",
        "23rd",
    ]
    assert letter.long_date(date(2026, 10, 13)) == "13th October 2026"


def test_compose_fills_the_template_from_facts() -> None:
    facts = Facts()
    out = letter.compose(facts, GOOD, "Acme", date(2026, 10, 13))
    assert out["greeting"] == "Dear Acme Hiring Team,"
    assert out["lines"] == ["Portland, OR", "555-0142", "alex.rivera@example.com"]
    assert out["paragraphs"][0].startswith("Good day! I saw your job post and Acme's mission to make home")
    assert "Example State University with a Bachelor of Science in Computer Science, I've" in out["paragraphs"][1]
    assert letter.compose(facts, GOOD, None, date(2026, 10, 13))["greeting"] == "Dear Hiring Team,"

    facts.raw["contact"]["address"] = "1 Main St\nPortland, OR 97201"
    facts.raw["education"][0]["degree"] = "Associate of Science, Minor in Mathematics"
    facts.raw["education"][0]["school"] = "University of Washington"
    out = letter.compose(facts, GOOD, "Acme", date(2026, 10, 13))
    assert out["lines"][:2] == ["1 Main St", "Portland, OR 97201"]
    assert "the University of Washington with an Associate of Science and a minor in" in out["paragraphs"][1]
    text = letter.as_text(out)
    assert text.startswith("Alex Rivera\n1 Main St\n")
    assert text.endswith("Sincerely,\nAlex Rivera\n")


def test_industry_must_be_a_broad_field() -> None:
    with pytest.raises(ValueError, match="industry"):
        Phrases.model_validate({**GOOD.model_dump(), "industry": "digital identity software"})


def test_check_rejects_unsupported_phrases() -> None:
    facts = Facts()
    resume = letter.resume_context(tailored(), facts)
    job = "We are a home services startup hiring a React engineer."
    assert letter.check(GOOD, about_job=job, about_candidate=resume, facts=facts).ok
    bad = GOOD.model_copy(update={"bring": "5 years of Kubernetes", "enjoy": "x" * 101, "hook": "[hook]"})
    errors = letter.check(bad, about_job=job, about_candidate=resume, facts=facts).errors
    assert any("bring: numbers" in e for e in errors)
    assert any("'Kubernetes'" in e for e in errors)
    assert any(e.startswith("enjoy: 101 chars") for e in errors)
    assert any(e.startswith("hook: contains brackets") for e in errors)


def test_write_retries_then_saves_the_letter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "posting.txt").write_text("Acme builds React apps for home services. Send a cover letter.")
    (tmp_path / "tailored.json").write_text(tailored().model_dump_json())
    answers = [GOOD.model_copy(update={"bring": "Kubernetes at scale"}), GOOD]
    prompts: list[str] = []

    def fake_call(_system: str, prompt: str, *_args: object, **_kwargs: object) -> tuple[Phrases, str]:
        prompts.append(prompt)
        return answers.pop(0), "fake"

    monkeypatch.setattr(letter.llm, "call", fake_call)
    rep = letter.write(tmp_path, Facts(), employer="Acme", today=date(2026, 10, 13))
    assert rep.ok
    assert "Kubernetes" in prompts[1]
    assert "<facts>" not in prompts[0]
    assert "Acme's mission to make home repairs painless" in (tmp_path / "cover_letter.txt").read_text()
    assert json.loads((tmp_path / "cover_letter.json").read_text())["errors"] == []
    assert not (tmp_path / ".cover_letter.partial").exists()


def test_generate_one_writes_a_letter_only_when_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.add_job(conn, id="t:1", source="test", title="Dev", company="Acme", posting="Include a cover letter.")
    db.add_job(conn, id="t:2", source="test", title="Dev", company="Beta", posting="Apply online.")
    monkeypatch.setattr(pipeline, "produce", lambda *_a, **_kw: (tmp_path, Report(), "fake"))
    written: list[str | None] = []
    monkeypatch.setattr(letter, "write", lambda _out, _facts, *, employer, **_kw: written.append(employer))
    for job_id in ("t:1", "t:2"):
        pipeline.generate_one(conn, job_id, Facts())
    pipeline.generate_one(conn, "t:1", Facts(), cover_letter=False)
    assert written == ["Acme"]
