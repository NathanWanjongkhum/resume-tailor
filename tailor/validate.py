"""Deterministic checks that a tailored resume makes no claim absent from the facts.

Errors block the resume (the model gets one retry with the errors as feedback);
warnings are shown to the reviewer.
"""

import re
from dataclasses import dataclass, field

from .facts import Facts
from .schema import Bullet, ExperiencePick, ProjectPick, SkillLine, Tailored

# Technologies a model might plausibly slip into a bullet. Anything in this list,
# the skills section, or any project stack counts as a "tech term": it may appear
# in a bullet only if the cited claim or that entry's stack already mentions it.
EXTRA_TECH = [
    "Kubernetes",
    "Terraform",
    "Kafka",
    "Redis",
    "MongoDB",
    "GraphQL",
    "Django",
    "Flask",
    "FastAPI",
    "Spring",
    "Angular",
    "Next.js",
    "Express",
    "TensorFlow",
    "scikit-learn",
    "Pandas",
    "NumPy",
    "Spark",
    "Airflow",
    "Azure",
    "CI/CD",
    "Jenkins",
    "GitHub Actions",
    "Linux",
    "REST",
    "gRPC",
    "microservices",
    "DynamoDB",
    "EC2",
    "ECS",
    "EKS",
    "SQS",
    "SNS",
    "Go",
    "Kotlin",
    "Swift",
    "Ruby",
    "Scala",
    "LLM",
    "Transformer",
    "RAG",
    "Embedded",
    "RTOS",
    "CUDA",
    "OpenGL",
    "Unity",
    "Tableau",
    "Snowflake",
    "BigQuery",
    "Postgres",
]

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*\s*%?")
MAX_BULLET_CHARS = 230


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def term_re(term: str) -> re.Pattern[str]:
    """Whole-word pattern for a tech term.

    Terms with capitals ("REST", "Go", "Express", "Spring") match case-sensitively, so
    ordinary prose ("the rest of", "go live", "expressed") isn't read as a technology.
    All-lowercase terms ("microservices") match any case.
    """
    flags = 0 if term != term.lower() else re.IGNORECASE
    return re.compile(rf"(?<![\w+#]){re.escape(term)}(?![\w+#])", flags)


def numbers(text: str) -> set[str]:
    return {n.replace(" ", "") for n in _NUM_RE.findall(text)}


def tech_terms(facts: Facts) -> list[tuple[str, re.Pattern[str]]]:
    """Every technology a generated sentence might mention, with its pattern."""
    vocab = set(facts.skill_pool()) | set(EXTRA_TECH)
    for e in [*facts.experience.values(), *facts.projects.values()]:
        vocab |= set(e["stack"])
    # Single letters ("C", "R") match too much prose to be useful.
    return [(v, term_re(v)) for v in sorted(vocab) if len(v) > 1]


class _Checker:
    def __init__(self, facts: Facts) -> None:
        self.facts = facts
        self.rep = Report()
        self.used: set[str] = set()
        self.vocab = tech_terms(facts)

    def bullets(self, entry_id: str, bullets: list[Bullet]) -> None:
        stack_text = " ".join(self.facts.entry(entry_id)["stack"])
        for b in bullets:
            where = f"{entry_id}/{b.claim_id}"
            claim = self.facts.claims.get(b.claim_id)
            if claim is None:
                self.rep.errors.append(f"{where}: unknown claim id")
                continue
            if claim.entry_id != entry_id:
                self.rep.errors.append(f"{where}: claim belongs to {claim.entry_id}, not {entry_id}")
            if b.claim_id in self.used:
                self.rep.errors.append(f"{where}: claim cited twice")
            self.used.add(b.claim_id)
            if extra := numbers(b.text) - numbers(claim.text) - numbers(stack_text):
                self.rep.errors.append(f"{where}: numbers not in the source claim: {sorted(extra)}")
            for term, pattern in self.vocab:
                if pattern.search(b.text) and not pattern.search(claim.text) and not pattern.search(stack_text):
                    self.rep.errors.append(f"{where}: mentions {term!r}, which the claim does not support")
            if len(b.text) > MAX_BULLET_CHARS:
                self.rep.warnings.append(f"{where}: {len(b.text)} chars, may wrap to a third line")

    def experience(self, picks: list[ExperiencePick]) -> None:
        seen = set()
        for pick in picks:
            if pick.experience_id not in self.facts.experience:
                self.rep.errors.append(f"unknown experience id {pick.experience_id!r}")
                continue
            seen.add(pick.experience_id)
            self.bullets(pick.experience_id, pick.bullets)
        for missing in self.facts.experience.keys() - seen:
            self.rep.warnings.append(f"experience {missing!r} was dropped")

    def projects(self, picks: list[ProjectPick]) -> None:
        for pick in picks:
            if pick.project_id not in self.facts.projects:
                self.rep.errors.append(f"unknown project id {pick.project_id!r}")
                continue
            if extra := set(pick.stack) - set(self.facts.projects[pick.project_id]["stack"]):
                self.rep.errors.append(f"{pick.project_id}: stack items not in facts: {sorted(extra)}")
            self.bullets(pick.project_id, pick.bullets)

    def skills(self, lines: list[SkillLine]) -> None:
        categories = set(self.facts.raw["skills"])
        pool = {s.lower() for s in self.facts.skill_pool()}
        listed: set[str] = set()
        for line in lines:
            if line.category not in categories:
                self.rep.errors.append(f"skills: unknown category {line.category!r}")
            if extra := [s for s in line.items if s.lower() not in pool]:
                self.rep.errors.append(f"skills/{line.category}: no evidence in facts or GitHub: {extra}")
            if dupes := [s for s in line.items if s.lower() in listed]:
                self.rep.errors.append(f"skills/{line.category}: listed twice: {dupes}")
            listed |= {s.lower() for s in line.items}
        items = [s for line in lines for s in line.items]
        for s in items:
            if covered_by := [o for o in items if o != s and term_re(s).search(o)]:
                self.rep.warnings.append(f"skills: {s!r} may be redundant next to {covered_by}")


def validate(t: Tailored, facts: Facts) -> Report:
    check = _Checker(facts)
    check.experience(t.experience)
    check.projects(t.projects)
    check.skills(t.skills)
    return check.rep
