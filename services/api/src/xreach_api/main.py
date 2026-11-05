"""FastAPI endpoints for scoring a draft and comparing publication surfaces."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ValidationError
from starlette.datastructures import UploadFile
from starlette.responses import StreamingResponse

from .calibration import ScoreCalibrationConfig
from .models import (
    AlertConfigurationRequest,
    AlertConfigurationResponse,
    AlertRuleConfig,
    AlertTestRequest,
    AnalyzeRequest,
    AnalyzeResponse,
    AutomaticAnalyzeRequest,
    CandidateInput,
    MultimodalOutput,
    PercentileBounds,
    PlacementRequest,
    RadarIngestRequest,
    OutcomeSnapshotRecord,
    PublicationContextRecord,
    ScoreOutput,
)
from .telemetry import DraftTelemetryRequest, TelemetryStore
from .multimodal import (
    MediaUpload,
    MultimodalBackend,
    MultimodalService,
    MultimodalUnavailable,
)
from .radar_stream import (
    RadarEventBuffer,
    RadarSeedProducer,
    default_radar_producer,
    encode_sse,
)
from .rust_scorer import RustScorer, ScorerUnavailable

MAX_MEDIA_BYTES = 50 * 1024 * 1024
ModelT = TypeVar("ModelT", bound=BaseModel)
logger = logging.getLogger(__name__)
MODEL_LIMITATIONS = (
    "Model scores compare weighted action propensities; they do not predict impressions, virality, or distribution.",
    "Model score percentiles describe rank within the configured score reference distribution, not reach probability.",
    "Local Topic Codes come from this installation's local quantizer and are not official X Phoenix Semantic IDs.",
    "Uncertainty bounds are available only for empirical calibrations with a known reference sample size.",
    "Live ranking parameters, account state, audience response, and platform changes are outside this model.",
)


def create_app(
    scorer: RustScorer | None = None,
    multimodal: MultimodalBackend | None = None,
    score_calibration: ScoreCalibrationConfig | None = None,
    radar_events: RadarEventBuffer | None = None,
    radar_producer: RadarSeedProducer | None = None,
    radar_heartbeat_seconds: float = 15.0,
    radar_ingest_token: str | None = None,
    alert_admin_token: str | None = None,
    *,
    reach_calibration: ScoreCalibrationConfig | None = None,
) -> FastAPI:
    if not 0.1 <= radar_heartbeat_seconds <= 300:
        raise ValueError("radar heartbeat interval must be between 0.1 and 300 seconds")
    if score_calibration is not None and reach_calibration is not None:
        raise ValueError("configure score_calibration only once")
    calibration = (
        score_calibration
        or reach_calibration
        or ScoreCalibrationConfig.from_environment()
    )
    event_buffer = radar_events or RadarEventBuffer()
    producer = radar_producer or default_radar_producer(event_buffer)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        producer_task = asyncio.create_task(producer.run())
        app.state.radar_producer_task = producer_task
        try:
            yield
        finally:
            producer_task.cancel()
            with suppress(asyncio.CancelledError):
                await producer_task

    app = FastAPI(
        title="X-Reach API",
        version="0.1.0",
        description=(
            "Extracts local multimodal draft signals and scores their Phoenix "
            "prediction heads with the Rust core. Model score percentiles are "
            "available when a historical score calibration is configured; they "
            "are comparative score ranks, not forecasts of reach."
        ),
        lifespan=lifespan,
    )
    app.state.scorer = scorer or RustScorer()
    app.state.multimodal = multimodal or MultimodalService()
    app.state.score_calibration = calibration
    app.state.radar_events = event_buffer
    app.state.radar_producer = producer
    app.state.radar_heartbeat_seconds = radar_heartbeat_seconds
    app.state.radar_ingest_token = (
        radar_ingest_token
        if radar_ingest_token is not None
        else os.environ.get("XREACH_RADAR_INGEST_TOKEN")
    ) or None
    app.state.alert_admin_token = (
        alert_admin_token
        if alert_admin_token is not None
        else os.environ.get("XREACH_ALERT_ADMIN_TOKEN")
    ) or None
    app.state.alert_rules = []
    app.state.alert_webhooks = {}
    app.state.telemetry = TelemetryStore()

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "scoring_engine": "rust-core"}

    @app.post("/api/v1/telemetry/drafts", status_code=201)
    def register_telemetry_draft(payload: DraftTelemetryRequest) -> dict[str, Any]:
        try:
            item = app.state.telemetry.register_draft(payload)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return item.model_dump(mode="json")

    @app.post("/api/v1/telemetry/publications", status_code=201)
    def attribute_publication(payload: PublicationContextRecord) -> dict[str, Any]:
        try:
            item = app.state.telemetry.publish(payload)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return item.model_dump(mode="json")

    @app.post("/api/v1/telemetry/outcomes", status_code=201)
    def record_telemetry_outcome(payload: OutcomeSnapshotRecord) -> dict[str, Any]:
        try:
            item = app.state.telemetry.record_outcome(payload)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return item.model_dump(mode="json")

    @app.get("/api/v1/telemetry/summary")
    def telemetry_summary() -> dict[str, object]:
        return app.state.telemetry.summary()

    @app.get("/api/v1/radar/stream")
    async def radar_stream(
        request: Request,
        simcluster: list[str] | None = Query(default=None),
        after: int | None = Query(default=None, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        after_id = after
        if last_event_id is not None:
            try:
                after_id = int(last_event_id)
            except ValueError as error:
                raise HTTPException(
                    status_code=400,
                    detail="Last-Event-ID must be a non-negative integer",
                ) from error
            if after_id < 0:
                raise HTTPException(
                    status_code=400,
                    detail="Last-Event-ID must be a non-negative integer",
                )
        cluster_filters = tuple(
            label.strip()
            for group in (simcluster or ())
            for label in group.split(",")
            if label.strip()
        )

        async def stream_events():
            subscriber, history, queue, history_lost, oldest_id = (
                app.state.radar_events.subscribe(
                    after_id=after_id, simclusters=cluster_filters
                )
            )
            try:
                yield "retry: 3000\n\n"
                if history_lost:
                    reset_id = max(0, (oldest_id or 1) - 1)
                    reset = json.dumps(
                        {"reason": "history_expired", "oldest_event_id": oldest_id},
                        separators=(",", ":"),
                    )
                    yield f"id: {reset_id}\nevent: reset\ndata: {reset}\n\n"
                for event in history:
                    yield encode_sse(event)
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=app.state.radar_heartbeat_seconds
                        )
                    except TimeoutError:
                        heartbeat = datetime.now(timezone.utc).isoformat()
                        yield f'event: heartbeat\ndata: {{"timestamp":"{heartbeat}"}}\n\n'
                    else:
                        dropped_through = (
                            app.state.radar_events.consume_dropped_through(subscriber)
                        )
                        if dropped_through is not None:
                            notice = json.dumps(
                                {
                                    "reason": "subscriber_backlog_expired",
                                    "resume_after_event_id": dropped_through,
                                },
                                separators=(",", ":"),
                            )
                            yield (
                                f"id: {dropped_through}\nevent: reset\n"
                                f"data: {notice}\n\n"
                            )
                        yield encode_sse(event)
            finally:
                app.state.radar_events.unsubscribe(subscriber)

        return StreamingResponse(
            stream_events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.post("/api/v1/radar/events")
    def ingest_radar_events(
        batch: RadarIngestRequest,
        background_tasks: BackgroundTasks,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        expected = app.state.radar_ingest_token
        if expected is not None:
            supplied = (
                authorization.removeprefix("Bearer ")
                if authorization and authorization.startswith("Bearer ")
                else ""
            )
            if not supplied or not hmac.compare_digest(supplied, expected):
                raise HTTPException(
                    status_code=401, detail="invalid radar ingestion token"
                )
        for payload in batch.events:
            tweet_id = str(payload.get("tweet_id", "")).strip()
            if not tweet_id:
                raise HTTPException(
                    status_code=422,
                    detail="each radar event must include a non-empty tweet_id",
                )
        published = [
            app.state.radar_events.publish(payload) for payload in batch.events
        ]
        for payload in batch.events:
            background_tasks.add_task(_dispatch_seed_alerts, app, payload)
        return {
            "published": len(published),
            "first_event_id": published[0].event_id,
            "last_event_id": published[-1].event_id,
        }

    @app.get("/api/v1/alerts", response_model=AlertConfigurationResponse)
    def get_alert_configuration(
        x_alert_admin_token: str | None = Header(default=None),
    ) -> AlertConfigurationResponse:
        _require_alert_admin(app, x_alert_admin_token)
        return _alert_configuration_response(app)

    @app.put("/api/v1/alerts", response_model=AlertConfigurationResponse)
    def set_alert_configuration(
        configuration: AlertConfigurationRequest,
        x_alert_admin_token: str | None = Header(default=None),
    ) -> AlertConfigurationResponse:
        _require_alert_admin(app, x_alert_admin_token)
        AlertRule, _, _, WebhookTarget = _alert_components()
        app.state.alert_rules = [
            AlertRule(
                rule.rule_id,
                rule.min_views_per_minute,
                rule.max_age_minutes,
                frozenset(rule.simclusters),
            )
            for rule in configuration.rules
        ]
        app.state.alert_webhooks = {
            webhook.webhook_id: WebhookTarget(
                webhook.url,
                webhook.secret,
                format=webhook.format,
            )
            for webhook in configuration.webhooks
        }
        return _alert_configuration_response(app)

    @app.post("/api/v1/alerts/test")
    async def test_alert_configuration(
        payload: AlertTestRequest,
        x_alert_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_alert_admin(app, x_alert_admin_token)
        rules = {rule.rule_id: rule for rule in app.state.alert_rules}
        rule = rules.get(payload.rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="alert rule not found")
        if not app.state.alert_webhooks:
            raise HTTPException(
                status_code=409, detail="no alert webhooks are configured"
            )
        _, AlertRuleEngine, WebhookDispatcher, _ = _alert_components()
        events = AlertRuleEngine([rule]).evaluate(payload.seed)
        if not events:
            raise HTTPException(
                status_code=422,
                detail="seed does not satisfy the selected alert rule",
            )
        dispatcher = WebhookDispatcher(list(app.state.alert_webhooks.values()))
        deliveries = await dispatcher.dispatch(events[0])
        return {
            "event_id": events[0].event_id,
            "deliveries": dict(zip(app.state.alert_webhooks, deliveries, strict=True)),
        }

    @app.post("/api/v1/analyze", response_model=AnalyzeResponse)
    async def analyze(request: Request) -> AnalyzeResponse:
        payload, media = await _analysis_payload(request)
        extracted: MultimodalOutput | None = None
        if "predictions" in payload:
            data = _validated(AnalyzeRequest, payload)
        else:
            automatic = _validated(AutomaticAnalyzeRequest, payload)
            extracted = await run_in_threadpool(
                _extract,
                app.state.multimodal,
                automatic.text,
                media,
            )
            data = CandidateInput(
                predictions=extracted.predictions,
                **automatic.model_dump(exclude={"text", "media_attached"}),
            )

        rust_result = await run_in_threadpool(
            _score, app.state.scorer, data, app.state.score_calibration
        )
        score_percentile, calibration_status, percentile_bounds = (
            _score_percentile_output(rust_result)
        )
        return AnalyzeResponse(
            algorithmic_viability_score=rust_result["final_score"],
            model_score_percentile=score_percentile,
            model_score_percentile_bounds=percentile_bounds,
            score_calibration_status=calibration_status,
            score=_score_output(rust_result),
            placement_recommendation={
                "surface": "UNDETERMINED",
                "reasoning": (
                    "A single draft score does not compare surfaces. Submit profile "
                    "and direct-root-reply predictions to /api/v1/placement."
                ),
            },
            suggested_tweaks=[
                *(
                    [
                        "Model score percentile is unavailable until a score calibration is configured."
                    ]
                    if calibration_status == "unavailable"
                    else []
                ),
                *(
                    ["Treat action propensities as an uncalibrated model proxy."]
                    if extracted is not None
                    and "uncalibrated" in extracted.propensity_status
                    else []
                ),
                *(
                    ["Review visibility drops before considering either placement."]
                    if rust_result["dropped_by"]
                    else []
                ),
            ],
            model_name=extracted.model_name if extracted else None,
            local_topic_codes=extracted.semantic_id if extracted else None,
            predicted_probabilities=extracted.predictions if extracted else None,
            modalities=extracted.modalities if extracted else [],
            keyframes=extracted.keyframes if extracted else [],
            video_duration_seconds=(
                extracted.video_duration_seconds if extracted else None
            ),
            quantizer_status=extracted.quantizer_status if extracted else None,
            propensity_status=extracted.propensity_status if extracted else None,
            model_limitations=list(MODEL_LIMITATIONS),
        )

    @app.post("/api/v1/placement")
    def placement(data: PlacementRequest) -> dict[str, Any]:
        common = {
            "author_id": data.author_id,
            "is_mutual_follow_author": data.is_mutual_follow_author,
            "in_network": False,
            "oon_multiplier": data.oon_multiplier,
        }
        profile = CandidateInput(
            predictions=data.profile_predictions,
            visibility_labels=data.profile_visibility_labels,
            has_nsfw_media=data.profile_has_nsfw_media,
            has_untrusted_external_url=data.profile_has_untrusted_external_url,
            **common,
        )
        root_reply = CandidateInput(
            predictions=data.direct_root_reply_predictions,
            is_reply=True,
            visibility_labels=data.direct_root_reply_visibility_labels,
            has_nsfw_media=data.direct_root_reply_has_nsfw_media,
            has_untrusted_external_url=data.direct_root_reply_has_untrusted_external_url,
            **common,
        )
        profile_result = _score(app.state.scorer, profile, app.state.score_calibration)
        reply_result = _score(app.state.scorer, root_reply, app.state.score_calibration)
        if profile_result["dropped_by"] and reply_result["dropped_by"]:
            surface = "NO_SAFE_SURFACE"
            reasoning = "Both placement candidates trigger visibility drops; revise the draft before posting."
        elif profile_result["dropped_by"] and not reply_result["dropped_by"]:
            surface = "DIRECT_ROOT_REPLY"
            reasoning = "The profile candidate triggers visibility drops; the direct-root reply does not."
        elif reply_result["dropped_by"] and not profile_result["dropped_by"]:
            surface = "PROFILE_POST"
            reasoning = "The direct-root reply triggers visibility drops; the profile candidate does not."
        elif reply_result["final_score"] > profile_result["final_score"]:
            surface = "DIRECT_ROOT_REPLY"
            reasoning = "The supplied model predictions score higher for a direct reply to a root post."
        else:
            surface = "PROFILE_POST"
            reasoning = "The supplied model predictions score as well or higher for a profile post."
        recommended_result = (
            reply_result
            if surface == "DIRECT_ROOT_REPLY"
            else profile_result
            if surface == "PROFILE_POST"
            else None
        )
        score_percentile, calibration_status, percentile_bounds = (
            _score_percentile_output(recommended_result)
            if recommended_result is not None
            else (None, "unavailable", None)
        )
        profile_percentile, profile_status, profile_bounds = _score_percentile_output(
            profile_result
        )
        reply_percentile, reply_status, reply_bounds = _score_percentile_output(
            reply_result
        )
        return {
            "recommended_surface": surface,
            "reasoning": reasoning,
            "model_score_percentile": score_percentile,
            "model_score_percentile_bounds": (
                percentile_bounds.model_dump() if percentile_bounds else None
            ),
            "score_calibration_status": calibration_status,
            "scores": {
                "profile": {
                    **_score_output(profile_result).model_dump(),
                    "model_score_percentile": profile_percentile,
                    "model_score_percentile_bounds": (
                        profile_bounds.model_dump() if profile_bounds else None
                    ),
                    "score_calibration_status": profile_status,
                },
                "direct_root_reply": {
                    **_score_output(reply_result).model_dump(),
                    "model_score_percentile": reply_percentile,
                    "model_score_percentile_bounds": (
                        reply_bounds.model_dump() if reply_bounds else None
                    ),
                    "score_calibration_status": reply_status,
                },
            },
            "model_limitations": list(MODEL_LIMITATIONS),
        }

    return app


async def _analysis_payload(
    request: Request,
) -> tuple[dict[str, Any], tuple[MediaUpload, ...]]:
    content_type = request.headers.get("content-type", "")
    try:
        if content_type.startswith("application/json"):
            value = await request.json()
            if not isinstance(value, dict):
                raise ValueError("JSON request body must be an object")
            return value, ()
        if content_type.startswith("multipart/form-data"):
            async with request.form(max_files=4, max_fields=20) as form:
                predictions = form.get("predictions")
                uploads = form.getlist("media")
                if any(not isinstance(upload, UploadFile) for upload in uploads):
                    raise ValueError("media fields must be file uploads")
                media = tuple([await _read_upload(upload) for upload in uploads])
                payload: dict[str, Any] = {
                    "text": str(form.get("text", "")),
                    "media_attached": bool(media),
                }
                if isinstance(predictions, str):
                    value = json.loads(predictions)
                    if not isinstance(value, dict):
                        raise TypeError("predictions must be a JSON object")
                    payload["predictions"] = value
                elif predictions is not None:
                    raise ValueError("predictions must be JSON-encoded text")
                return payload, media
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    raise HTTPException(
        status_code=415,
        detail="Use application/json or multipart/form-data.",
    )


async def _read_upload(upload: UploadFile) -> MediaUpload:
    content = await upload.read(MAX_MEDIA_BYTES + 1)
    if len(content) > MAX_MEDIA_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"{upload.filename or 'media'} exceeds the 50 MB media limit",
        )
    return MediaUpload(
        data=content,
        media_type=upload.content_type or "application/octet-stream",
        filename=upload.filename or "upload",
    )


def _validated(model: type[ModelT], payload: Mapping[str, Any]) -> ModelT:
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        details = [
            {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
            for item in error.errors()
        ]
        raise HTTPException(status_code=422, detail=details) from error


def _extract(
    backend: MultimodalBackend,
    text: str,
    media: tuple[MediaUpload, ...],
) -> MultimodalOutput:
    try:
        payload = backend.extract(text, media)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except MultimodalUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    try:
        return MultimodalOutput.model_validate(payload)
    except ValidationError as error:
        raise HTTPException(
            status_code=503,
            detail="multimodal extractor returned invalid model output",
        ) from error


def _score(
    scorer: RustScorer,
    candidate: CandidateInput,
    score_calibration: ScoreCalibrationConfig | None = None,
) -> dict[str, Any]:
    payload = candidate.model_dump(
        mode="json", exclude_none=True, exclude={"text", "media_attached"}
    )
    try:
        config = score_calibration.to_payload() if score_calibration else None
        return scorer.score(payload, score_calibration=config)
    except ScorerUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _score_percentile_output(
    result: dict[str, Any],
) -> tuple[float | None, str, PercentileBounds | None]:
    status = result.get("score_calibration_status", "unavailable")
    percentile = result.get("model_score_percentile")
    raw_bounds = result.get("model_score_percentile_bounds")
    if status == "unavailable":
        if percentile is not None or raw_bounds is not None:
            raise HTTPException(
                status_code=503,
                detail="Rust scorer returned percentile data without score calibration",
            )
        return None, "unavailable", None
    if status not in {"empirical", "log_normal"}:
        raise HTTPException(
            status_code=503,
            detail="Rust scorer returned an unknown score calibration status",
        )
    if (
        not isinstance(percentile, (float, int))
        or isinstance(percentile, bool)
        or not 0 <= percentile <= 100
    ):
        raise HTTPException(
            status_code=503,
            detail="Rust scorer returned an invalid model score percentile",
        )
    bounds = None
    if raw_bounds is not None:
        try:
            bounds = PercentileBounds.model_validate(raw_bounds)
        except ValidationError as error:
            raise HTTPException(
                status_code=503,
                detail="Rust scorer returned invalid model score percentile bounds",
            ) from error
        if not bounds.lower <= float(percentile) <= bounds.upper:
            raise HTTPException(
                status_code=503,
                detail="Rust scorer returned bounds that exclude the model score percentile",
            )
    return float(percentile), status, bounds


def _score_output(result: dict[str, Any]) -> ScoreOutput:
    return ScoreOutput(
        weighted_score=result["weighted_score"],
        final_score=result["final_score"],
        author_diversity_multiplier=result["author_diversity_multiplier"],
        out_of_network_multiplier=result["out_of_network_multiplier"],
        dropped_by=result["dropped_by"],
    )


def _alert_components():
    """Load the sibling radar package in editable and installed deployments."""
    try:
        from xreach_radar.alerts import (
            AlertRule,
            AlertRuleEngine,
            WebhookDispatcher,
            WebhookTarget,
        )
    except ModuleNotFoundError as error:
        if error.name != "xreach_radar" and not str(error.name).startswith(
            "xreach_radar."
        ):
            raise
        radar_source = Path(__file__).resolve().parents[3] / "radar" / "src"
        sys.path.insert(0, str(radar_source))
        from xreach_radar.alerts import (
            AlertRule,
            AlertRuleEngine,
            WebhookDispatcher,
            WebhookTarget,
        )
    return AlertRule, AlertRuleEngine, WebhookDispatcher, WebhookTarget


def _require_alert_admin(app: FastAPI, supplied: str | None) -> None:
    expected = app.state.alert_admin_token
    if expected is None:
        raise HTTPException(
            status_code=503,
            detail="alert administration is disabled; configure XREACH_ALERT_ADMIN_TOKEN",
        )
    if supplied is None or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid alert admin token")


def _alert_configuration_response(app: FastAPI) -> AlertConfigurationResponse:
    return AlertConfigurationResponse(
        rules=[
            AlertRuleConfig(
                rule_id=rule.rule_id,
                min_views_per_minute=rule.min_views_per_minute,
                max_age_minutes=rule.max_age_minutes,
                simclusters=sorted(rule.simclusters),
            )
            for rule in app.state.alert_rules
        ],
        webhooks=[
            {"webhook_id": webhook_id, "format": target.format}
            for webhook_id, target in app.state.alert_webhooks.items()
        ],
    )


async def _dispatch_seed_alerts(app: FastAPI, seed: Mapping[str, Any]) -> None:
    if not app.state.alert_rules or not app.state.alert_webhooks:
        return
    try:
        _, AlertRuleEngine, WebhookDispatcher, _ = _alert_components()
        events = AlertRuleEngine(app.state.alert_rules).evaluate(seed)
        if not events:
            return
        dispatcher = WebhookDispatcher(list(app.state.alert_webhooks.values()))
        for event in events:
            results = await dispatcher.dispatch(event)
            if not all(results):
                logger.warning(
                    "one or more alert webhooks failed for event %s", event.event_id
                )
    except Exception:
        logger.exception(
            "failed to dispatch radar alert for seed %s", seed.get("tweet_id")
        )


app = create_app()
