import json
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from xreach_api.main import create_app
from xreach_api.multimodal import MediaUpload, MultimodalService
from xreach_api.rust_scorer import RustScorer


class FakeRustScorer:
    def __init__(self):
        self.candidates = []

    def score(self, candidate):
        self.candidates.append(candidate)
        predictions = candidate["predictions"]
        weighted = predictions.get("share_via_copy_link", 0.0) * 20
        dropped = candidate.get("visibility_labels", [])
        return {
            "weighted_score": weighted,
            "final_score": 0.0 if dropped else weighted + 0.001,
            "author_diversity_multiplier": 1.0,
            "out_of_network_multiplier": candidate.get("oon_multiplier", 1.0),
            "dropped_by": dropped,
        }


class FakeMultimodal:
    def __init__(self):
        self.requests = []

    def extract(self, text, media):
        self.requests.append((text, media))
        return {
            "model_name": "fake-clip",
            "embedding_dimension": 8,
            "semantic_id": [12, 44, 91, 8, 203, 17],
            "features": [{"embedding": [0.1] * 8}],
            "predictions": {
                "favorite": 0.2,
                "reply": 0.1,
                "retweet": 0.05,
                "photo_expand": 0.3,
                "video_open": 0.0,
                "click": 0.1,
                "open_link": 0.0,
                "share": 0.04,
                "share_via_dm": 0.02,
                "share_via_copy_link": 0.25,
                "dwell": 0.4,
                "quote": 0.03,
                "follow_author": 0.01,
                "dwell_seconds": 6.0,
                "click_dwell_seconds": 2.0,
                "not_interested": 0.01,
                "block_author": 0.001,
                "mute_author": 0.001,
                "report": 0.001,
                "not_dwelled": 0.1,
            },
            "modalities": ["text", "image"],
            "keyframes": [{"label": "opening", "timestamp_seconds": 1.2}],
            "video_duration_seconds": 18.0,
            "quantizer_status": "deterministic_untrained_codebooks",
            "propensity_status": "uncalibrated_prompt_proxy",
        }


def client_with(scorer, multimodal=None):
    return TestClient(create_app(scorer, multimodal))


def test_health_reports_api_status_without_building_rust_binary():
    response = client_with(FakeRustScorer()).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "scoring_engine": "rust-core"}


def test_analyze_validates_prediction_heads_and_returns_uncalibrated_score():
    scorer = FakeRustScorer()
    response = client_with(scorer).post(
        "/api/v1/analyze",
        json={
            "text": "A test draft",
            "predictions": {"share_via_copy_link": 0.2},
            "author_id": 42,
            "in_network": False,
            "oon_multiplier": 0.6,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["score"]["weighted_score"] == 4.0
    assert payload["algorithmic_viability_score"] == 4.001
    assert payload["reach_potential_percentile"] is None
    assert payload["reach_calibration_status"] == "unavailable"
    assert payload["placement_recommendation"]["surface"] == "UNDETERMINED"
    assert scorer.candidates[0]["author_id"] == 42
    assert scorer.candidates[0]["in_network"] is False


def test_analyze_accepts_multipart_media_when_predictions_are_supplied():
    scorer = FakeRustScorer()
    response = client_with(scorer).post(
        "/api/v1/analyze",
        data={"text": "", "predictions": json.dumps({"favorite": 0.1})},
        files={"media": ("draft.png", b"image-bytes", "image/png")},
    )

    assert response.status_code == 200
    assert scorer.candidates[0]["predictions"]["favorite"] == 0.1


def test_analyze_extracts_predictions_and_semantic_metadata_from_multipart():
    scorer = FakeRustScorer()
    multimodal = FakeMultimodal()
    response = client_with(scorer, multimodal).post(
        "/api/v1/analyze",
        data={"text": "A visual draft"},
        files={"media": ("draft.png", b"image-bytes", "image/png")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["algorithmic_viability_score"] == 5.001
    assert payload["semantic_id"] == [12, 44, 91, 8, 203, 17]
    assert payload["model_name"] == "fake-clip"
    assert payload["modalities"] == ["text", "image"]
    assert payload["keyframes"] == [{"label": "opening", "timestamp_seconds": 1.2}]
    assert "features" not in payload
    assert multimodal.requests[0][0] == "A visual draft"
    upload = multimodal.requests[0][1][0]
    assert upload.filename == "draft.png"
    assert upload.media_type == "image/png"
    assert upload.data == b"image-bytes"
    assert scorer.candidates[0]["predictions"]["share_via_copy_link"] == 0.25


def test_analyze_extracts_text_only_json_without_predictions():
    multimodal = FakeMultimodal()
    response = client_with(FakeRustScorer(), multimodal).post(
        "/api/v1/analyze", json={"text": "Text only"}
    )

    assert response.status_code == 200
    assert multimodal.requests == [("Text only", ())]


def test_invalid_extractor_output_is_service_unavailable():
    class InvalidMultimodal:
        def extract(self, text, media):
            return {"predictions": {"favorite": 0.1}}

    response = client_with(FakeRustScorer(), InvalidMultimodal()).post(
        "/api/v1/analyze", json={"text": "Text only"}
    )

    assert response.status_code == 503
    assert (
        response.json()["detail"]
        == "multimodal extractor returned invalid model output"
    )


def test_extractor_semantic_id_codes_are_strict_integers():
    class InvalidSemanticId(FakeMultimodal):
        def extract(self, text, media):
            payload = dict(super().extract(text, media))
            payload["semantic_id"] = ["12", 44, 91, 8, 203, 17]
            return payload

    response = client_with(FakeRustScorer(), InvalidSemanticId()).post(
        "/api/v1/analyze", json={"text": "Text only"}
    )

    assert response.status_code == 503


def test_lazy_multimodal_service_adapts_uploads_to_sibling_package(monkeypatch):
    observed = {}

    class FakeAsset:
        def __init__(self, **values):
            self.__dict__.update(values)

    class FakeResult:
        def to_dict(self):
            return {"semantic_id": [1, 2, 3, 4, 5, 6]}

    class FakeExtractor:
        def extract(self, text, media):
            observed["request"] = (text, media)
            return FakeResult()

    monkeypatch.setitem(
        sys.modules,
        "xreach_multimodal",
        SimpleNamespace(MediaAsset=FakeAsset, MultimodalExtractor=FakeExtractor),
    )
    service = MultimodalService()
    payload = service.extract(
        "draft",
        [MediaUpload(b"bytes", "image/png", "draft.png")],
    )

    assert payload == {"semantic_id": [1, 2, 3, 4, 5, 6]}
    text, media = observed["request"]
    assert text == "draft"
    assert media[0].data == b"bytes"
    assert media[0].media_type == "image/png"
    assert media[0].filename == "draft.png"


def test_analyze_requires_at_least_one_prediction_head():
    response = client_with(FakeRustScorer()).post(
        "/api/v1/analyze", json={"text": "A test draft", "predictions": {}}
    )

    assert response.status_code == 422


def test_placement_compares_independently_scored_profile_and_root_reply():
    scorer = FakeRustScorer()
    response = client_with(scorer).post(
        "/api/v1/placement",
        json={
            "text": "A test draft",
            "author_id": 7,
            "profile_predictions": {"share_via_copy_link": 0.1},
            "direct_root_reply_predictions": {"share_via_copy_link": 0.4},
            "oon_multiplier": 0.5,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["recommended_surface"] == "DIRECT_ROOT_REPLY"
    assert payload["scores"]["profile"]["weighted_score"] == 2.0
    assert payload["scores"]["direct_root_reply"]["weighted_score"] == 8.0
    assert len(scorer.candidates) == 2
    assert scorer.candidates[0]["is_reply"] is False
    assert scorer.candidates[1]["is_reply"] is True


def test_placement_prefers_surviving_candidate_when_other_is_filtered():
    response = client_with(FakeRustScorer()).post(
        "/api/v1/placement",
        json={
            "profile_predictions": {"favorite": 0.2},
            "direct_root_reply_predictions": {"favorite": 0.9},
            "direct_root_reply_visibility_labels": ["do_not_amplify"],
        },
    )

    assert response.status_code == 200
    assert response.json()["recommended_surface"] == "PROFILE_POST"


def test_rust_scorer_uses_configured_json_process(tmp_path: Path):
    executable = tmp_path / "fake-scorer"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "request = json.load(sys.stdin)\n"
        "print(json.dumps({'weighted_score': request['predictions']['favorite']}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    result = RustScorer(binary=str(executable)).score(
        {"predictions": {"favorite": 0.25}}
    )

    assert result == {"weighted_score": 0.25}
