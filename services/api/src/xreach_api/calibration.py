"""Load and validate configured Phoenix-score percentile calibration."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ReachCalibrationConfig:
    method: str
    reference_scores: tuple[float, ...] = ()
    log_mean: float | None = None
    log_std_dev: float | None = None

    @classmethod
    def from_environment(cls) -> ReachCalibrationConfig | None:
        path = os.environ.get("XREACH_REACH_CALIBRATION_FILE")
        return cls.from_file(path) if path else None

    @classmethod
    def from_file(cls, path: str | Path) -> ReachCalibrationConfig:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"could not read reach calibration file: {error}") from error
        if not isinstance(value, dict):
            raise ValueError("reach calibration file must contain a JSON object")
        method = value.get("method")
        if method == "empirical":
            scores = value.get("reference_scores")
            if (
                not isinstance(scores, list)
                or len(scores) < 2
                or len(scores) > 1_000_000
                or any(
                    isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    for score in scores
                )
            ):
                raise ValueError(
                    "empirical calibration needs 2 to 1000000 finite reference scores"
                )
            try:
                normalized_scores = tuple(map(float, scores))
            except (OverflowError, ValueError) as error:
                raise ValueError("empirical calibration scores must be finite") from error
            if not all(math.isfinite(score) for score in normalized_scores):
                raise ValueError("empirical calibration scores must be finite")
            return cls(method="empirical", reference_scores=normalized_scores)
        if method == "log_normal":
            log_mean = value.get("log_mean")
            log_std_dev = value.get("log_std_dev")
            if (
                isinstance(log_mean, bool)
                or isinstance(log_std_dev, bool)
                or not isinstance(log_mean, (int, float))
                or not isinstance(log_std_dev, (int, float))
            ):
                raise ValueError(
                    "log-normal calibration needs finite log_mean and positive log_std_dev"
                )
            try:
                log_mean = float(log_mean)
                log_std_dev = float(log_std_dev)
            except (OverflowError, ValueError) as error:
                raise ValueError(
                    "log-normal calibration needs finite log_mean and positive log_std_dev"
                ) from error
            if (
                not math.isfinite(log_mean)
                or not math.isfinite(log_std_dev)
                or log_std_dev <= 0
            ):
                raise ValueError(
                    "log-normal calibration needs finite log_mean and positive log_std_dev"
                )
            return cls(
                method=method,
                log_mean=log_mean,
                log_std_dev=log_std_dev,
            )
        raise ValueError("reach calibration method must be empirical or log_normal")

    def to_payload(self) -> dict[str, Any]:
        if self.method == "empirical":
            return {
                "method": self.method,
                "reference_scores": list(self.reference_scores),
            }
        return {
            "method": self.method,
            "log_mean": self.log_mean,
            "log_std_dev": self.log_std_dev,
        }
