"""Load the facts profile and build the resume content the template renders."""

from dataclasses import dataclass
from typing import Any

from . import github, profile

Entry = dict[str, Any]
SECTIONS = ("contact", "education", "experience", "projects", "skills")


@dataclass(frozen=True)
class Claim:
    id: str
    text: str
    entry_id: str  # experience or project id this claim belongs to


class Facts:
    def __init__(self, raw: dict[str, Any] | None = None) -> None:
        self.raw: dict[str, Any] = profile.load("facts") if raw is None else raw
        for section in SECTIONS:
            if section not in self.raw:
                raise KeyError(section)
        self.experience: dict[str, Entry] = {e["id"]: e for e in self.raw["experience"]}
        self.projects: dict[str, Entry] = {p["id"]: p for p in self.raw["projects"]}
        self.claims: dict[str, Claim] = {}
        for entry in [*self.raw["experience"], *self.raw["projects"]]:
            for c in entry["claims"]:
                if c["id"] in self.claims:
                    msg = f"duplicate claim id {c['id']!r}"
                    raise ValueError(msg)
                self.claims[c["id"]] = Claim(c["id"], " ".join(c["text"].split()), entry["id"])

    def entry(self, entry_id: str) -> Entry:
        return self.experience.get(entry_id) or self.projects[entry_id]

    def skill_pool(self) -> dict[str, Entry]:
        """Every skill the resume may list, with its evidence.

        A skill qualifies if it's claimed in the facts, used in a facts
        entry's stack, or evidenced by a public GitHub repo. Returns
        skill -> {"category": claimed category or None, "evidence": [...]}.
        """
        pool: dict[str, Entry] = {}
        by_lower: dict[str, str] = {}

        def add(skill: str, evidence: str, category: str | None = None) -> None:
            key = by_lower.setdefault(skill.lower(), skill)
            entry = pool.setdefault(key, {"category": None, "evidence": []})
            entry["category"] = entry["category"] or category
            entry["evidence"].append(evidence)

        for category, items in self.raw["skills"].items():
            for s in items:
                add(s, "claimed", category)
        for e in [*self.raw["experience"], *self.raw["projects"]]:
            for s in e["stack"]:
                add(s, f"used in {e['id']}")
        for repo, info in github.load().items():
            for s, why in info["skills"].items():
                add(s, f"github {repo}: {why}")
        return pool

    def base_resume(self) -> Entry:
        """The untailored resume: every claim verbatim, in facts order."""
        return {
            "contact": self.raw["contact"],
            "education": self.raw["education"],
            "experience": [
                {**e, "bullets": [self.claims[c["id"]].text for c in e["claims"]]} for e in self.raw["experience"]
            ],
            "projects": [
                {**p, "bullets": [self.claims[c["id"]].text for c in p["claims"]]} for p in self.raw["projects"]
            ],
            "skills": self.raw["skills"],
        }
