from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from xreach_multimodal.extractor import MultimodalExtractor
from xreach_multimodal.propensity import ACTION_PRIORS, LinearPropensityHead
from xreach_multimodal.types import MediaAsset
from xreach_multimodal.video import DecodedKeyframe


class FakeEncoder:
    model_name = "fake-vit-b-32"
    embedding_dimension = 4

    def encode_text(self, texts: Sequence[str]) -> np.ndarray:
        return np.tile(np.array([[1, 0, 0, 0]], dtype=np.float32), (len(texts), 1))

    def encode_images(self, images: Sequence[Image.Image]) -> np.ndarray:
        return np.tile(np.array([[0, 1, 0, 0]], dtype=np.float32), (len(images), 1))


class FixedPropensityHead:
    def predict(self, embedding, **kwargs):
        return {
            **{name: 0.1 for name in ACTION_PRIORS},
            "dwell_seconds": 2.0,
            "click_dwell_seconds": 1.0,
        }


def png_bytes(color=(255, 0, 0)):
    stream = BytesIO()
    Image.new("RGB", (8, 8), color).save(stream, format="PNG")
    return stream.getvalue()


def test_text_and_image_return_rvq_sid_and_prediction_heads():
    extractor = MultimodalExtractor(encoder=FakeEncoder(), propensity_estimator=FixedPropensityHead())
    result = extractor.extract(
        "A small test draft",
        [MediaAsset(png_bytes(), "image/png", "draft.png")],
    )

    assert result.modalities == ("text", "image")
    assert len(result.semantic_id) == 6
    assert result.embedding_dimension == 4
    assert result.predictions["favorite"] == 0.1
    assert result.predictions["dwell_seconds"] == 2.0
    assert result.propensity_status == "linear_head_uncalibrated"
    assert [feature.modality for feature in result.features] == ["text", "image"]
    assert len(result.features[1].embedding) == 4
    assert result.to_dict()["semantic_id"] == list(result.semantic_id)


def test_video_keyframes_and_duration_flow_into_features():
    image = Image.new("RGB", (8, 8), "blue")

    def fake_video(_data):
        return (
            [
                DecodedKeyframe(image, "opening", 0.0),
                DecodedKeyframe(image, "midpoint", 5.0),
                DecodedKeyframe(image, "climax", 8.0),
            ],
            10.0,
        )

    extractor = MultimodalExtractor(
        encoder=FakeEncoder(),
        propensity_estimator=FixedPropensityHead(),
        video_decoder=fake_video,
    )
    result = extractor.extract("Video draft", [MediaAsset(b"video", "video/mp4")])

    assert result.modalities == ("text", "video")
    assert result.video_duration_seconds == 10.0
    assert [frame.label for frame in result.keyframes] == ["opening", "midpoint", "climax"]
    assert [
        feature.label
        for feature in result.features
        if feature.modality == "video_frame"
    ] == ["opening", "midpoint", "climax"]


def test_rejects_unsupported_files_and_oversized_media():
    extractor = MultimodalExtractor(
        encoder=FakeEncoder(),
        propensity_estimator=FixedPropensityHead(),
        max_media_bytes=4,
    )

    with pytest.raises(ValueError, match="size limit"):
        extractor.extract("draft", [MediaAsset(b"12345", "image/png")])
    with pytest.raises(ValueError, match="unsupported media type"):
        MultimodalExtractor(
            encoder=FakeEncoder(), propensity_estimator=FixedPropensityHead()
        ).extract("draft", [MediaAsset(b"data", "application/pdf")])


def test_rejects_invalid_images():
    extractor = MultimodalExtractor(encoder=FakeEncoder(), propensity_estimator=FixedPropensityHead())

    with pytest.raises(ValueError, match="could not decode image"):
        extractor.extract("draft", [MediaAsset(b"not-an-image", "image/png")])


def test_prompt_proxy_returns_all_probability_heads_and_is_explicitly_uncalibrated():
    class PromptEncoder(FakeEncoder):
        def encode_text(self, texts):
            vectors = []
            for index, _ in enumerate(texts):
                vectors.append([1, 0, 0, 0] if index % 2 == 0 else [0, 1, 0, 0])
            return np.asarray(vectors, dtype=np.float32)

    extractor = MultimodalExtractor(encoder=PromptEncoder())
    result = extractor.extract("A prompt-proxy test")

    assert result.propensity_status == "uncalibrated_prompt_proxy"
    assert set(ACTION_PRIORS).issubset(result.predictions)
    assert all(0 <= result.predictions[name] <= 1 for name in ACTION_PRIORS)


def test_linear_propensity_head_produces_complete_prediction_payload():
    head = LinearPropensityHead(
        weights=np.zeros((len(ACTION_PRIORS), 4), dtype=np.float32),
        bias=np.zeros(len(ACTION_PRIORS), dtype=np.float32),
    )
    extractor = MultimodalExtractor(
        encoder=FakeEncoder(), propensity_estimator=head
    )

    result = extractor.extract("A classifier-head test")

    assert result.propensity_status == "linear_head_uncalibrated"
    assert result.predictions["favorite"] == 0.5
    assert result.predictions["video_open"] == 0.0
    assert result.predictions["dwell_seconds"] > 0


def test_linear_head_can_fit_and_reload_labeled_action_outputs(tmp_path):
    embeddings = np.asarray([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=np.float32)
    targets = np.stack(
        [
            np.zeros(len(ACTION_PRIORS), dtype=np.float32),
            np.ones(len(ACTION_PRIORS), dtype=np.float32),
        ]
    )
    head = LinearPropensityHead.fit(embeddings, targets, epochs=80, learning_rate=0.2)
    checkpoint = tmp_path / "propensities.npz"
    head.save(str(checkpoint))
    restored = LinearPropensityHead.load(str(checkpoint))

    low = restored.predict(embeddings[0])["favorite"]
    high = restored.predict(embeddings[1])["favorite"]
    assert 0 <= low < 0.5 < high <= 1
