"""FastAPI endpoints for scoring a draft and comparing publication surfaces."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError

from .models import (
    AnalyzeRequest,
    AnalyzeResponse,
    CandidateInput,
    PlacementRequest,
    ScoreOutput,
)
from .rust_scorer import RustScorer, ScorerUnavailable


def create_app(scorer: RustScorer | None = None) -> FastAPI:
    app = FastAPI(
        title="X-Reach API",
        version="0.1.0",
        description=(
            "Scores caller-supplied Phoenix prediction heads with the Rust core. "
            "No calibrated reach-percentile model is configured."
        ),
    )
    app.state.scorer = scorer or RustScorer()

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "scoring_engine": "rust-core"}

    @app.post("/api/v1/analyze", response_model=AnalyzeResponse)
    async def analyze(request: Request) -> AnalyzeResponse:
        payload = await _analysis_payload(request)
        try:
            data = AnalyzeRequest.model_validate(payload)
        except ValidationError as error:
            details = [
                {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
                for item in error.errors()
            ]
            raise HTTPException(status_code=422, detail=details) from error

        rust_result = await run_in_threadpool(_score, app.state.scorer, data)
        return AnalyzeResponse(
            algorithmic_viability_score=rust_result["final_score"],
            score=_score_output(rust_result),
            placement_recommendation={
                "surface": "UNDETERMINED",
                "reasoning": (
                    "A single draft score does not compare surfaces. Submit profile "
                    "and direct-root-reply predictions to /api/v1/placement."
                ),
            },
            suggested_tweaks=[
                "Reach percentile is unavailable until a calibration model is configured.",
                *(
                    ["Review visibility drops before considering either placement."]
                    if rust_result["dropped_by"]
                    else []
                ),
            ],
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
        profile_result = _score(app.state.scorer, profile)
        reply_result = _score(app.state.scorer, root_reply)
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
        return {
            "recommended_surface": surface,
            "reasoning": reasoning,
            "reach_potential_percentile": None,
            "reach_calibration_status": "unavailable",
            "scores": {
                "profile": _score_output(profile_result).model_dump(),
                "direct_root_reply": _score_output(reply_result).model_dump(),
            },
        }

    return app


async def _analysis_payload(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    try:
        if content_type.startswith("application/json"):
            value = await request.json()
            if not isinstance(value, dict):
                raise ValueError("JSON request body must be an object")
            return value
        if content_type.startswith("multipart/form-data"):
            async with request.form(max_files=4, max_fields=20) as form:
                predictions = form.get("predictions")
                if not isinstance(predictions, str):
                    raise ValueError(
                        "multipart requests must include JSON-encoded predictions"
                    )
                value = json.loads(predictions)
                if not isinstance(value, dict):
                    raise ValueError("predictions must be a JSON object")
                # Attached media is transport-only; an upstream model layer
                # provides its predicted action heads separately.
                return {
                    "text": str(form.get("text", "")),
                    "predictions": value,
                    "media_attached": any(
                        getattr(upload, "filename", "")
                        for upload in form.getlist("media")
                    ),
                }
    except (json.JSONDecodeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    raise HTTPException(
        status_code=415,
        detail="Use application/json or multipart/form-data with predictions.",
    )


def _score(scorer: RustScorer, candidate: CandidateInput) -> dict[str, Any]:
    payload = candidate.model_dump(
        mode="json", exclude_none=True, exclude={"text", "media_attached"}
    )
    try:
        return scorer.score(payload)
    except ScorerUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _score_output(result: dict[str, Any]) -> ScoreOutput:
    return ScoreOutput(
        weighted_score=result["weighted_score"],
        final_score=result["final_score"],
        author_diversity_multiplier=result["author_diversity_multiplier"],
        out_of_network_multiplier=result["out_of_network_multiplier"],
        dropped_by=result["dropped_by"],
    )


app = create_app()
