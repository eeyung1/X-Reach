"""Lazy adapter from API uploads to the sibling multimodal package."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol


@dataclass(frozen=True)
class MediaUpload:
    data: bytes
    media_type: str
    filename: str


class MultimodalBackend(Protocol):
    def extract(self, text: str, media: Sequence[MediaUpload]) -> Mapping[str, Any]: ...


class MultimodalUnavailable(RuntimeError):
    """The local inference package or its OpenCLIP runtime is unavailable."""


class MultimodalService:
    """Load OpenCLIP only when an automatic analysis request needs it."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._extractor: Any | None = None
        self._media_asset_type: Any | None = None

    def extract(self, text: str, media: Sequence[MediaUpload]) -> Mapping[str, Any]:
        extractor, media_asset_type = self._backend()
        assets = tuple(
            media_asset_type(
                data=upload.data,
                media_type=upload.media_type,
                filename=upload.filename,
            )
            for upload in media
        )
        try:
            result = extractor.extract(text, assets)
        except (TypeError, ValueError):
            raise
        except RuntimeError as error:
            raise MultimodalUnavailable(str(error)) from error
        payload = result.to_dict() if hasattr(result, "to_dict") else result
        if not isinstance(payload, Mapping):
            raise MultimodalUnavailable(
                "multimodal extractor returned an invalid result"
            )
        return payload

    def _backend(self) -> tuple[Any, Any]:
        if self._extractor is not None and self._media_asset_type is not None:
            return self._extractor, self._media_asset_type
        with self._lock:
            if self._extractor is not None and self._media_asset_type is not None:
                return self._extractor, self._media_asset_type
            try:
                from xreach_multimodal import MediaAsset, MultimodalExtractor

                extractor = MultimodalExtractor()
            except (ImportError, RuntimeError) as error:
                raise MultimodalUnavailable(
                    "Multimodal inference is unavailable. Install xreach-multimodal "
                    "with its inference extra and configure local OpenCLIP weights."
                ) from error
            self._extractor = extractor
            self._media_asset_type = MediaAsset
            return extractor, MediaAsset
