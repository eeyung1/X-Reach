"""Database records that do not belong to an HTTP request contract."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from ..models import DraftInputRecord


class DraftRegistrationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    creator_id: str = Field(min_length=1, max_length=64)
    baseline_24h_impressions: StrictInt | None = Field(default=None, ge=0)
    draft: DraftInputRecord

    @field_validator("creator_id")
    @classmethod
    def require_nonblank_creator_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("creator_id must not be blank")
        return value


class CreatorBaselineRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    creator_id: str = Field(min_length=1, max_length=64)
    distribution: Literal["log_normal"] = "log_normal"
    observation_count: StrictInt = Field(gt=0)
    mu: float = Field(allow_inf_nan=False)
    sigma: float = Field(ge=0, allow_inf_nan=False)
    impression_floor: StrictInt = Field(ge=1)
    mean_impressions: float = Field(ge=0, allow_inf_nan=False)
    median_impressions: float = Field(ge=0, allow_inf_nan=False)
    format_breakdown: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("creator_id")
    @classmethod
    def require_nonblank_creator_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("creator_id must not be blank")
        return value
