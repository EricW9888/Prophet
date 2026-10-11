"""Shared stored/derived lifecycle facts; no provider calls or inferred prices."""

from datetime import UTC, datetime

from investos.core.dates import parse_iso_datetime

ACTIVE_SHADOW_STATUSES = ("queued", "pending", "running")
TERMINAL_SHADOW_STATUSES = ("completed", "failed", "skipped", "superseded")


def lifecycle_state(experiment, *, now=None):
    now = now or datetime.now(UTC)
    stored = "queued" if experiment.run_status == "pending" else experiment.run_status
    if stored not in {"queued", "running"}:
        return stored
    end = parse_iso_datetime(getattr(experiment, "end_point", None))
    if end is not None and end <= now:
        return "overdue"
    start = parse_iso_datetime(getattr(experiment, "start_point", None))
    if start is not None and start > now:
        return "waiting"
    state = (
        experiment.final_portfolio_state_json
        or experiment.initial_portfolio_state_json
        or {}
    )
    details = state.get("run_details") or {}
    checkpoint = parse_iso_datetime(
        (details.get("progress") or {}).get("next_checkpoint_at")
    )
    if stored == "queued":
        return "due"
    if (
        details.get("pending_evidence_events")
        or checkpoint is None
        or checkpoint <= now
    ):
        return "due"
    return "waiting"


def lifecycle_counts(experiments, *, now=None):
    now = now or datetime.now(UTC)
    counts = {
        key: 0
        for key in (
            "total",
            "active",
            "queued",
            "running",
            "waiting",
            "due",
            "overdue",
            "completed",
            "failed",
            "skipped",
            "superseded",
            "manual",
        )
    }
    for experiment in experiments:
        counts["total"] += 1
        stored = (
            "queued" if experiment.run_status == "pending" else experiment.run_status
        )
        if stored in counts:
            counts[stored] += 1
        if stored in {"queued", "running"}:
            counts["active"] += 1
            counts[lifecycle_state(experiment, now=now)] += 1
    return counts
