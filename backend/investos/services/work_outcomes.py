"""User-visible work outcomes from retained records, never method completion."""


def model_run_outcome(run):
    label = str(run.model_used or "").lower()
    output = getattr(run, "structured_output_json", None) or {}
    if label.startswith(("cache_hit:", "cached:")):
        return "cache_hit"
    if label.startswith("deferred:"):
        return "deferred"
    if label.startswith("fallback:") or output.get("is_fallback") is True:
        return "fallback"
    if label.startswith(("failed:", "error:")):
        return "failed"
    from investos.core.providers import llm_provider_capability

    if llm_provider_capability(label.split(":", 1)[0].split("->", 1)[0]):
        return "live_success"
    return "unclassified"


def evidence_work_outcome(detail):
    detail = detail or {}
    processing = detail.get("processing") or {}
    if detail.get("outcome") == "provider_deferred" or detail.get("deferred"):
        return "deferred"
    if processing.get("extraction_status") in {
        "retry_scheduled",
        "retry_exhausted",
        "pending",
        "running",
    }:
        return "deferred"
    if processing.get("extraction_status") == "not_applicable":
        return "skipped"
    if (
        detail.get("quarantined")
        or processing.get("investment_object_status") == "quarantined"
    ):
        return "rejected"
    if detail.get("missing_raw_content") or detail.get("degraded"):
        return "failed"
    if (
        processing.get("persisted_object_count", 0) > 0
        and processing.get("investment_object_status") == "completed"
    ):
        return "knowledge_promoted"
    if processing.get("extraction_status") == "completed":
        return "enriched"
    return "no_new_knowledge"


def group_research_operations(entries):
    groups = {}
    result = []
    # Entries are newest first. Legacy rows without an operation ID remain
    # independent: proximity and wording are not proof of the same request.
    for entry in entries:
        operation = (entry.get("metadata") or {}).get("operation_id")
        if not operation:
            result.append(entry)
            continue
        if operation not in groups:
            grouped = {
                **entry,
                "phase": "operation",
                "phases": [],
                "estimated_credits": 0,
            }
            groups[operation] = grouped
            result.append(grouped)
        grouped = groups[operation]
        grouped["phases"].append(entry)
        grouped["estimated_credits"] += int(entry.get("estimated_credits") or 0)
    for grouped in groups.values():
        grouped["phases"].reverse()
    return result
