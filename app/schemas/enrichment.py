from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorkMode(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class IndiaEligibility(StrEnum):
    INDIA = "india"
    REMOTE_INDIA = "remote_india"
    NOT_INDIA = "not_india"
    UNKNOWN = "unknown"


class Enrichment(BaseModel):
    """Facts an enrichment response is allowed to contain. Extra keys are invalid."""

    model_config = ConfigDict(extra="forbid")

    normalized_title: str | None
    role_category: str | None
    seniority: str | None
    skills: list[str] = Field(max_length=20)
    experience_min: float | None
    experience_max: float | None
    work_mode: WorkMode | None
    india_eligibility: IndiaEligibility

    @field_validator("skills")
    @classmethod
    def skills_are_explicit(cls, skills: list[str]) -> list[str]:
        cleaned: list[str] = []
        for skill in skills:
            text = skill.strip()
            if not text:
                raise ValueError("skills must be explicit")
            cleaned.append(text)
        return cleaned

    @model_validator(mode="after")
    def experience_range_is_ordered(self) -> "Enrichment":
        if (
            self.experience_min is not None
            and self.experience_max is not None
            and self.experience_min > self.experience_max
        ):
            raise ValueError("experience_min cannot exceed experience_max")
        return self
