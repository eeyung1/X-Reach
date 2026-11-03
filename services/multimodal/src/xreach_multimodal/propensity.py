"""Transparent zero-shot prompt proxy for Phoenix action propensity heads."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np


ACTION_PRIORS = {
    "favorite": 0.20,
    "reply": 0.05,
    "retweet": 0.02,
    "photo_expand": 0.08,
    "video_open": 0.08,
    "click": 0.10,
    "open_link": 0.05,
    "share": 0.03,
    "share_via_dm": 0.01,
    "share_via_copy_link": 0.01,
    "dwell": 0.35,
    "quote": 0.01,
    "follow_author": 0.005,
    "not_interested": 0.05,
    "block_author": 0.001,
    "mute_author": 0.002,
    "report": 0.001,
    "not_dwelled": 0.30,
}


class PromptPropensityEstimator:
    """Semantic similarity proxy, not a trained or calibrated engagement model."""

    def __init__(self, temperature: float = 4.0) -> None:
        self.temperature = temperature
        self._prompt_embeddings: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def prepare(self, encode_text) -> None:
        prompts = []
        for action in ACTION_PRIORS:
            prompts.extend(
                [
                    f"A viewer is likely to {action.replace('_', ' ')} this post.",
                    f"A viewer is unlikely to {action.replace('_', ' ')} this post.",
                ]
            )
        vectors = np.asarray(encode_text(prompts), dtype=np.float32)
        if vectors.shape[0] != len(prompts) or vectors.ndim != 2:
            raise ValueError("text encoder returned the wrong number of prompt embeddings")
        vectors = self._normalize_rows(vectors)
        self._prompt_embeddings = {
            action: (vectors[index * 2], vectors[index * 2 + 1])
            for index, action in enumerate(ACTION_PRIORS)
        }

    def predict(
        self,
        embedding: np.ndarray,
        *,
        text: str,
        media_count: int,
        has_image: bool,
        video_duration_seconds: float | None,
        has_video: bool,
    ) -> dict[str, float]:
        if not self._prompt_embeddings:
            raise RuntimeError("call prepare(encode_text) before predicting propensities")
        vector = self._normalize_rows(np.asarray(embedding, dtype=np.float32).reshape(1, -1))[0]
        predictions: dict[str, float] = {}
        for action, prior in ACTION_PRIORS.items():
            positive, negative = self._prompt_embeddings[action]
            semantic_delta = float(np.dot(vector, positive) - np.dot(vector, negative))
            prior_logit = math.log(prior / (1.0 - prior))
            logit = prior_logit + self.temperature * semantic_delta
            predictions[action] = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, logit))))
        if not has_video:
            predictions["video_open"] = 0.0
        if not has_image:
            predictions["photo_expand"] = 0.0
        word_count = len(text.split())
        dwell = min(30.0, 1.0 + 0.12 * min(word_count, 100) + 1.5 * media_count)
        if has_video:
            dwell = min(60.0, dwell + min(video_duration_seconds or 0.0, 20.0) * 0.25)
        predictions["dwell_seconds"] = dwell
        predictions["click_dwell_seconds"] = dwell * (0.5 + 0.5 * predictions["click"])
        return predictions

    @staticmethod
    def _normalize_rows(values: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(values, axis=1, keepdims=True)
        return values / np.maximum(norms, 1e-12)


class LinearPropensityHead:
    """Multilabel logistic head that can be fitted on Phoenix training labels."""

    def __init__(self, weights: np.ndarray, bias: np.ndarray) -> None:
        self.weights = np.asarray(weights, dtype=np.float32)
        self.bias = np.asarray(bias, dtype=np.float32)
        if self.weights.ndim != 2 or self.weights.shape[0] != len(ACTION_PRIORS):
            raise ValueError(f"weights must have shape ({len(ACTION_PRIORS)}, dimension)")
        if self.bias.shape != (len(ACTION_PRIORS),):
            raise ValueError(f"bias must have shape ({len(ACTION_PRIORS)},)")
        if not np.isfinite(self.weights).all() or not np.isfinite(self.bias).all():
            raise ValueError("propensity head checkpoint must be finite")

    @classmethod
    def load(cls, path: str) -> LinearPropensityHead:
        with np.load(path) as archive:
            return cls(archive["weights"], archive["bias"])

    @classmethod
    def fit(
        cls,
        embeddings: np.ndarray,
        targets: np.ndarray,
        *,
        epochs: int = 300,
        learning_rate: float = 0.1,
        l2: float = 1e-4,
    ) -> LinearPropensityHead:
        """Fit one sigmoid head per Phoenix action from labeled examples.

        `targets` columns follow the insertion order of `ACTION_PRIORS`. These
        outputs still require a held-out calibration step before production
        percentile estimates can be claimed.
        """
        features = np.asarray(embeddings, dtype=np.float32)
        labels = np.asarray(targets, dtype=np.float32)
        expected_heads = len(ACTION_PRIORS)
        if features.ndim != 2 or features.shape[0] == 0:
            raise ValueError("embeddings must be a non-empty matrix")
        if labels.shape != (features.shape[0], expected_heads):
            raise ValueError(f"targets must have shape (n, {expected_heads})")
        if not np.isfinite(features).all() or not np.isfinite(labels).all():
            raise ValueError("training arrays must be finite")
        if np.any((labels < 0) | (labels > 1)):
            raise ValueError("target probabilities must be between 0 and 1")
        if epochs < 1 or learning_rate <= 0 or l2 < 0:
            raise ValueError("epochs and learning_rate must be positive; l2 must be non-negative")

        norms = np.maximum(np.linalg.norm(features, axis=1, keepdims=True), 1e-12)
        features = features / norms
        weights = np.zeros((expected_heads, features.shape[1]), dtype=np.float32)
        prevalence = np.clip(labels.mean(axis=0), 1e-4, 1.0 - 1e-4)
        bias = np.log(prevalence / (1.0 - prevalence)).astype(np.float32)
        for _ in range(epochs):
            logits = features @ weights.T + bias
            probabilities = 1.0 / (1.0 + np.exp(-np.clip(logits, -30, 30)))
            residual = (probabilities - labels) / features.shape[0]
            weights -= learning_rate * (residual.T @ features + l2 * weights)
            bias -= learning_rate * residual.sum(axis=0)
        return cls(weights, bias)

    def save(self, path: str) -> None:
        np.savez_compressed(path, weights=self.weights, bias=self.bias)

    def predict(
        self,
        embedding: np.ndarray,
        *,
        text: str = "",
        media_count: int = 0,
        has_image: bool = False,
        video_duration_seconds: float | None = None,
        has_video: bool = False,
    ) -> dict[str, float]:
        vector = np.asarray(embedding, dtype=np.float32)
        if vector.shape != (self.weights.shape[1],):
            raise ValueError("embedding dimension does not match propensity checkpoint")
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(vector).all() or norm <= 1e-12:
            raise ValueError("embedding is empty or non-finite")
        vector = vector / norm
        logits = self.weights @ vector + self.bias
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(logits, -30, 30)))
        predictions = {
            action: float(probability)
            for action, probability in zip(ACTION_PRIORS, probabilities, strict=True)
        }
        if not has_video:
            predictions["video_open"] = 0.0
        if not has_image:
            predictions["photo_expand"] = 0.0
        word_count = len(text.split())
        dwell = min(30.0, 1.0 + 0.12 * min(word_count, 100) + 1.5 * media_count)
        if has_video:
            dwell = min(60.0, dwell + min(video_duration_seconds or 0.0, 20.0) * 0.25)
        predictions["dwell_seconds"] = dwell
        predictions["click_dwell_seconds"] = dwell * (0.5 + 0.5 * predictions["click"])
        return predictions
