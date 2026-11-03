"""Text/image/video embedding, RVQ, and Phoenix-head estimate pipeline."""

from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO
from typing import Any, Protocol

import numpy as np
from PIL import Image, UnidentifiedImageError

from .encoder import OpenClipEncoder
from .propensity import PromptPropensityEstimator
from .quantizer import ResidualVectorQuantizer
from .types import Keyframe, MediaAsset, ModalityFeature, MultimodalResult
from .video import DecodedKeyframe, extract_keyframes


MAX_MEDIA_BYTES = 50 * 1024 * 1024
MAX_MEDIA_ITEMS = 4
MAX_IMAGE_PIXELS = 40_000_000


class Encoder(Protocol):
    model_name: str
    embedding_dimension: int

    def encode_text(self, texts: Sequence[str]) -> np.ndarray: ...

    def encode_images(self, images: Sequence[Image.Image]) -> np.ndarray: ...


class MultimodalExtractor:
    def __init__(
        self,
        encoder: Encoder | None = None,
        quantizer: ResidualVectorQuantizer | None = None,
        propensity_estimator: Any | None = None,
        *,
        video_decoder=extract_keyframes,
        max_media_bytes: int = MAX_MEDIA_BYTES,
        max_media_items: int = MAX_MEDIA_ITEMS,
    ) -> None:
        self.encoder = encoder or OpenClipEncoder()
        dimension = self.encoder.embedding_dimension
        self.quantizer = quantizer or ResidualVectorQuantizer(dimension=dimension)
        if self.quantizer.dimension != dimension:
            raise ValueError("RVQ and OpenCLIP embedding dimensions must match")
        self.propensity_estimator = propensity_estimator or PromptPropensityEstimator()
        self.video_decoder = video_decoder
        self.max_media_bytes = max_media_bytes
        self.max_media_items = max_media_items
        if isinstance(self.propensity_estimator, PromptPropensityEstimator):
            self.propensity_estimator.prepare(self.encoder.encode_text)

    def extract(self, text: str, media: Sequence[MediaAsset] = ()) -> MultimodalResult:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if len(text) > 280:
            raise ValueError("text exceeds the 280 character limit")
        if not text.strip() and not media:
            raise ValueError("provide non-empty text or at least one media file")
        if len(media) > self.max_media_items:
            raise ValueError(f"at most {self.max_media_items} media files are allowed")

        modality_vectors: list[np.ndarray] = []
        modalities: list[str] = []
        features: list[ModalityFeature] = []
        keyframes: list[Keyframe] = []
        image_count = 0
        video_count = 0
        video_durations: list[float] = []
        image_metadata: list[tuple[str, str | None, str | None, float | None]] = []

        if text.strip():
            text_vector = self._single_embedding(self.encoder.encode_text([text]), "text")
            modality_vectors.append(text_vector)
            modalities.append("text")
            features.append(
                ModalityFeature("text", tuple(float(value) for value in text_vector))
            )

        images: list[Image.Image] = []
        for asset in media:
            self._validate_asset(asset)
            normalized_type = asset.media_type.lower().split(";", maxsplit=1)[0].strip()
            if normalized_type.startswith("image/"):
                images.append(self._decode_image(asset.data))
                image_metadata.append(("image", asset.filename, None, None))
                image_count += 1
                modalities.append("image")
            elif normalized_type.startswith("video/"):
                decoded, duration = self.video_decoder(asset.data)
                if not decoded:
                    raise ValueError(f"{asset.filename} has no decodable video frames")
                images.extend(frame.image for frame in decoded)
                image_metadata.extend(
                    ("video_frame", asset.filename, frame.label, frame.timestamp_seconds)
                    for frame in decoded
                )
                keyframes.extend(
                    Keyframe(frame.label, frame.timestamp_seconds) for frame in decoded
                )
                video_durations.append(duration)
                video_count += 1
                modalities.append("video")
            else:
                raise ValueError(f"unsupported media type: {asset.media_type}")

        if images:
            raw_image_vectors = np.asarray(
                self.encoder.encode_images(images), dtype=np.float32
            )
            if raw_image_vectors.shape != (len(images), self.encoder.embedding_dimension):
                raise ValueError("image encoder returned an unexpected feature shape")
            image_vectors = np.stack([self._normalize(vector) for vector in raw_image_vectors])
            image_mean = self._normalize(image_vectors.mean(axis=0))
            modality_vectors.append(image_mean)
            features.extend(
                ModalityFeature(
                    modality=modality,
                    embedding=tuple(float(value) for value in vector),
                    filename=filename,
                    label=label,
                    timestamp_seconds=timestamp,
                )
                for vector, (modality, filename, label, timestamp) in zip(
                    image_vectors, image_metadata, strict=True
                )
            )

        fused = self._normalize(np.mean(np.stack(modality_vectors), axis=0))
        semantic_id, _ = self.quantizer.encode(fused)
        duration = max(video_durations) if video_durations else None
        predictions = self.propensity_estimator.predict(
            fused,
            text=text,
            media_count=image_count + video_count,
            has_image=image_count > 0,
            video_duration_seconds=duration,
            has_video=video_count > 0,
        )
        self._validate_predictions(predictions)
        propensity_status = (
            "uncalibrated_prompt_proxy"
            if isinstance(self.propensity_estimator, PromptPropensityEstimator)
            else "linear_head_uncalibrated"
        )
        return MultimodalResult(
            model_name=self.encoder.model_name,
            embedding_dimension=self.encoder.embedding_dimension,
            semantic_id=semantic_id,
            features=tuple(features),
            predictions=predictions,
            modalities=tuple(dict.fromkeys(modalities)),
            keyframes=tuple(keyframes),
            video_duration_seconds=duration,
            quantizer_status=self.quantizer.status,
            propensity_status=propensity_status,
        )

    def _validate_asset(self, asset: MediaAsset) -> None:
        if not isinstance(asset.data, bytes) or not asset.data:
            raise ValueError(f"{asset.filename} is empty or invalid")
        if len(asset.data) > self.max_media_bytes:
            raise ValueError(f"{asset.filename} exceeds the media size limit")

    @staticmethod
    def _decode_image(data: bytes) -> Image.Image:
        try:
            image = Image.open(BytesIO(data))
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("image dimensions exceed the pixel limit")
            image.load()
            return image.convert("RGB")
        except (UnidentifiedImageError, OSError) as error:
            raise ValueError(f"could not decode image: {error}") from error

    @staticmethod
    def _single_embedding(values: np.ndarray, modality: str) -> np.ndarray:
        vectors = np.asarray(values, dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[0] != 1:
            raise ValueError(f"{modality} encoder returned an unexpected feature shape")
        return MultimodalExtractor._normalize(vectors[0])

    @staticmethod
    def _normalize(vector: np.ndarray) -> np.ndarray:
        value = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(value))
        if not np.isfinite(value).all() or norm <= 1e-12:
            raise ValueError("embedding is empty or non-finite")
        return value / norm

    @staticmethod
    def _validate_predictions(predictions: dict[str, float]) -> None:
        if not isinstance(predictions, dict):
            raise TypeError("propensity estimator must return a dictionary")
        required = {
            "favorite", "reply", "retweet", "photo_expand", "video_open", "click",
            "open_link", "share", "share_via_dm", "share_via_copy_link", "dwell",
            "quote", "follow_author", "not_interested", "block_author", "mute_author",
            "report", "not_dwelled", "dwell_seconds", "click_dwell_seconds",
        }
        missing = required - predictions.keys()
        if missing:
            raise ValueError(f"propensity estimator omitted heads: {sorted(missing)}")
        for name in required:
            value = float(predictions[name])
            if not np.isfinite(value) or value < 0:
                raise ValueError(f"prediction {name} must be finite and non-negative")
            if name not in {"dwell_seconds", "click_dwell_seconds"} and value > 1:
                raise ValueError(f"probability head {name} must be between 0 and 1")
