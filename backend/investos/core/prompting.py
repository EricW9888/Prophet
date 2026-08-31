from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str
    )


def estimate_tokens_from_text(text: str) -> int:
    compact = " ".join(text.split())
    if not compact:
        return 0
    return max(1, (len(compact) + 3) // 4)


def estimate_tokens_from_payload(payload: Any) -> int:
    return estimate_tokens_from_text(canonical_json(payload))


def hash_llm_request(
    *,
    label: str,
    system_prompt: str,
    user_payload: Any,
    schema: dict[str, Any],
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "label": label,
                "system_prompt": system_prompt,
                "user_payload": user_payload,
                "schema": schema,
            }
        ).encode("utf-8")
    ).hexdigest()


def compact_text(value: str | None, max_chars: int = 240) -> str:
    text = " ".join((value or "").split())
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


def compact_context_text(value: str | None, max_chars: int = 320) -> str:
    text = " ".join((value or "").split())
    if len(text) <= max_chars:
        return text
    head_chars = max(80, int(max_chars * 0.68))
    tail_chars = max(40, max_chars - head_chars - 7)
    head = text[:head_chars].rstrip()
    tail = text[-tail_chars:].lstrip()
    return f"{head} [...] {tail}"


def bounded_document_excerpt(
    text: str | None,
    *,
    head_chars: int = 3200,
    tail_chars: int = 1200,
) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= head_chars + tail_chars + 32:
        return compact
    head = compact[:head_chars].rstrip()
    tail = compact[-tail_chars:].lstrip()
    return f"{head}\n[...excerpt trimmed for budget...]\n{tail}"


def compact_evidence_nodes(
    nodes: list[dict[str, Any]] | None,
    *,
    max_items: int = 6,
    max_chars: int = 320,
) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for node in (nodes or [])[:max_items]:
        source = compact_node_source(node.get("source"))
        compacted.append(
            {
                "id": str(node.get("id")),
                "type": node.get("type"),
                "tier": node.get("tier"),
                "importance": node.get("importance"),
                "contradiction_role": node.get("contradiction_role"),
                "text": compact_context_text(node.get("text"), max_chars=max_chars),
                "created_at": node.get("created_at"),
                **({"source": source} if source else {}),
            }
        )
    return compacted


def compact_node_source(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    feedback = (
        source.get("feedback") if isinstance(source.get("feedback"), dict) else {}
    )
    compacted: dict[str, Any] = {
        "name": compact_context_text(source.get("name"), max_chars=120),
        "type": source.get("type"),
        "is_trusted": source.get("is_trusted"),
        "evidence_title": compact_context_text(
            source.get("evidence_title"), max_chars=160
        ),
    }
    if source.get("url"):
        compacted["url"] = source.get("url")
    quality = source.get("quality") if isinstance(source.get("quality"), dict) else {}
    if quality:
        compacted["quality"] = {
            "quality_score": quality.get("quality_score"),
            "originality_score": quality.get("originality_score"),
            "timing_usefulness": quality.get("timing_usefulness"),
            "evidence_count": quality.get("evidence_count"),
            "notes": compact_context_text(quality.get("notes"), max_chars=180),
            "last_evaluated": quality.get("last_evaluated"),
        }
    trust_profile = (
        source.get("trust_profile")
        if isinstance(source.get("trust_profile"), dict)
        else {}
    )
    if trust_profile:
        compacted["trust_profile"] = {
            "factual_reliability": trust_profile.get("factual_reliability"),
            "noise_ratio": trust_profile.get("noise_ratio"),
            "trust_trajectory": trust_profile.get("trust_trajectory"),
            "correction_quality": trust_profile.get("correction_quality"),
        }
    value_profile = (
        source.get("value_profile")
        if isinstance(source.get("value_profile"), dict)
        else {}
    )
    if value_profile:
        compacted["value_profile"] = {
            "timing_value": value_profile.get("timing_value"),
            "portfolio_relevance_value": value_profile.get("portfolio_relevance_value"),
            "specificity": value_profile.get("specificity"),
            "originality": value_profile.get("originality"),
        }
    if feedback:
        compacted["feedback"] = {
            "rating": feedback.get("rating"),
            "note": compact_context_text(feedback.get("note"), max_chars=180),
            "flagged_at": feedback.get("flagged_at"),
        }
    return {key: value for key, value in compacted.items() if value}


def compact_source_feedback(
    feedback: dict[str, Any] | None, *, max_items: int = 4
) -> dict[str, Any]:
    if not isinstance(feedback, dict):
        return {}
    raw_counts = (
        feedback.get("counts") if isinstance(feedback.get("counts"), dict) else {}
    )
    counts = {
        "useful": int(raw_counts.get("useful") or 0),
        "not_useful": int(raw_counts.get("not_useful") or 0),
    }
    recent: list[dict[str, Any]] = []
    for item in (feedback.get("recent") or [])[:max_items]:
        if not isinstance(item, dict):
            continue
        recent.append(
            {
                "rating": item.get("rating"),
                "source_name": compact_context_text(
                    item.get("source_name"), max_chars=120
                ),
                "source_type": item.get("source_type"),
                "title": compact_context_text(item.get("title"), max_chars=160),
                "note": compact_context_text(item.get("note"), max_chars=220),
                "context": compact_context_text(item.get("context"), max_chars=120),
                "flagged_at": item.get("flagged_at"),
            }
        )
    if counts["useful"] == 0 and counts["not_useful"] == 0 and not recent:
        return {}
    return {
        "counts": counts,
        "recent": recent,
        "instruction": (
            "Use this user feedback as retrieval preference context: prefer similarly specific useful sources, "
            "and down-rank not-useful sources unless corroborated by stronger direct evidence."
        ),
    }


def compact_packet_context(
    packet_context: dict[str, Any],
    *,
    max_items_per_layer: int = 6,
    max_chars: int = 320,
) -> dict[str, Any]:
    portfolio_context = packet_context.get("portfolio_context") or {}
    source_feedback_context = compact_source_feedback(
        portfolio_context.get("source_feedback")
    )
    fresh_research = packet_context.get("fresh_research_context")
    compact_fresh_research: dict[str, Any] = {}
    if isinstance(fresh_research, dict) and fresh_research:
        compact_fresh_research = {
            "required": fresh_research.get("required"),
            "reason": compact_context_text(fresh_research.get("reason"), max_chars=220),
            "searched": fresh_research.get("searched"),
            "status": fresh_research.get("status"),
            "query": compact_context_text(fresh_research.get("query"), max_chars=240),
            "checked_at": fresh_research.get("checked_at"),
            "results": [
                {
                    "title": compact_context_text(item.get("title"), max_chars=180),
                    "url": item.get("url"),
                    "published_date": item.get("published_date"),
                    "content": compact_context_text(
                        item.get("content"), max_chars=max_chars
                    ),
                    "score": item.get("score"),
                }
                for item in (fresh_research.get("results") or [])[:4]
                if isinstance(item, dict)
            ],
        }
        compact_fresh_research = {
            key: value
            for key, value in compact_fresh_research.items()
            if value not in (None, "", [])
        }
    return {
        "query_text": compact_context_text(
            packet_context.get("query_text"), max_chars=260
        ),
        "subject_type": packet_context.get("subject_type"),
        "subject_id": str(packet_context.get("subject_id")),
        "subject_name": packet_context.get("subject_name"),
        "coverage": packet_context.get("coverage") or {},
        "gap_flags": (packet_context.get("gap_flags") or [])[:6],
        "portfolio_context": portfolio_context,
        "source_feedback_context": source_feedback_context,
        "fresh_research_context": compact_fresh_research,
        "research_plan": (
            {
                "original_question": compact_context_text(
                    (packet_context.get("research_plan") or {}).get(
                        "original_question"
                    ),
                    max_chars=260,
                ),
                "research_objective": compact_context_text(
                    (packet_context.get("research_plan") or {}).get(
                        "research_objective"
                    ),
                    max_chars=260,
                ),
                "retrieval_query": compact_context_text(
                    (packet_context.get("research_plan") or {}).get("retrieval_query"),
                    max_chars=260,
                ),
                "information_needs": [
                    compact_context_text(item, max_chars=180)
                    for item in (
                        (packet_context.get("research_plan") or {}).get(
                            "information_needs"
                        )
                        or []
                    )[:6]
                ],
                "external_research_required": (
                    packet_context.get("research_plan") or {}
                ).get("external_research_required"),
                "research_mode": (packet_context.get("research_plan") or {}).get(
                    "research_mode"
                ),
                "portfolio_context_role": (
                    packet_context.get("research_plan") or {}
                ).get("portfolio_context_role"),
                "use_historical_analogies": (
                    packet_context.get("research_plan") or {}
                ).get("use_historical_analogies"),
                "reason": compact_context_text(
                    (packet_context.get("research_plan") or {}).get("reason"),
                    max_chars=220,
                ),
            }
            if packet_context.get("research_plan")
            else {}
        ),
        "opportunity_context": (
            {
                "universe_size": (packet_context.get("opportunity_context") or {}).get(
                    "universe_size"
                ),
                "enabled_universe_size": (
                    packet_context.get("opportunity_context") or {}
                ).get("enabled_universe_size"),
                "scan_started": (packet_context.get("opportunity_context") or {}).get(
                    "scan_started"
                ),
                "scan_status": (packet_context.get("opportunity_context") or {}).get(
                    "scan_status"
                ),
                "scan_detail": compact_context_text(
                    (packet_context.get("opportunity_context") or {}).get(
                        "scan_detail"
                    ),
                    max_chars=220,
                ),
                "candidate_count": (
                    packet_context.get("opportunity_context") or {}
                ).get("candidate_count"),
                "coverage_note": compact_context_text(
                    (packet_context.get("opportunity_context") or {}).get(
                        "coverage_note"
                    ),
                    max_chars=220,
                ),
                "candidates": [
                    {
                        "id": str(item.get("id")) if item.get("id") else None,
                        "ticker": item.get("ticker"),
                        "title": compact_context_text(item.get("title"), max_chars=160),
                        "status": item.get("status"),
                        "priority_score": item.get("priority_score"),
                        "signal_stage": item.get("signal_stage"),
                        "why_now": compact_context_text(
                            item.get("why_now"), max_chars=220
                        ),
                        "investable_thesis": compact_context_text(
                            item.get("investable_thesis"),
                            max_chars=220,
                        ),
                        "portfolio_transmission": compact_context_text(
                            item.get("portfolio_transmission"), max_chars=180
                        ),
                        "falsification_tests": [
                            compact_context_text(test, max_chars=160)
                            for test in (item.get("falsification_tests") or [])[:3]
                        ],
                        "evidence_ref_count": len(item.get("evidence_refs") or []),
                    }
                    for item in (
                        (packet_context.get("opportunity_context") or {}).get(
                            "candidates"
                        )
                        or []
                    )[:8]
                    if isinstance(item, dict)
                ],
            }
            if packet_context.get("opportunity_context")
            else {}
        ),
        "conversation_context": (
            {
                "subject_name": (packet_context.get("conversation_context") or {}).get(
                    "subject_name"
                ),
                "subject_type": (packet_context.get("conversation_context") or {}).get(
                    "subject_type"
                ),
                "recent_turns": [
                    {
                        "role": item.get("role"),
                        "content": compact_context_text(
                            item.get("content"), max_chars=max_chars
                        ),
                        "stance": item.get("stance"),
                        "confidence_band": item.get("confidence_band"),
                        "process_mode": item.get("process_mode"),
                    }
                    for item in (
                        (packet_context.get("conversation_context") or {}).get(
                            "recent_turns"
                        )
                        or []
                    )[-max_items_per_layer:]
                ],
            }
            if packet_context.get("conversation_context")
            else {}
        ),
        "direct_evidence_count": len(packet_context.get("direct_evidence") or []),
        "connected_evidence_count": len(packet_context.get("connected_evidence") or []),
        "historical_evidence_count": len(
            packet_context.get("historical_evidence") or []
        ),
        "historical_analogies": [
            {
                "name": compact_context_text(analogy.get("name"), max_chars=140),
                "period": analogy.get("period"),
                "episode_type": analogy.get("episode_type"),
                "dominant_channel": compact_context_text(
                    analogy.get("dominant_channel"), max_chars=max_chars
                ),
                "lesson": compact_context_text(
                    analogy.get("lesson"), max_chars=max_chars
                ),
                "match_score": analogy.get("match_score"),
            }
            for analogy in (packet_context.get("historical_analogies") or [])[:3]
        ],
        "historical_analogy_lenses": [
            {
                "name": compact_context_text(lens.get("name"), max_chars=140),
                "period": lens.get("period"),
                "lens_use_policy": compact_context_text(
                    lens.get("lens_use_policy"), max_chars=max_chars
                ),
                "current_application_prompt": compact_context_text(
                    lens.get("current_application_prompt"),
                    max_chars=max_chars,
                ),
                "what_rhymes": compact_context_text(
                    lens.get("what_rhymes"), max_chars=max_chars
                ),
                "dominant_channel_test": compact_context_text(
                    lens.get("dominant_channel_test"),
                    max_chars=max_chars,
                ),
                "where_analogy_breaks": compact_context_text(
                    lens.get("where_analogy_breaks"),
                    max_chars=max_chars,
                ),
                "portfolio_transmission": compact_context_text(
                    lens.get("portfolio_transmission"),
                    max_chars=max_chars,
                ),
                "best_next_check": compact_context_text(
                    lens.get("best_next_check"), max_chars=max_chars
                ),
                "investor_questions": [
                    compact_context_text(str(question), max_chars=max_chars)
                    for question in (lens.get("investor_questions") or [])[:4]
                    if str(question).strip()
                ],
            }
            for lens in (packet_context.get("historical_analogy_lenses") or [])[:3]
        ],
        "contradiction_evidence_count": len(
            packet_context.get("contradiction_evidence") or []
        ),
        "lesson_count": len(packet_context.get("lessons") or []),
        "direct_evidence": compact_evidence_nodes(
            packet_context.get("direct_evidence"),
            max_items=max_items_per_layer,
            max_chars=max_chars,
        ),
        "connected_evidence": compact_evidence_nodes(
            packet_context.get("connected_evidence"),
            max_items=max_items_per_layer,
            max_chars=max_chars,
        ),
        "historical_evidence": compact_evidence_nodes(
            packet_context.get("historical_evidence"),
            max_items=max_items_per_layer,
            max_chars=max_chars,
        ),
        "contradiction_evidence": compact_evidence_nodes(
            packet_context.get("contradiction_evidence"),
            max_items=max_items_per_layer,
            max_chars=max_chars,
        ),
        "lessons": [
            {
                "id": str(lesson.get("id")),
                "title": compact_context_text(lesson.get("title"), max_chars=140),
                "summary": compact_context_text(
                    lesson.get("summary"), max_chars=max_chars
                ),
                "lesson_type": lesson.get("lesson_type"),
                "maturity_status": lesson.get("maturity_status"),
                "confidence_score": lesson.get("confidence_score"),
                "observation_counts": {
                    "supporting": lesson.get("supporting_observations", 0),
                    "contradicting": lesson.get("contradicting_observations", 0),
                    "neutral": lesson.get("neutral_observations", 0),
                },
                "applicable_sectors": (lesson.get("applicable_sectors") or [])[:4],
                "applicable_regimes": (lesson.get("applicable_regimes") or [])[:4],
                "usage_count": lesson.get("usage_count"),
                "created_at": lesson.get("created_at"),
            }
            for lesson in (packet_context.get("lessons") or [])[:max_items_per_layer]
        ],
    }


def compact_reasoning_result(reasoning_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "stance": reasoning_result.get("stance"),
        "confidence_band": reasoning_result.get("confidence_band"),
        "thesis_summary": compact_text(
            reasoning_result.get("thesis_summary"), max_chars=260
        ),
        "reasoning": compact_text(reasoning_result.get("reasoning"), max_chars=320),
        "what_would_falsify": (reasoning_result.get("what_would_falsify") or [])[:3],
        "what_would_strengthen": (reasoning_result.get("what_would_strengthen") or [])[
            :3
        ],
        "supporting_evidence_ids": (
            reasoning_result.get("supporting_evidence_ids") or []
        )[:5],
        "contradicting_evidence_ids": (
            reasoning_result.get("contradicting_evidence_ids") or []
        )[:5],
        "active_contradictions": (reasoning_result.get("active_contradictions") or [])[
            :3
        ],
    }
