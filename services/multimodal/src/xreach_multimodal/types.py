"""Small input and output types that keep file handling explicit."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class MediaAsset:
    data: bytes
    media_type: str
    filename: str = "upload"


@dataclass(frozen=True)
class Keyframe:
    label: str
    timestamp_seconds: float


@dataclass(frozen=True)
class ModalityFeature:
    modality: str
    embedding: tuple[float, ...]
    filename: str | None = None
    label: str | None = None
    timestamp_seconds: float | None = None


@dataclass(frozen=True)
class MultimodalResult:
    model_name: str
    embedding_dimension: int
    semantic_id: tuple[int, ...]
    features: tuple[ModalityFeature, ...]
    predictions: dict[str, float]
    modalities: tuple[str, ...]
    keyframes: tuple[Keyframe, ...]
    video_duration_seconds: float | None
    quantizer_status: str
    propensity_status: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["semantic_id"] = list(self.semantic_id)
        result["features"] = [asdict(feature) for feature in self.features]
        result["modalities"] = list(self.modalities)
        result["keyframes"] = [asdict(frame) for frame in self.keyframes]
        return result
