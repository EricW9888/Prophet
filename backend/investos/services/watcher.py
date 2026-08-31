from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.watcher import ActiveWatcher, WatcherEvidenceEvaluation
from investos.services.agent_action_log import AgentActionLogService
from investos.services.knowledge_audit import KnowledgeAuditService
from investos.services.market_data import MarketDataService
from investos.services.push_notification import PushNotificationService
from investos.services.watcher_evidence import WatcherEvidenceService


class WatcherService:
    PRICE_CONDITIONS = {"price_above", "price_below"}
    OBJECTIVE_INSENSITIVE_CONDITIONS = {"earnings_release", "news_sentiment"}

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_active(self) -> list[ActiveWatcher]:
        watchers = list(
            (
                await self.session.execute(
                    select(ActiveWatcher).where(ActiveWatcher.is_active == True)
                )
            )
            .scalars()
            .all()
        )
        far_future = datetime.max.replace(tzinfo=UTC)
        watchers.sort(
            key=lambda watcher: (
                watcher.deadline is None,
                self._aware_datetime(watcher.deadline) or far_future,
                -(
                    self._aware_datetime(watcher.created_at)
                    or datetime.min.replace(tzinfo=UTC)
                ).timestamp(),
            )
        )
        return watchers

    async def list_active_with_countdowns(self) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        watchers = await self.list_active()
        evaluations = await WatcherEvidenceService(self.session).latest_evaluations(
            watchers
        )
        return [
            self.to_response(
                watcher,
                now=now,
                evaluation=evaluations.get(watcher.id),
            )
            for watcher in watchers
        ]

    async def list_reminders(self) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        return [
            self.to_response(watcher, now=now)
            for watcher in await self.list_active()
            if watcher.deadline is not None
        ]

    @classmethod
    def to_response(
        cls,
        watcher: ActiveWatcher,
        *,
        now: datetime | None = None,
        evaluation: WatcherEvidenceEvaluation | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        deadline = cls._aware_datetime(watcher.deadline)
        countdown_seconds = None
        is_overdue = False
        if deadline is not None:
            remaining_seconds = int((deadline - now).total_seconds())
            countdown_seconds = max(0, remaining_seconds)
            is_overdue = watcher.status == "pending" and remaining_seconds <= 0

        has_deadline = deadline is not None
        has_condition = bool(watcher.condition_type) and watcher.condition_type not in {
            "deadline",
            "reminder",
        }
        if has_deadline and has_condition:
            reminder_kind = "deadline_and_condition"
        elif has_deadline:
            reminder_kind = "deadline"
        else:
            reminder_kind = "condition"

        return {
            "id": watcher.id,
            "source": watcher.source,
            "source_id": watcher.source_id,
            "ticker": watcher.ticker,
            "entity_id": watcher.entity_id,
            "condition_type": watcher.condition_type,
            "condition_params_json": watcher.condition_params_json,
            "objective": watcher.objective,
            "adjustment_plan": watcher.adjustment_plan,
            "deadline": deadline,
            "status": watcher.status,
            "is_active": watcher.is_active,
            "last_checked_at": cls._aware_datetime(watcher.last_checked_at),
            "triggered_at": cls._aware_datetime(watcher.triggered_at),
            "trigger_detail": watcher.trigger_detail,
            "created_at": cls._aware_datetime(watcher.created_at),
            "countdown_seconds": countdown_seconds,
            "is_overdue": is_overdue,
            "reminder_kind": reminder_kind,
            "evaluation_status": evaluation.status if evaluation else None,
            "evaluation_detail": evaluation.detail if evaluation else None,
            "evaluation_evidence_refs": (
                evaluation.evidence_refs_json if evaluation else []
            ),
            "evaluation_error": evaluation.error if evaluation else None,
        }

    @staticmethod
    def _aware_datetime(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    async def register_watcher(
        self,
        *,
        source: str,
        source_id: UUID | None = None,
        ticker: str | None = None,
        entity_id: UUID | None = None,
        condition_type: str,
        condition_params: dict[str, Any],
        objective: str,
        adjustment_plan: str,
        deadline: datetime | None = None,
    ) -> ActiveWatcher:
        params = dict(condition_params or {})
        status = "pending"
        is_active = True
        trigger_detail = None
        if condition_type in self.PRICE_CONDITIONS:
            threshold, error = self._price_threshold(params)
            if error:
                status = "failed"
                is_active = False
                trigger_detail = error
            else:
                params["threshold"] = threshold

        if is_active and status == "pending":
            duplicate = await self._active_duplicate_for(
                ticker=ticker,
                entity_id=entity_id,
                condition_type=condition_type,
                condition_params=params,
                objective=objective,
                adjustment_plan=adjustment_plan,
            )
            if duplicate is not None:
                return duplicate

        watcher = ActiveWatcher(
            source=source,
            source_id=source_id,
            ticker=ticker.upper() if ticker else None,
            entity_id=entity_id,
            condition_type=condition_type,
            condition_params_json=params,
            objective=objective,
            adjustment_plan=adjustment_plan,
            deadline=deadline,
            is_active=is_active,
            status=status,
            trigger_detail=trigger_detail,
        )
        self.session.add(watcher)
        await self.session.commit()
        await self.session.refresh(watcher)
        return watcher

    async def deduplicate_active_watchers(
        self, *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Deactivate equivalent active watches while preserving one canonical row.

        The agent may revisit the same thesis several times through chat,
        research, and shadow loops. Those loops can legitimately point at the
        same catalyst, but the user-facing live-watch surface should show one
        operational watch per target/condition/objective/action plan.
        """
        watchers = list(
            (
                await self.session.execute(
                    select(ActiveWatcher)
                    .where(
                        ActiveWatcher.is_active == True,
                        ActiveWatcher.status == "pending",
                    )
                    .order_by(ActiveWatcher.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        groups: dict[tuple[str, ...], list[ActiveWatcher]] = {}
        for watcher in watchers:
            groups.setdefault(self._semantic_key(watcher), []).append(watcher)

        now = datetime.now(UTC)
        duplicate_groups: list[dict[str, Any]] = []
        for group in groups.values():
            if len(group) < 2:
                continue
            group.sort(
                key=lambda item: item.created_at or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )
            kept = group[0]
            removed = group[1:]
            record = {
                "kept_id": str(kept.id),
                "ticker": kept.ticker,
                "condition_type": kept.condition_type,
                "objective": kept.objective,
                "removed_count": len(removed),
                "removed_ids": [str(item.id) for item in removed],
            }
            duplicate_groups.append(record)
            if dry_run:
                continue
            for watcher in removed:
                watcher.is_active = False
                watcher.status = "superseded"
                watcher.last_checked_at = now
                watcher.trigger_detail = (
                    f"Superseded by equivalent active watcher {kept.id} created at {kept.created_at.isoformat()}."
                    if kept.created_at
                    else f"Superseded by equivalent active watcher {kept.id}."
                )
                await KnowledgeAuditService(self.session).record_change(
                    node_type="watcher",
                    node_id=watcher.id,
                    change_type="deduplicated_watcher",
                    reason="Watcher hygiene collapsed equivalent active watches into one live catalyst.",
                    actor="watcher_hygiene",
                    source_type="watcher",
                    source_id=kept.id,
                    subject_type="watcher",
                    subject_id=watcher.id,
                    metadata={
                        "kept_watcher_id": str(kept.id),
                        "ticker": watcher.ticker,
                        "condition_type": watcher.condition_type,
                        "condition_params": watcher.condition_params_json or {},
                        "objective": watcher.objective,
                    },
                )
            AgentActionLogService.append(
                source="watcher",
                action_type="hygiene",
                status="ok",
                summary=(
                    f"Watcher hygiene collapsed {len(removed)} duplicate active watch(es) "
                    f"for {kept.ticker or 'untargeted'}: {kept.objective}"
                ),
                subject_id=str(kept.id),
                subject_type="watcher",
                metadata=record,
            )

        if duplicate_groups and not dry_run:
            await self.session.commit()
        return {
            "scanned": len(watchers),
            "duplicate_group_count": len(duplicate_groups),
            "deduplicated_count": sum(
                item["removed_count"] for item in duplicate_groups
            ),
            "duplicate_groups": duplicate_groups,
            "dry_run": dry_run,
        }

    async def evaluate_watchers(self) -> int:
        """
        Main evaluation loop. Checks all active watchers against live data.
        Returns the number of triggered or expired watchers.
        """
        active = await self.list_active()
        if not active:
            return 0

        triggered_count = 0
        now = datetime.now(UTC)

        # Resolve each ticker once, but evaluate every watcher so deadline-only
        # reminders are not skipped just because they have no market symbol.
        prices: dict[str, float | None] = {}
        for w in active:
            if not w.ticker or w.ticker in prices:
                continue
            try:
                price_data = await MarketDataService(self.session).get_live_price(
                    w.ticker
                )
                prices[w.ticker] = price_data.get("price") if price_data else None
            except Exception:
                prices[w.ticker] = None

        for w in active:
            triggered = False
            trigger_detail = ""
            price = prices.get(w.ticker) if w.ticker else None
            evaluated = False

            # 1. Check Deadline
            if w.deadline and now >= w.deadline:
                w.status = "expired"
                w.is_active = False
                w.trigger_detail = f"Deadline reached: {w.deadline.isoformat()}"
                triggered = True
                evaluated = True

            # 2. Check Price Conditions
            elif price is not None and w.condition_type in self.PRICE_CONDITIONS:
                threshold, error = self._price_threshold(w.condition_params_json)
                if error:
                    self._fail_invalid_watcher(w, error, now)
                    continue
                evaluated = True

                if w.condition_type == "price_above" and price >= threshold:
                    triggered = True
                    trigger_detail = f"Price {price} hit threshold >= {threshold}"
                elif w.condition_type == "price_below" and price <= threshold:
                    triggered = True
                    trigger_detail = f"Price {price} hit threshold <= {threshold}"

            if triggered:
                if w.status == "pending":  # Only set if not already expired.
                    w.status = "triggered"
                w.is_active = False
                w.triggered_at = now
                w.trigger_detail = trigger_detail or w.trigger_detail

                AgentActionLogService.append(
                    source="watcher",
                    action_type="trigger",
                    status="ok",
                    summary=f"Watcher triggered for {w.ticker}: {w.objective}",
                    subject_id=str(w.id),
                    subject_type="watcher",
                    metadata={
                        "trigger_detail": w.trigger_detail,
                        "adjustment_plan": w.adjustment_plan,
                    },
                )
                await PushNotificationService(self.session).enqueue_watch_transition(w)
                triggered_count += 1

            if evaluated:
                w.last_checked_at = now

        await self.session.commit()
        triggered_count += await self.retry_deferred_evidence_evaluations(limit=6)
        return triggered_count

    async def evaluate_new_evidence(
        self,
        *,
        subject_id: UUID,
        subject_type: str,
        raw_evidence_id: UUID,
    ) -> int:
        return await WatcherEvidenceService(self.session).evaluate_new_evidence(
            subject_id=subject_id,
            subject_type=subject_type,
            raw_evidence_id=raw_evidence_id,
        )

    async def retry_deferred_evidence_evaluations(self, *, limit: int = 6) -> int:
        return await WatcherEvidenceService(self.session).retry_deferred_evaluations(
            limit=limit
        )

    @classmethod
    def _price_threshold(
        cls, params: dict[str, Any] | None
    ) -> tuple[float, str | None]:
        if not isinstance(params, dict):
            return 0.0, "Invalid price watcher: condition parameters must be an object."
        raw_threshold = params.get("threshold")
        if raw_threshold is None or raw_threshold == "":
            return 0.0, "Invalid price watcher: missing numeric threshold."
        try:
            return float(raw_threshold), None
        except (TypeError, ValueError):
            return (
                0.0,
                f"Invalid price watcher: non-numeric threshold {raw_threshold!r}.",
            )

    @staticmethod
    def _fail_invalid_watcher(
        watcher: ActiveWatcher, detail: str, checked_at: datetime
    ) -> None:
        watcher.status = "failed"
        watcher.is_active = False
        watcher.last_checked_at = checked_at
        watcher.trigger_detail = detail
        AgentActionLogService.append(
            source="watcher",
            action_type="validation",
            status="error",
            summary=f"Watcher disabled for {watcher.ticker or 'unknown ticker'}: {detail}",
            subject_id=str(watcher.id),
            subject_type="watcher",
            metadata={
                "condition_type": watcher.condition_type,
                "condition_params": watcher.condition_params_json,
            },
        )

    async def _active_duplicate_for(
        self,
        *,
        ticker: str | None,
        entity_id: UUID | None,
        condition_type: str,
        condition_params: dict[str, Any],
        objective: str,
        adjustment_plan: str,
    ) -> ActiveWatcher | None:
        if not hasattr(self.session, "execute"):
            return None
        target_ticker = ticker.upper() if ticker else None
        stmt = select(ActiveWatcher).where(
            ActiveWatcher.is_active == True,
            ActiveWatcher.status == "pending",
            ActiveWatcher.condition_type == condition_type,
        )
        if target_ticker:
            stmt = stmt.where(ActiveWatcher.ticker == target_ticker)
        else:
            stmt = stmt.where(
                ActiveWatcher.ticker.is_(None),
                (
                    ActiveWatcher.entity_id == entity_id
                    if entity_id
                    else ActiveWatcher.entity_id.is_(None)
                ),
            )
        desired_key = self._semantic_key_from_values(
            ticker=target_ticker,
            entity_id=entity_id,
            condition_type=condition_type,
            condition_params=condition_params,
            objective=objective,
            adjustment_plan=adjustment_plan,
        )
        candidates = list(
            (await self.session.execute(stmt.order_by(ActiveWatcher.created_at.desc())))
            .scalars()
            .all()
        )
        for watcher in candidates:
            if self._semantic_key(watcher) == desired_key:
                return watcher
        return None

    @classmethod
    def _semantic_key(cls, watcher: ActiveWatcher) -> tuple[str, ...]:
        return cls._semantic_key_from_values(
            ticker=watcher.ticker,
            entity_id=watcher.entity_id,
            condition_type=watcher.condition_type,
            condition_params=watcher.condition_params_json or {},
            objective=watcher.objective,
            adjustment_plan=watcher.adjustment_plan,
        )

    @classmethod
    def _semantic_key_from_values(
        cls,
        *,
        ticker: str | None,
        entity_id: UUID | None,
        condition_type: str,
        condition_params: dict[str, Any],
        objective: str,
        adjustment_plan: str,
    ) -> tuple[str, ...]:
        target_key = (ticker or "").upper() or str(entity_id or "")
        normalized_condition = (condition_type or "").casefold()
        if normalized_condition in cls.PRICE_CONDITIONS:
            params_key = cls._normalized_params_key(
                {"threshold": (condition_params or {}).get("threshold")}
            )
        elif normalized_condition in cls.OBJECTIVE_INSENSITIVE_CONDITIONS:
            params_key = ""
        else:
            params_key = cls._normalized_params_key(
                {
                    "condition_params": condition_params or {},
                    "objective": cls._normalized_text_key(objective),
                    "adjustment_plan": cls._normalized_text_key(adjustment_plan),
                }
            )
        return (
            target_key,
            normalized_condition,
            params_key,
        )

    @staticmethod
    def _normalized_text_key(value: str | None) -> str:
        return " ".join((value or "").casefold().split())

    @staticmethod
    def _normalized_params_key(params: dict[str, Any] | None) -> str:
        if not isinstance(params, dict):
            return ""
        return json.dumps(
            WatcherService._normalize_param_value(params),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _normalize_param_value(value: Any) -> Any:
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            return round(float(value), 6)
        if isinstance(value, dict):
            return {
                str(key): WatcherService._normalize_param_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [WatcherService._normalize_param_value(item) for item in value]
        return value
