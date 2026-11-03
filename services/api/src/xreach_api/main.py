"""FastAPI endpoints for scoring a draft and comparing publication surfaces."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypeVar

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ValidationError
from starlette.datastructures import UploadFile

from .calibration import ReachCalibrationConfig
from .models import (
    AnalyzeRequest,
    AnalyzeResponse,
    AutomaticAnalyzeRequest,
    CandidateInput,
    MultimodalOutput,
    PlacementRequest,
    ScoreOutput,
)
from .multimodal import (
    MediaUpload,
    MultimodalBackend,
    MultimodalService,
    MultimodalUnavailable,
)
from .rust_scorer import RustScorer, ScorerUnavailable

MAX_MEDIA_BYTES = 50 * 1024 * 1024
ModelT = TypeVar("ModelT", bound=BaseModel)


def create_app(
    scorer: RustScorer | None = None,
    multimodal: MultimodalBackend | None = None,
    reach_calibration: ReachCalibrationConfig | None = None,
) -> FastAPI:
    calibration = (
        reach_calibration
        if reach_calibration is not None
        else ReachCalibrationConfig.from_environment()
    )
    app = FastAPI(
        title="X-Reach API",
        version="0.1.0",
        description=(
            "Extracts local multimodal draft signals and scores their Phoenix "
            "prediction heads with the Rust core. Reach percentiles are available "
            "when a historical score calibration is configured."
        ),
    )
    app.state.scorer = scorer or RustScorer()
    app.state.multimodal = multimodal or MultimodalService()
    app.state.reach_calibration = calibration

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "scoring_engine": "rust-core"}

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
            _score, app.state.scorer, data, app.state.reach_calibration
        )
        reach_percentile, reach_status = _reach_output(rust_result)
        return AnalyzeResponse(
            algorithmic_viability_score=rust_result["final_score"],
            reach_potential_percentile=reach_percentile,
            reach_calibration_status=reach_status,
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
                        "Reach percentile is unavailable until a calibration model is configured."
                    ]
                    if reach_status == "unavailable"
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
            semantic_id=extracted.semantic_id if extracted else None,
            predicted_probabilities=extracted.predictions if extracted else None,
            modalities=extracted.modalities if extracted else [],
            keyframes=extracted.keyframes if extracted else [],
            video_duration_seconds=(
                extracted.video_duration_seconds if extracted else None
            ),
            quantizer_status=extracted.quantizer_status if extracted else None,
            propensity_status=extracted.propensity_status if extracted else None,
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
        profile_result = _score(
            app.state.scorer, profile, app.state.reach_calibration
        )
        reply_result = _score(
            app.state.scorer, root_reply, app.state.reach_calibration
        )
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
            else profile_result if surface == "PROFILE_POST" else None
        )
        reach_percentile, reach_status = (
            _reach_output(recommended_result)
            if recommended_result is not None
            else (None, "unavailable")
        )
        return {
            "recommended_surface": surface,
            "reasoning": reasoning,
            "reach_potential_percentile": reach_percentile,
            "reach_calibration_status": reach_status,
            "scores": {
                "profile": {
                    **_score_output(profile_result).model_dump(),
                    "reach_potential_percentile": _reach_output(profile_result)[0],
                    "reach_calibration_status": _reach_output(profile_result)[1],
                },
                "direct_root_reply": {
                    **_score_output(reply_result).model_dump(),
                    "reach_potential_percentile": _reach_output(reply_result)[0],
                    "reach_calibration_status": _reach_output(reply_result)[1],
                },
            },
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
    reach_calibration: ReachCalibrationConfig | None = None,
) -> dict[str, Any]:
    payload = candidate.model_dump(
        mode="json", exclude_none=True, exclude={"text", "media_attached"}
    )
    try:
        config = reach_calibration.to_payload() if reach_calibration else None
        return scorer.score(payload, reach_calibration=config)
    except ScorerUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _reach_output(result: dict[str, Any]) -> tuple[float | None, str]:
    status = result.get("reach_calibration_status", "unavailable")
    percentile = result.get("reach_potential_percentile")
    if status == "unavailable":
        return None, "unavailable"
    if status not in {"empirical", "log_normal"}:
        raise HTTPException(
            status_code=503,
            detail="Rust scorer returned an unknown reach calibration status",
        )
    if (
        not isinstance(percentile, (float, int))
        or isinstance(percentile, bool)
        or not 0 <= percentile <= 100
    ):
        raise HTTPException(
            status_code=503, detail="Rust scorer returned an invalid reach percentile"
        )
    return float(percentile), status


def _score_output(result: dict[str, Any]) -> ScoreOutput:
    return ScoreOutput(
        weighted_score=result["weighted_score"],
        final_score=result["final_score"],
        author_diversity_multiplier=result["author_diversity_multiplier"],
        out_of_network_multiplier=result["out_of_network_multiplier"],
        dropped_by=result["dropped_by"],
    )


app = create_app()
