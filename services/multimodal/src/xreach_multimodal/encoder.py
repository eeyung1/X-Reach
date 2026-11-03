"""Lazy OpenCLIP ViT-B/32 adapter; importing the package does not load weights."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
from PIL import Image


class OpenClipEncoder:
    def __init__(
        self,
        model_name: str = "ViT-B-32",
        pretrained: str = "laion2b_s34b_b79k",
        device: str | None = None,
        *,
        model: Any | None = None,
        preprocess: Any | None = None,
        tokenizer: Any | None = None,
        torch_module: Any | None = None,
    ) -> None:
        try:
            if torch_module is None:
                import torch as torch_module  # type: ignore[no-redef]
            if model is None or preprocess is None or tokenizer is None:
                import open_clip

                loaded_model, _, loaded_preprocess = open_clip.create_model_and_transforms(
                    model_name, pretrained=pretrained
                )
                model = model or loaded_model
                preprocess = preprocess or loaded_preprocess
                tokenizer = tokenizer or open_clip.get_tokenizer(model_name)
        except ImportError as error:
            raise RuntimeError(
                "Install the inference extra with `pip install -e '.[inference]'` "
                "to load OpenCLIP."
            ) from error

        self.model_name = model_name
        self.pretrained = pretrained
        self.torch = torch_module
        self.device = device or ("cuda" if torch_module.cuda.is_available() else "cpu")
        self.model = model.to(self.device).eval()
        self.preprocess = preprocess
        self.tokenizer = tokenizer
        self.embedding_dimension = int(
            getattr(getattr(self.model, "visual", None), "output_dim", 512)
        )

    def encode_text(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.embedding_dimension), dtype=np.float32)
        tokens = self.tokenizer(list(texts)).to(self.device)
        with self.torch.inference_mode():
            features = self.model.encode_text(tokens)
        return self._normalize(features)

    def encode_images(self, images: Sequence[Image.Image]) -> np.ndarray:
        if not images:
            return np.empty((0, self.embedding_dimension), dtype=np.float32)
        batch = self.torch.stack([self.preprocess(image.convert("RGB")) for image in images])
        with self.torch.inference_mode():
            features = self.model.encode_image(batch.to(self.device))
        return self._normalize(features)

    def _normalize(self, features: Any) -> np.ndarray:
        features = features.float()
        features = features / features.norm(dim=-1, keepdim=True).clamp(min=1e-12)
        return features.detach().cpu().numpy().astype(np.float32, copy=False)
