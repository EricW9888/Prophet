from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable
from urllib.parse import urlsplit

CONFIDENCE_ORDER = ["very_low", "low", "medium", "high", "very_high"]
SIGNATURE_TOKEN_RE = re.compile(r"[a-z0-9]+")
SIGNATURE_HEX_RE = re.compile(r"[0-9a-f]{16}", re.I)
SIGNATURE_SHINGLE_SIZE = 4
SIGNATURE_MIN_TOKENS = 40


def near_duplicate_signature(value: object) -> tuple[str | None, int]:
    """Return a standard SimHash-style signature for substantive text."""
    text = str(value or "").casefold()[:200_000]
    tokens = SIGNATURE_TOKEN_RE.findall(text)
    token_count = len(tokens)
    if token_count < SIGNATURE_MIN_TOKENS:
        return None, token_count
    features = Counter(
        " ".join(tokens[index : index + SIGNATURE_SHINGLE_SIZE])
        for index in range(token_count - SIGNATURE_SHINGLE_SIZE + 1)
    )
    vector = [0] * 64
    for feature, weight in features.items():
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        bits = int.from_bytes(digest, "big")
        for index in range(64):
            vector[index] += weight if bits & (1 << index) else -weight
    fingerprint = sum(1 << index for index, score in enumerate(vector) if score >= 0)
    return f"{fingerprint:016x}", token_count


def near_duplicate_distance(first: object, second: object) -> int | None:
    first_text = str(first or "").strip()
    second_text = str(second or "").strip()
    if not SIGNATURE_HEX_RE.fullmatch(first_text) or not SIGNATURE_HEX_RE.fullmatch(
        second_text
    ):
        return None
    try:
        return (int(first_text, 16) ^ int(second_text, 16)).bit_count()
    except (TypeError, ValueError):
        return None


def _safe_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalized_publisher_host(value: object) -> str | None:
    """Return a conservative publisher identity without guessing at ownership."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text if "://" in text else f"https://{text}")
    except ValueError:
        return None
    host = (parsed.hostname or "").strip(".").casefold()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def source_authority(source_type: object, metadata: object = None) -> str:
    """Classify provenance conservatively, preferring explicit ingestion metadata."""
    payload = metadata if isinstance(metadata, dict) else {}
    explicit = (
        str(
            payload.get("source_authority")
            or payload.get("directness")
            or payload.get("evidence_directness")
            or ""
        )
        .strip()
        .casefold()
    )
    if explicit in {"primary", "secondary", "tertiary"}:
        return explicit
    if payload.get("is_primary_source") is True:
        return "primary"
    normalized_type = str(source_type or "").strip().casefold()
    if normalized_type in {"filing", "official"}:
        return "primary"
    return "secondary"


def source_lineage_key(
    *,
    source_id: object,
    source_url: object,
    evidence_url: object,
    metadata: object = None,
) -> str:
    """Collapse known aliases and same-publisher copies without inventing ownership."""
    payload = metadata if isinstance(metadata, dict) else {}
    for key in (
        "canonical_source_id",
        "origin_source_id",
        "upstream_source_id",
        "publisher_id",
        "syndication_origin_id",
    ):
        value = str(payload.get(key) or "").strip().casefold()
        if value:
            return f"lineage:{value}"
    for key in ("canonical_source_url", "origin_url", "upstream_url"):
        host = normalized_publisher_host(payload.get(key))
        if host:
            return f"publisher:{host}"
    host = normalized_publisher_host(source_url) or normalized_publisher_host(
        evidence_url
    )
    if host:
        return f"publisher:{host}"
    return f"source:{str(source_id)}" if source_id else "source:unknown"


def build_source_provenance(
    *,
    source_id: object,
    source_type: object,
    source_url: object,
    source_item_id: object,
    raw_evidence_id: object,
    evidence_url: object,
    content_hash: object,
    public_time: object,
    event_time: object,
    ingest_time: object,
    metadata: object = None,
) -> dict:
    payload = metadata if isinstance(metadata, dict) else {}
    return {
        "source_id": str(source_id) if source_id else None,
        "source_item_id": str(source_item_id) if source_item_id else None,
        "raw_evidence_id": str(raw_evidence_id) if raw_evidence_id else None,
        "content_hash": str(content_hash or "").strip() or None,
        "near_duplicate_signature": str(
            payload.get("near_duplicate_signature") or ""
        ).strip()
        or None,
        "signature_token_count": _safe_nonnegative_int(
            payload.get("signature_token_count")
        ),
        "publisher_host": (
            normalized_publisher_host(source_url)
            or normalized_publisher_host(evidence_url)
        ),
        "lineage_key": source_lineage_key(
            source_id=source_id,
            source_url=source_url,
            evidence_url=evidence_url,
            metadata=payload,
        ),
        "authority": source_authority(source_type, payload),
        "public_time": public_time.isoformat() if public_time else None,
        "event_time": event_time.isoformat() if event_time else None,
        "ingest_time": ingest_time.isoformat() if ingest_time else None,
    }


class CorroborationService:
    """Apply source-independent promotion rules to model-selected assertions."""

    def __init__(
        self,
        *,
        minimum_independent_sources: int = 2,
        near_duplicate_max_distance: int = 3,
    ):
        self.minimum_independent_sources = max(2, int(minimum_independent_sources))
        self.near_duplicate_max_distance = max(0, int(near_duplicate_max_distance))

    def assess_result(self, result: dict, packet_context: dict) -> dict:
        node_index = self._node_index(packet_context)
        assertions = self._material_assertions(result)
        assessed = [self._assess_assertion(item, node_index) for item in assertions]
        material_assumptions = self._assess_material_assumptions(result, node_index)

        unresolved_assumptions = [
            item for item in material_assumptions if item["status"] != "corroborated"
        ]
        promotable = (
            bool(assessed)
            and all(item["status"] == "corroborated" for item in assessed)
            and not unresolved_assumptions
        )
        statuses = {item["status"] for item in assessed}
        if promotable:
            status = "corroborated"
            confidence_cap = None
        elif "scope_mismatch" in statuses or "disputed" in statuses:
            status = "disputed"
            confidence_cap = "low"
        elif "unsupported" in statuses or unresolved_assumptions:
            status = "insufficient_support"
            confidence_cap = "very_low"
        elif statuses:
            status = "single_source"
            confidence_cap = "low"
        else:
            status = "no_material_assertions"
            confidence_cap = "very_low"

        unique_sources = {
            key for item in assessed for key in item.get("independent_support_keys", [])
        }
        duplicate_count = sum(
            int(item.get("duplicate_copy_count") or 0) for item in assessed
        )
        assessment = {
            "policy": "independent_source_corroboration_v1",
            "status": status,
            "minimum_independent_sources": self.minimum_independent_sources,
            "independent_supporting_source_count": len(unique_sources),
            "duplicate_copy_count": duplicate_count,
            "material_assertion_count": len(assessed),
            "unresolved_material_assumption_count": len(unresolved_assumptions),
            "can_promote": promotable,
            "confidence_cap": confidence_cap,
            "assertions": assessed,
            "material_assumptions": material_assumptions,
        }
        result["corroboration"] = assessment
        if confidence_cap:
            result["confidence_band"] = self._cap_confidence(
                result.get("confidence_band"), confidence_cap
            )
        return assessment

    def apply_independent_review(self, result: dict, review: dict | None) -> None:
        if not review:
            return
        result["independent_review"] = review
        primary_stance = str(result.get("stance") or "uncertain")
        review_stance = str(review.get("candidate_stance") or "uncertain")
        conclusive = {"bullish", "bearish", "neutral"}
        disagreement = (
            primary_stance in conclusive
            and review_stance in conclusive
            and primary_stance != review_stance
        )
        review["stance_disagrees"] = disagreement
        if not disagreement:
            return
        assessment = result.setdefault("corroboration", {})
        assessment["status"] = "analyst_disagreement"
        assessment["can_promote"] = False
        assessment["confidence_cap"] = "low"
        result["confidence_band"] = self._cap_confidence(
            result.get("confidence_band"), "low"
        )

    def _assess_assertion(self, assertion: dict, node_index: dict[str, dict]) -> dict:
        support_nodes = self._nodes_for_ids(
            assertion.get("supporting_evidence_ids") or [], node_index
        )
        contradiction_nodes = self._nodes_for_ids(
            assertion.get("contradicting_evidence_ids") or [], node_index
        )
        support_groups, duplicate_count = self._independent_groups(support_nodes)
        contradiction_groups, _ = self._independent_groups(contradiction_nodes)
        scope_status = str(assertion.get("scope_consistency") or "unknown")

        if scope_status == "mixed":
            status = "scope_mismatch"
        elif contradiction_groups:
            status = "disputed"
        elif len(support_groups) >= self.minimum_independent_sources:
            status = "corroborated"
        elif support_groups:
            status = "single_source"
        else:
            status = "unsupported"

        primary_count = sum(
            1
            for group in support_groups.values()
            if any(
                str((node.get("source") or {}).get("authority") or "") == "primary"
                for node in group
            )
        )
        return {
            "statement": str(assertion.get("statement") or "").strip(),
            "subject_scope": str(assertion.get("subject_scope") or "").strip() or None,
            "time_scope": str(assertion.get("time_scope") or "").strip() or None,
            "scope_consistency": scope_status,
            "scope_notes": str(assertion.get("scope_notes") or "").strip() or None,
            "status": status,
            "supporting_evidence_ids": [str(node.get("id")) for node in support_nodes],
            "contradicting_evidence_ids": [
                str(node.get("id")) for node in contradiction_nodes
            ],
            "independent_supporting_source_count": len(support_groups),
            "independent_contradicting_source_count": len(contradiction_groups),
            "primary_source_count": primary_count,
            "duplicate_copy_count": duplicate_count,
            "independent_support_keys": sorted(support_groups),
        }

    def _assess_material_assumptions(
        self, result: dict, node_index: dict[str, dict]
    ) -> list[dict]:
        output: list[dict] = []
        for item in result.get("assumptions") or []:
            if not isinstance(item, dict) or not bool(item.get("is_material")):
                continue
            nodes = self._nodes_for_ids(item.get("evidence_ids") or [], node_index)
            groups, duplicate_count = self._independent_groups(nodes)
            status = (
                "corroborated"
                if len(groups) >= self.minimum_independent_sources
                else "single_source" if groups else "unsupported"
            )
            output.append(
                {
                    "statement": str(item.get("statement") or "").strip(),
                    "status": status,
                    "independent_source_count": len(groups),
                    "duplicate_copy_count": duplicate_count,
                    "falsifier": str(item.get("falsifier") or "").strip() or None,
                }
            )
        return output

    def _material_assertions(self, result: dict) -> list[dict]:
        assertions = [
            item
            for item in (result.get("material_assertions") or [])
            if isinstance(item, dict) and str(item.get("statement") or "").strip()
        ]
        if assertions:
            return assertions
        support_ids = result.get("supporting_evidence_ids") or []
        contradiction_ids = result.get("contradicting_evidence_ids") or []
        if not support_ids and not contradiction_ids:
            return []
        return [
            {
                "statement": result.get("thesis_summary")
                or result.get("reasoning")
                or "",
                "subject_scope": None,
                "time_scope": None,
                "scope_consistency": "unknown",
                "scope_notes": "Legacy aggregate evidence list; assertion scope was not supplied.",
                "supporting_evidence_ids": support_ids,
                "contradicting_evidence_ids": contradiction_ids,
            }
        ]

    @staticmethod
    def _node_index(packet_context: dict) -> dict[str, dict]:
        nodes: list[dict] = []
        for key in (
            "direct_evidence",
            "connected_evidence",
            "historical_evidence",
            "contradiction_evidence",
        ):
            nodes.extend(
                item
                for item in (packet_context.get(key) or [])
                if isinstance(item, dict)
            )
        return {str(node.get("id")): node for node in nodes if node.get("id")}

    @staticmethod
    def _nodes_for_ids(
        values: Iterable[object], node_index: dict[str, dict]
    ) -> list[dict]:
        output: list[dict] = []
        seen: set[str] = set()
        for value in values:
            key = str(value)
            if key in seen or key not in node_index:
                continue
            seen.add(key)
            output.append(node_index[key])
        return output

    def _independent_groups(
        self, nodes: list[dict]
    ) -> tuple[dict[str, list[dict]], int]:
        groups: dict[str, list[dict]] = {}
        seen_hashes: set[str] = set()
        seen_signatures: list[tuple[str, int]] = []
        duplicate_count = 0
        for node in nodes:
            source = node.get("source") if isinstance(node.get("source"), dict) else {}
            lineage_key = str(source.get("lineage_key") or "").strip()
            if not lineage_key:
                continue
            content_hash = str(source.get("content_hash") or "").strip().casefold()
            if content_hash and content_hash in seen_hashes:
                duplicate_count += 1
                continue
            signature = str(source.get("near_duplicate_signature") or "").strip()
            token_count = _safe_nonnegative_int(source.get("signature_token_count"))
            if signature and token_count >= SIGNATURE_MIN_TOKENS:
                is_near_duplicate = any(
                    prior_token_count >= SIGNATURE_MIN_TOKENS
                    and (
                        distance := near_duplicate_distance(signature, prior_signature)
                    )
                    is not None
                    and distance <= self.near_duplicate_max_distance
                    for prior_signature, prior_token_count in seen_signatures
                )
                if is_near_duplicate:
                    duplicate_count += 1
                    continue
            if content_hash:
                seen_hashes.add(content_hash)
            if signature and token_count >= SIGNATURE_MIN_TOKENS:
                seen_signatures.append((signature, token_count))
            groups.setdefault(lineage_key, []).append(node)
        return groups, duplicate_count

    @staticmethod
    def _cap_confidence(value: object, cap: str) -> str:
        normalized = str(value or "very_low")
        if normalized not in CONFIDENCE_ORDER:
            normalized = "very_low"
        return CONFIDENCE_ORDER[
            min(CONFIDENCE_ORDER.index(normalized), CONFIDENCE_ORDER.index(cap))
        ]
