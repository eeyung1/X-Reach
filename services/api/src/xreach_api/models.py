"""Validated request and response models for the scoring API."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Predictions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    favorite: float | None = Field(default=None, ge=0, le=1)
    reply: float | None = Field(default=None, ge=0, le=1)
    retweet: float | None = Field(default=None, ge=0, le=1)
    photo_expand: float | None = Field(default=None, ge=0, le=1)
    video_open: float | None = Field(default=None, ge=0, le=1)
    click: float | None = Field(default=None, ge=0, le=1)
    open_link: float | None = Field(default=None, ge=0, le=1)
    share: float | None = Field(default=None, ge=0, le=1)
    share_via_dm: float | None = Field(default=None, ge=0, le=1)
    share_via_copy_link: float | None = Field(default=None, ge=0, le=1)
    dwell: float | None = Field(default=None, ge=0, le=1)
    quote: float | None = Field(default=None, ge=0, le=1)
    follow_author: float | None = Field(default=None, ge=0, le=1)
    dwell_seconds: float | None = Field(default=None, ge=0, le=86_400)
    click_dwell_seconds: float | None = Field(default=None, ge=0, le=86_400)
    not_interested: float | None = Field(default=None, ge=0, le=1)
    block_author: float | None = Field(default=None, ge=0, le=1)
    mute_author: float | None = Field(default=None, ge=0, le=1)
    report: float | None = Field(default=None, ge=0, le=1)
    not_dwelled: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def require_at_least_one_prediction(self) -> Predictions:
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("at least one predicted action head is required")
        return self


SafetyLabel = Literal[
    "spam_high_recall",
    "do_not_amplify",
    "nsfw_high_recall",
    "malicious_url",
]


class CandidateOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    author_id: int = Field(default=0, ge=0, le=18_446_744_073_709_551_615)
    is_reply: bool = False
    is_retweet: bool = False
    is_mutual_follow_author: bool = False
    in_network: bool | None = None
    cold_start_lift_to_rank: int | None = Field(default=None, ge=0)
    visibility_labels: list[SafetyLabel] = Field(default_factory=list)
    has_nsfw_media: bool = False
    has_untrusted_external_url: bool = False
    oon_multiplier: float = Field(default=1.0, ge=0, le=100)


class CandidateInput(CandidateOptions):
    predictions: Predictions


class AnalyzeRequest(CandidateInput):
    text: str = Field(default="", max_length=280)
    media_attached: bool = False

    @model_validator(mode="after")
    def require_draft_content(self) -> AnalyzeRequest:
        if not self.text.strip() and not self.media_attached:
            raise ValueError("text or attached media is required")
        return self


class AutomaticAnalyzeRequest(CandidateOptions):
    text: str = Field(default="", max_length=280)
    media_attached: bool = False

    @model_validator(mode="after")
    def require_draft_content(self) -> AutomaticAnalyzeRequest:
        if not self.text.strip() and not self.media_attached:
            raise ValueError("text or attached media is required")
        return self


class PlacementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=280)
    author_id: int = Field(default=0, ge=0, le=18_446_744_073_709_551_615)
    profile_predictions: Predictions
    direct_root_reply_predictions: Predictions
    is_mutual_follow_author: bool = False
    oon_multiplier: float = Field(default=1.0, ge=0, le=100)
    profile_visibility_labels: list[SafetyLabel] = Field(default_factory=list)
    direct_root_reply_visibility_labels: list[SafetyLabel] = Field(default_factory=list)
    profile_has_nsfw_media: bool = False
    direct_root_reply_has_nsfw_media: bool = False
    profile_has_untrusted_external_url: bool = False
    direct_root_reply_has_untrusted_external_url: bool = False


class ScoreOutput(BaseModel):
    weighted_score: float
    final_score: float
    author_diversity_multiplier: float
    out_of_network_multiplier: float
    dropped_by: list[str]


class KeyframeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    timestamp_seconds: float = Field(ge=0)


class MultimodalOutput(BaseModel):
    # Embeddings/features are intentionally omitted from the public API payload.
    model_config = ConfigDict(extra="ignore")

    model_name: str
    embedding_dimension: int = Field(gt=0)
    semantic_id: list[int]
    predictions: Predictions
    modalities: list[str]
    keyframes: list[KeyframeOutput] = Field(default_factory=list)
    video_duration_seconds: float | None = Field(default=None, ge=0)
    quantizer_status: str
    propensity_status: str

    @field_validator("semantic_id", mode="before")
    @classmethod
    def validate_semantic_id(cls, value: object) -> object:
        if (
            not isinstance(value, list)
            or len(value) != 6
            or any(
                not isinstance(code, int)
                or isinstance(code, bool)
                or not 0 <= code < 256
                for code in value
            )
        ):
            raise ValueError("semantic_id must contain six codes from 0 through 255")
        return value


class AnalyzeResponse(BaseModel):
    algorithmic_viability_score: float
    reach_potential_percentile: float | None = Field(default=None, ge=0, le=100)
    reach_calibration_status: Literal["unavailable", "empirical", "log_normal"] = (
        "unavailable"
    )
    score: ScoreOutput
    placement_recommendation: dict[str, str]
    suggested_tweaks: list[str]
    model_name: str | None = None
    semantic_id: list[int] | None = None
    predicted_probabilities: Predictions | None = None
    modalities: list[str] = Field(default_factory=list)
    keyframes: list[KeyframeOutput] = Field(default_factory=list)
    video_duration_seconds: float | None = None
    quantizer_status: str | None = None
    propensity_status: str | None = None


class RadarIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[dict[str, object]] = Field(min_length=1, max_length=500)


class AlertRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    min_views_per_minute: float = Field(default=500, ge=0, allow_inf_nan=False)
    max_age_minutes: float = Field(default=30, gt=0, allow_inf_nan=False)
    simclusters: list[str] = Field(default_factory=list, max_length=100)


class AlertWebhookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    webhook_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    url: str = Field(min_length=1, max_length=2048)
    secret: str = Field(min_length=1, max_length=4096)
    format: Literal["generic", "discord", "slack"] = "generic"

    @field_validator("url")
    @classmethod
    def validate_webhook_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("webhook URL must use HTTP or HTTPS and include a host")
        if parsed.username or parsed.password:
            raise ValueError("webhook URL must not contain credentials")
        try:
            parsed.port
        except ValueError as error:
            raise ValueError("webhook URL must include a valid port") from error
        return value


class AlertConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[AlertRuleConfig] = Field(max_length=100)
    webhooks: list[AlertWebhookConfig] = Field(max_length=100)

    @model_validator(mode="after")
    def require_unique_ids(self) -> AlertConfigurationRequest:
        if len({rule.rule_id for rule in self.rules}) != len(self.rules):
            raise ValueError("alert rule IDs must be unique")
        if len({target.webhook_id for target in self.webhooks}) != len(self.webhooks):
            raise ValueError("webhook IDs must be unique")
        return self


class AlertConfigurationResponse(BaseModel):
    rules: list[AlertRuleConfig]
    webhooks: list[dict[str, str]]


class AlertTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1, max_length=80)
    seed: dict[str, Any]
