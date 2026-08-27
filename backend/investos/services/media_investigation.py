from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from investos.config import settings
from investos.core.llm import call_llm_json, compact_exception_message

MEDIA_INVESTIGATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "materiality": {
            "type": "string",
            "enum": ["low", "medium", "high"],
        },
        "first_pass_sufficient": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
        "resolved_points": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
        },
        "unresolved_gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "description": {"type": "string"},
                    "why_material": {"type": "string"},
                    "recommended_pass": {
                        "type": ["string", "null"],
                        "enum": [
                            "audio_transcription",
                            "frame_ocr",
                            "external_verification",
                            None,
                        ],
                    },
                },
                "required": [
                    "description",
                    "why_material",
                    "recommended_pass",
                ],
            },
            "maxItems": 8,
        },
        "requested_passes": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "audio_transcription",
                    "frame_ocr",
                    "external_verification",
                ],
            },
            "maxItems": 3,
            "uniqueItems": True,
        },
        "followup_questions": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 6,
        },
    },
    "required": [
        "materiality",
        "first_pass_sufficient",
        "confidence",
        "reason",
        "resolved_points",
        "unresolved_gaps",
        "requested_passes",
        "followup_questions",
    ],
}


class MediaInvestigationPlanner:
    """Assess whether a media representation warrants a bounded deeper pass."""

    async def assess(
        self,
        *,
        transcript: str,
        representation: str,
        title: str | None,
        source_url: str,
        prior_assessment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not settings.YOUTUBE_ADAPTIVE_INVESTIGATION_ENABLED:
            return {
                "status": "disabled",
                "representation": representation,
                "policy_version": 1,
                "requested_passes": [],
                "followup_questions": [],
            }

        max_chars = max(1000, int(settings.YOUTUBE_INVESTIGATION_MAX_TRANSCRIPT_CHARS))
        excerpt = transcript[:max_chars]
        try:
            result = await call_llm_json(
                system_prompt=(
                    "Evaluate the adequacy of one attributable media representation for investment research. "
                    "Judge the supplied text on what it actually resolves; do not infer unseen video, audio, charts, "
                    "slides, or sources. Request audio transcription only when caption errors or omissions could change "
                    "a material conclusion. Request frame_ocr only when referenced visual information is necessary to "
                    "evaluate a material claim. Request external_verification for claims that require independent "
                    "corroboration. A successful first pass may still warrant a focused follow-up, but do not request "
                    "more work merely for completeness. Return specific unresolved questions rather than generic topics."
                ),
                user_prompt=json.dumps(
                    {
                        "title": title,
                        "source_url": source_url,
                        "representation": representation,
                        "transcript_length": len(transcript),
                        "transcript_excerpt": excerpt,
                        "transcript_truncated": len(transcript) > len(excerpt),
                        "prior_assessment": prior_assessment,
                    },
                    ensure_ascii=True,
                    default=str,
                ),
                schema=MEDIA_INVESTIGATION_SCHEMA,
                timeout_seconds=max(
                    10, int(settings.YOUTUBE_INVESTIGATION_TIMEOUT_SECONDS)
                ),
            )
        except Exception as exc:
            return {
                "status": "deferred",
                "representation": representation,
                "policy_version": 1,
                "requested_passes": [],
                "followup_questions": [],
                "reason": compact_exception_message(exc),
                "assessed_at": datetime.now(UTC).isoformat(),
            }

        requested_passes = list(dict.fromkeys(result.get("requested_passes") or []))
        return {
            "status": "complete",
            "representation": representation,
            "policy_version": 1,
            "transcript_length": len(transcript),
            "transcript_excerpt_length": len(excerpt),
            "assessed_at": datetime.now(UTC).isoformat(),
            **result,
            "requested_passes": requested_passes,
        }
