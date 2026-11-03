import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from xreach_api.main import create_app
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


def client_with(scorer):
    return TestClient(create_app(scorer))


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
