"""Six-stage residual vector quantizer with train/load/save support."""

from __future__ import annotations

from pathlib import Path

import numpy as np


class ResidualVectorQuantizer:
    def __init__(
        self,
        dimension: int = 512,
        levels: int = 6,
        codebook_size: int = 256,
        *,
        codebooks: np.ndarray | None = None,
        seed: int = 2026,
    ) -> None:
        if dimension < 1 or levels < 1 or codebook_size < 2:
            raise ValueError("dimension/levels must be positive and codebook_size >= 2")
        self.dimension = dimension
        self.levels = levels
        self.codebook_size = codebook_size
        if codebooks is None:
            rng = np.random.default_rng(seed)
            codebooks = rng.normal(
                0.0, 1.0 / np.sqrt(dimension),
                (levels, codebook_size, dimension),
            ).astype(np.float32)
            self.status = "bootstrap_untrained"
        else:
            self.status = "loaded"
        self.codebooks = self._validate_codebooks(codebooks)

    def encode(self, embedding: np.ndarray) -> tuple[tuple[int, ...], np.ndarray]:
        residual = self._validate_vector(embedding).copy()
        indices: list[int] = []
        reconstruction = np.zeros_like(residual)
        for codebook in self.codebooks:
            index = self._nearest(residual, codebook)
            centroid = codebook[index]
            indices.append(index)
            reconstruction += centroid
            residual -= centroid
        return tuple(indices), reconstruction

    def fit(self, embeddings: np.ndarray, iterations: int = 20, seed: int = 2026) -> None:
        """Fit residual k-means codebooks from a representative embedding matrix."""
        values = np.asarray(embeddings, dtype=np.float32)
        if values.ndim != 2 or values.shape[1] != self.dimension or values.shape[0] == 0:
            raise ValueError(f"embeddings must have shape (n, {self.dimension}) with n > 0")
        if iterations < 1:
            raise ValueError("iterations must be positive")
        rng = np.random.default_rng(seed)
        residuals = values.copy()
        trained: list[np.ndarray] = []
        for _ in range(self.levels):
            centers = self._initial_centers(residuals, rng)
            for _ in range(iterations):
                assignments = self._nearest_many(residuals, centers)
                counts = np.bincount(assignments, minlength=self.codebook_size)
                sums = np.zeros_like(centers)
                np.add.at(sums, assignments, residuals)
                occupied = counts > 0
                centers[occupied] = sums[occupied] / counts[occupied, None]
                if np.any(~occupied):
                    centers[~occupied] = residuals[
                        rng.integers(0, residuals.shape[0], size=int(np.sum(~occupied)))
                    ]
            trained.append(centers.copy())
            residuals -= centers[self._nearest_many(residuals, centers)]
        self.codebooks = self._validate_codebooks(np.stack(trained))
        self.status = "fitted"

    def save(self, path: str | Path) -> None:
        np.savez_compressed(path, codebooks=self.codebooks)

    @classmethod
    def load(cls, path: str | Path) -> ResidualVectorQuantizer:
        with np.load(path) as archive:
            codebooks = np.asarray(archive["codebooks"], dtype=np.float32)
        if codebooks.ndim != 3:
            raise ValueError("codebook checkpoint must be a rank-3 array")
        return cls(
            dimension=codebooks.shape[2],
            levels=codebooks.shape[0],
            codebook_size=codebooks.shape[1],
            codebooks=codebooks,
        )

    def _validate_codebooks(self, codebooks: np.ndarray) -> np.ndarray:
        values = np.asarray(codebooks, dtype=np.float32)
        expected = (self.levels, self.codebook_size, self.dimension)
        if values.shape != expected:
            raise ValueError(f"codebooks must have shape {expected}, got {values.shape}")
        if not np.isfinite(values).all():
            raise ValueError("codebooks must contain only finite values")
        return values.copy()

    def _validate_vector(self, vector: np.ndarray) -> np.ndarray:
        values = np.asarray(vector, dtype=np.float32)
        if values.shape != (self.dimension,):
            raise ValueError(f"embedding must have shape ({self.dimension},)")
        if not np.isfinite(values).all():
            raise ValueError("embedding must contain only finite values")
        return values

    def _initial_centers(self, values: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        if values.shape[0] >= self.codebook_size:
            return values[rng.choice(values.shape[0], self.codebook_size, replace=False)].copy()
        indices = rng.choice(values.shape[0], self.codebook_size, replace=True)
        centers = values[indices].copy()
        scale = max(float(np.std(values)), 1e-4) * 1e-3
        centers += rng.normal(0, scale, centers.shape).astype(np.float32)
        return centers

    @staticmethod
    def _nearest(vector: np.ndarray, codebook: np.ndarray) -> int:
        distances = np.sum((codebook - vector) ** 2, axis=1)
        return int(np.argmin(distances))

    @staticmethod
    def _nearest_many(values: np.ndarray, centers: np.ndarray) -> np.ndarray:
        center_norm = np.sum(centers * centers, axis=1)
        assignments = np.empty(values.shape[0], dtype=np.int64)
        center_transpose = centers.T
        for start in range(0, values.shape[0], 4096):
            chunk = values[start : start + 4096]
            value_norm = np.sum(chunk * chunk, axis=1, keepdims=True)
            distances = value_norm + center_norm[None, :] - 2.0 * chunk @ center_transpose
            assignments[start : start + len(chunk)] = np.argmin(distances, axis=1)
        return assignments
