"""Structured output the tailoring model must return."""

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Bullet(_Strict):
    claim_id: str = Field(description="id of the facts claim this bullet restates")
    text: str = Field(description="the bullet as it should appear on the resume")


class ExperiencePick(_Strict):
    experience_id: str
    bullets: list[Bullet]


class ProjectPick(_Strict):
    project_id: str
    reason: str = Field(description="one sentence: why this project fits the posting")
    stack: list[str] = Field(description="subset of the project's stack, most relevant first")
    bullets: list[Bullet]


class SkillLine(_Strict):
    category: str
    items: list[str]


class Tailored(_Strict):
    fit_summary: str = Field(description="2-3 sentences for the reviewer: how the resume was angled and why")
    matched_requirements: list[str] = Field(description="posting requirements the resume supports")
    gaps: list[str] = Field(description="posting requirements the facts do not support; never papered over")
    experience: list[ExperiencePick]
    projects: list[ProjectPick]
    skills: list[SkillLine]
