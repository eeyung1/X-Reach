import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from xreach_api.calibration import ScoreCalibrationConfig
from xreach_api.db import Database
from xreach_api.main import create_app
from xreach_api.multimodal import MediaUpload, MultimodalService
from xreach_api.radar_stream import RadarEventBuffer, RadarSeedProducer
from xreach_api.rust_scorer import RustScorer


class FakeRustScorer:
    def __init__(self):
        self.candidates = []
        self.calibrations = []

    def score(self, candidate, score_calibration=None):
        self.candidates.append(candidate)
        self.calibrations.append(score_calibration)
        predictions = candidate["predictions"]
        weighted = predictions.get("share_via_copy_link", 0.0) * 20
        dropped = candidate.get("visibility_labels", [])
        bounds = (
            {
                "lower": 60.0,
                "upper": 86.5,
                "confidence_level": 0.95,
                "method": "dkw",
            }
            if score_calibration and score_calibration["method"] == "empirical"
            else None
        )
        return {
            "weighted_score": weighted,
            "final_score": 0.0 if dropped else weighted + 0.001,
            "author_diversity_multiplier": 1.0,
            "out_of_network_multiplier": candidate.get("oon_multiplier", 1.0),
            "dropped_by": dropped,
            "model_score_percentile": 73.25 if score_calibration else None,
            "model_score_percentile_bounds": bounds,
            "score_calibration_status": (
                score_calibration["method"] if score_calibration else "unavailable"
            ),
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


def client_with(
    scorer, multimodal=None, score_calibration=None, alert_admin_token=None
):
    return TestClient(
        create_app(
            scorer,
            multimodal,
            score_calibration,
            alert_admin_token=alert_admin_token,
        )
    )


def alert_config():
    return {
        "rules": [
            {
                "rule_id": "fast-tech",
                "min_views_per_minute": 500,
                "max_age_minutes": 30,
                "simclusters": ["Technology"],
            }
        ],
        "webhooks": [
            {
                "webhook_id": "team-chat",
                "url": "https://hooks.example.test/secret-path",
                "secret": "never-return-this-secret",
                "format": "slack",
            }
        ],
    }


def alert_seed(**changes):
    return {
        "tweet_id": "root-1",
        "text": "A useful post",
        "age_minutes": 5,
        "views_per_minute": 900,
        "simclusters": ["Technology"],
        **changes,
    }


async def run_inline(function, *args):
    return function(*args)


def test_health_reports_api_status_without_building_rust_binary():
    response = client_with(FakeRustScorer()).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "scoring_engine": "rust-core"}


def test_health_fails_when_database_connection_is_unavailable():
    database = Database(":memory:")
    app = create_app(database=database)
    database.close()

    response = TestClient(app).get("/healthz")

    assert response.status_code == 503
    assert response.json()["detail"] == "database is unavailable"


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
    assert payload["model_score_percentile"] is None
    assert payload["model_score_percentile_bounds"] is None
    assert payload["score_calibration_status"] == "unavailable"
    assert "reach_potential_percentile" not in payload
    assert "reach_calibration_status" not in payload
    assert any(
        "do not predict impressions" in item for item in payload["model_limitations"]
    )
    assert payload["placement_recommendation"]["surface"] == "UNDETERMINED"
    assert scorer.candidates[0]["author_id"] == 42
    assert scorer.candidates[0]["in_network"] is False


def test_analyze_returns_configured_historical_score_percentile():
    scorer = FakeRustScorer()
    calibration = ScoreCalibrationConfig(
        method="empirical", reference_scores=(0.1, 0.2, 0.3)
    )
    response = client_with(scorer, score_calibration=calibration).post(
        "/api/v1/analyze",
        json={"text": "A test draft", "predictions": {"favorite": 0.2}},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["model_score_percentile"] == 73.25
    assert payload["model_score_percentile_bounds"] == {
        "lower": 60.0,
        "upper": 86.5,
        "confidence_level": 0.95,
        "method": "dkw",
    }
    assert payload["score_calibration_status"] == "empirical"
    assert scorer.calibrations == [
        {"method": "empirical", "reference_scores": [0.1, 0.2, 0.3]}
    ]


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
    assert payload["local_topic_codes"] == [12, 44, 91, 8, 203, 17]
    assert "semantic_id" not in payload
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


def test_placement_returns_calibrated_percentile_for_recommended_surface():
    calibration = ScoreCalibrationConfig(
        method="log_normal", log_mean=-2.0, log_std_dev=1.0
    )
    response = client_with(FakeRustScorer(), score_calibration=calibration).post(
        "/api/v1/placement",
        json={
            "profile_predictions": {"share_via_copy_link": 0.1},
            "direct_root_reply_predictions": {"share_via_copy_link": 0.4},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["recommended_surface"] == "DIRECT_ROOT_REPLY"
    assert payload["model_score_percentile"] == 73.25
    assert payload["model_score_percentile_bounds"] is None
    assert payload["score_calibration_status"] == "log_normal"
    assert payload["scores"]["profile"]["score_calibration_status"] == "log_normal"
    assert any("not reach probability" in item for item in payload["model_limitations"])


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


def test_rust_scorer_sends_calibration_configuration(tmp_path: Path):
    executable = tmp_path / "fake-scorer"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "request = json.load(sys.stdin)\n"
        "print(json.dumps({'method': request['score_calibration']['method']}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    result = RustScorer(binary=str(executable)).score(
        {"predictions": {"favorite": 0.25}},
        score_calibration={"method": "empirical", "reference_scores": [0.1, 0.2]},
    )

    assert result == {"method": "empirical"}


def test_calibration_config_rejects_invalid_reference_data(tmp_path: Path):
    config_path = tmp_path / "calibration.json"
    config_path.write_text(
        json.dumps({"method": "empirical", "reference_scores": [1]}),
        encoding="utf-8",
    )

    try:
        ScoreCalibrationConfig.from_file(config_path)
    except ValueError as error:
        assert "2 to 1000000" in str(error)
    else:
        raise AssertionError("invalid calibration configuration should be rejected")


def test_radar_event_buffer_replays_filtered_history_and_signals_expiry():
    events = RadarEventBuffer(capacity=2)
    events.publish({"tweet_id": "one", "simclusters": ["Technology"]})
    events.publish({"tweet_id": "two", "simclusters": ["Sports"]})
    events.publish({"tweet_id": "three", "simclusters": ["Technology"]})

    subscriber, replay, queue, history_lost, oldest_id = events.subscribe(
        after_id=0, simclusters=["technology"]
    )

    assert [event.payload["tweet_id"] for event in replay] == ["three"]
    assert queue.empty()
    assert history_lost is True
    assert oldest_id == 2
    events.unsubscribe(subscriber)


def test_radar_event_buffer_reports_dropped_subscriber_backlog():
    events = RadarEventBuffer(subscriber_capacity=1)
    subscriber, _, queue, _, _ = events.subscribe()
    events.publish({"tweet_id": "one", "simclusters": []})
    events.publish({"tweet_id": "two", "simclusters": []})

    assert events.consume_dropped_through(subscriber) == 1
    assert queue.get_nowait().event_id == 2
    events.unsubscribe(subscriber)


def test_radar_ingestion_requires_token_and_publishes_batch():
    class PausedProducer:
        async def run(self):
            await asyncio.Event().wait()

    events = RadarEventBuffer()
    client = TestClient(
        create_app(
            radar_events=events,
            radar_producer=PausedProducer(),
            radar_ingest_token="secret",
        )
    )
    batch = {
        "events": [
            {"tweet_id": "root-1", "simclusters": ["Technology"]},
            {"tweet_id": "root-2", "simclusters": ["Sports"]},
        ]
    }

    denied = client.post("/api/v1/radar/events", json=batch)
    accepted = client.post(
        "/api/v1/radar/events",
        json=batch,
        headers={"Authorization": "Bearer secret"},
    )
    subscriber, replay, _, _, _ = events.subscribe()
    events.unsubscribe(subscriber)

    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json() == {
        "published": 2,
        "first_event_id": 1,
        "last_event_id": 2,
    }
    assert [event.payload["tweet_id"] for event in replay] == ["root-1", "root-2"]


def test_radar_seed_producer_publishes_new_capture_seeds():
    class Provider:
        async def read_capture(self):
            return {"new_capture": True}

    class Scanner:
        def scan(self, capture):
            return [
                {
                    "tweet_id": "root-1",
                    "impressions": 5_000,
                    "replies": 2,
                    "views_per_minute": 1_000.0,
                    "simclusters": ["Technology"],
                }
            ]

    async def run_until_published():
        events = RadarEventBuffer()
        producer = RadarSeedProducer(
            events, Provider(), Scanner(), interval_seconds=0.1
        )
        task = asyncio.create_task(producer.run())
        try:
            for _ in range(100):
                await asyncio.sleep(0.005)
                subscriber, replay, _, _, _ = events.subscribe()
                events.unsubscribe(subscriber)
                if replay:
                    return replay
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        raise AssertionError("radar producer did not publish its seed")

    replay = asyncio.run(run_until_published())
    assert replay[0].payload["tweet_id"] == "root-1"
    assert replay[0].event_id == 1


def test_radar_sse_stream_replays_events_and_sets_streaming_headers():
    class PausedProducer:
        async def run(self):
            await asyncio.Event().wait()

    events = RadarEventBuffer()
    events.publish(
        {"tweet_id": "root-1", "simclusters": ["Technology"], "views_per_minute": 900}
    )
    client = TestClient(
        create_app(
            radar_events=events,
            radar_producer=PausedProducer(),
        )
    )

    with client.stream("GET", "/api/v1/radar/stream?simcluster=technology") as response:
        lines = iter(response.iter_lines())
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache, no-transform"
        assert next(lines) == "retry: 3000"
        assert next(lines) == ""
        assert next(lines) == "id: 1"
        assert next(lines) == "event: seed"
        assert '"tweet_id":"root-1"' in next(lines)


def test_alert_configuration_requires_admin_and_never_returns_webhook_secret():
    client = client_with(FakeRustScorer(), alert_admin_token="admin-secret")

    denied = client.put("/api/v1/alerts", json=alert_config())
    configured = client.put(
        "/api/v1/alerts",
        json=alert_config(),
        headers={"X-Alert-Admin-Token": "admin-secret"},
    )
    readback = client.get(
        "/api/v1/alerts", headers={"X-Alert-Admin-Token": "admin-secret"}
    )

    assert denied.status_code == 401
    assert configured.status_code == 200
    assert configured.json() == {
        "rules": alert_config()["rules"],
        "webhooks": [{"webhook_id": "team-chat", "format": "slack"}],
    }
    assert "never-return-this-secret" not in readback.text
    assert "secret-path" not in readback.text

    restarted = client_with(FakeRustScorer(), alert_admin_token="admin-secret")
    persisted = restarted.get(
        "/api/v1/alerts", headers={"X-Alert-Admin-Token": "admin-secret"}
    )
    assert persisted.status_code == 200
    assert persisted.json() == configured.json()


def test_alert_configuration_rejects_duplicate_ids_and_unsafe_url():
    client = client_with(FakeRustScorer(), alert_admin_token="admin-secret")
    duplicate = alert_config()
    duplicate["rules"].append(duplicate["rules"][0])
    unsafe = alert_config()
    unsafe["webhooks"][0]["url"] = "file:///etc/passwd"
    headers = {"X-Alert-Admin-Token": "admin-secret"}

    assert (
        client.put("/api/v1/alerts", json=duplicate, headers=headers).status_code == 422
    )
    assert client.put("/api/v1/alerts", json=unsafe, headers=headers).status_code == 422


def test_alert_test_endpoint_dispatches_matching_seed():
    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    client = client_with(FakeRustScorer(), alert_admin_token="admin-secret")
    headers = {"X-Alert-Admin-Token": "admin-secret"}
    assert (
        client.put("/api/v1/alerts", json=alert_config(), headers=headers).status_code
        == 200
    )

    with (
        patch("xreach_radar.alerts.urlopen", return_value=Response()) as send,
        patch("xreach_radar.alerts.asyncio.to_thread", new=run_inline),
    ):
        response = client.post(
            "/api/v1/alerts/test",
            json={"rule_id": "fast-tech", "seed": alert_seed()},
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json() == {
        "event_id": "fast-tech:root-1",
        "deliveries": {"team-chat": True},
    }
    assert send.call_count == 1
    assert send.call_args.args[0].headers["X-xreach-signature"].startswith("sha256=")


def test_alert_test_endpoint_rejects_missing_rules_and_nonmatching_seed():
    client = client_with(FakeRustScorer(), alert_admin_token="admin-secret")
    headers = {"X-Alert-Admin-Token": "admin-secret"}
    client.put("/api/v1/alerts", json=alert_config(), headers=headers)

    missing = client.post(
        "/api/v1/alerts/test",
        json={"rule_id": "missing", "seed": alert_seed()},
        headers=headers,
    )
    unmatched = client.post(
        "/api/v1/alerts/test",
        json={"rule_id": "fast-tech", "seed": alert_seed(views_per_minute=20)},
        headers=headers,
    )

    assert missing.status_code == 404
    assert unmatched.status_code == 422


def test_radar_ingestion_dispatches_matching_alert_in_background():
    class PausedProducer:
        async def run(self):
            await asyncio.Event().wait()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    client = TestClient(
        create_app(radar_producer=PausedProducer(), alert_admin_token="admin-secret")
    )
    headers = {"X-Alert-Admin-Token": "admin-secret"}
    client.put("/api/v1/alerts", json=alert_config(), headers=headers)

    with (
        patch("xreach_radar.alerts.urlopen", return_value=Response()) as send,
        patch("xreach_radar.alerts.asyncio.to_thread", new=run_inline),
    ):
        response = client.post(
            "/api/v1/radar/events",
            json={"events": [alert_seed()]},
        )

    assert response.status_code == 200
    assert send.call_count == 1
