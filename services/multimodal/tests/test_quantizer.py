import numpy as np
import pytest

from xreach_multimodal.quantizer import ResidualVectorQuantizer


def test_default_quantizer_emits_six_codes_in_range():
    quantizer = ResidualVectorQuantizer(dimension=8)

    semantic_id, reconstruction = quantizer.encode(np.ones(8, dtype=np.float32))

    assert len(semantic_id) == 6
    assert all(0 <= code < 256 for code in semantic_id)
    assert reconstruction.shape == (8,)
    assert quantizer.codebooks.shape == (6, 256, 8)
    assert quantizer.status == "bootstrap_untrained"


def test_rvq_fit_save_and_load_round_trip(tmp_path):
    embeddings = np.random.default_rng(4).normal(size=(24, 4)).astype(np.float32)
    quantizer = ResidualVectorQuantizer(dimension=4, codebook_size=8)
    quantizer.fit(embeddings, iterations=2)
    checkpoint = tmp_path / "rvq.npz"
    quantizer.save(checkpoint)

    restored = ResidualVectorQuantizer.load(checkpoint)
    ids, _ = restored.encode(embeddings[0])

    assert restored.status == "loaded"
    assert restored.codebooks.shape == (6, 8, 4)
    assert len(ids) == 6


def test_quantizer_rejects_wrong_embedding_dimension():
    quantizer = ResidualVectorQuantizer(dimension=4)

    with pytest.raises(ValueError, match=r"shape \(4,\)"):
        quantizer.encode(np.ones(3, dtype=np.float32))
