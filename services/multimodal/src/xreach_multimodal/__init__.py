"""Multimodal draft feature extraction for X-Reach."""

from .extractor import MultimodalExtractor
from .quantizer import ResidualVectorQuantizer
from .types import MediaAsset, ModalityFeature, MultimodalResult

__all__ = [
    "MediaAsset",
    "ModalityFeature",
    "MultimodalExtractor",
    "MultimodalResult",
    "ResidualVectorQuantizer",
]
