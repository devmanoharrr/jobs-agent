from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

WorkMode = Literal["remote", "hybrid", "onsite"]
EmploymentType = Literal["full_time", "part_time", "contract", "internship", "temporary"]

WORK_MODES = ("remote", "hybrid", "onsite")
EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "internship", "temporary")


class ModelJobFacts(BaseModel):
    """Fields the model is allowed to fill. Salary and location stay with the code."""

    model_config = ConfigDict(extra="forbid")

    summary: str | None = None
    role_category: str | None = None
    skills: list[str] = Field(default_factory=list, max_length=20)
    work_mode: WorkMode | None = None
    employment_type: EmploymentType | None = None
    experience_min_years: float | None = None
    experience_label: str | None = None
    responsibilities: list[str] = Field(default_factory=list, max_length=20)
    requirements: list[str] = Field(default_factory=list, max_length=20)
    nice_to_have: list[str] = Field(default_factory=list, max_length=20)
    benefits: list[str] = Field(default_factory=list, max_length=20)
    description_text: str | None = None


def _nullable_string() -> dict:
    return {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _nullable_enum(values: tuple[str, ...]) -> dict:
    return {"anyOf": [{"type": "string", "enum": list(values)}, {"type": "null"}]}


def _string_list() -> dict:
    return {"type": "array", "items": {"type": "string"}}


MODEL_JSON_SCHEMA: dict = {
    "name": "job_posting_facts",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": _nullable_string(),
            "role_category": _nullable_string(),
            "skills": _string_list(),
            "work_mode": _nullable_enum(WORK_MODES),
            "employment_type": _nullable_enum(EMPLOYMENT_TYPES),
            "experience_min_years": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "experience_label": _nullable_string(),
            "responsibilities": _string_list(),
            "requirements": _string_list(),
            "nice_to_have": _string_list(),
            "benefits": _string_list(),
            "description_text": _nullable_string(),
        },
        "required": [
            "summary",
            "role_category",
            "skills",
            "work_mode",
            "employment_type",
            "experience_min_years",
            "experience_label",
            "responsibilities",
            "requirements",
            "nice_to_have",
            "benefits",
            "description_text",
        ],
    },
}
