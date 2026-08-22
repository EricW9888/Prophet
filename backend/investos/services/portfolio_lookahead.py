from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.core.dates import lookahead_calendar_datetime
from investos.models.entity import Entity, Security
from investos.models.graph import Edge
from investos.models.knowledge import Event
from investos.models.portfolio import Position
from investos.models.watcher import ActiveWatcher
from investos.services.research import ResearchService
from investos.services.runtime_settings import RuntimeSettingsStore
from investos.services.watcher import WatcherService


class PortfolioLookaheadService:
    """Build a dated catalyst view for portfolio-level "what should I watch" turns."""

    LOOKAHEAD_DAYS = 10
    CALENDAR_WATCH_TERMS = {
        "approval",
        "calendar",
        "catalyst",
        "conference",
        "earnings",
        "event",
        "filing",
        "guidance",
        "launch",
        "news",
        "report",
        "regulatory",
        "sentiment",
        "thesis",
    }
    INVESTOR_EVENT_TERMS = {
        "announcement",
        "call",
        "contract",
        "deal",
        "earnings",
        "guidance",
        "partnership",
        "preannounce",
        "price reaction",
        "report",
        "results",
        "revision",
        "transcript",
    }

    def __init__(self, session: AsyncSession):
        self.session = session

    @classmethod
    def looks_like_lookahead_request(cls, message: str) -> bool:
        """Fallback intent guard; the LLM tool router is still the primary classifier."""
        normalized = " ".join(
            re.sub(r"[^a-z0-9\s]", " ", (message or "").lower()).split()
        )
        if not normalized:
            return False
        temporal_terms = {
            "today",
            "tomorrow",
            "week",
            "next week",
            "upcoming",
            "coming up",
            "calendar",
            "scheduled",
            "wednesday",
            "thursday",
            "friday",
            "monday",
            "tuesday",
        }
        attention_terms = {
            "watch",
            "look forward",
            "pay attention",
            "focus",
            "event",
            "events",
            "earnings",
            "report",
            "reports",
            "catalyst",
            "catalysts",
            "deadline",
            "reminder",
        }
        return any(term in normalized for term in temporal_terms) and any(
            term in normalized for term in attention_terms
        )

    async def build_payload(
        self,
        *,
        message: str,
        days: int | None = None,
        run_live_scan: bool = False,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        horizon_days = max(1, min(int(days or self.LOOKAHEAD_DAYS), 21))
        horizon = now + timedelta(days=horizon_days)
        positions = await self._positions()
        watchers = await WatcherService(self.session).list_active_with_countdowns()
        calendar_resolution: dict[str, Any] = {
            "attempted": False,
            "updated_count": 0,
            "unresolved_count": 0,
            "items": [],
        }
        if run_live_scan:
            calendar_resolution = await self._resolve_undated_calendar_watches(
                watchers=watchers,
                positions=positions,
                now=now,
                horizon=horizon,
            )
            if calendar_resolution.get("updated_count"):
                watchers = await WatcherService(
                    self.session
                ).list_active_with_countdowns()
        scheduled_events = await self._scheduled_events(
            positions=positions, now=now, horizon=horizon
        )

        research_result: dict[str, Any] | None = None
        if run_live_scan and RuntimeSettingsStore.load().research.api_key:
            research_result = await self._run_live_scan(
                message=message,
                positions=positions,
                now=now,
                horizon=horizon,
            )
            if research_result.get("started") and research_result.get("processed"):
                scheduled_events = await self._scheduled_events(
                    positions=positions,
                    now=now,
                    horizon=horizon,
                )

        attention_items = self._attention_items(
            positions=positions,
            scheduled_events=scheduled_events,
            watchers=watchers,
            now=now,
            horizon=horizon,
        )
        return {
            "as_of": now.isoformat(),
            "horizon_end": horizon.isoformat(),
            "horizon_days": horizon_days,
            "message": message,
            "positions": positions[:12],
            "attention_items": attention_items[:12],
            "scheduled_events": scheduled_events[:12],
            "active_deadline_watches": [
                watcher for watcher in watchers if watcher.get("deadline") is not None
            ][:12],
            "calendar_resolution": calendar_resolution,
            "research": research_result
            or {
                "started": False,
                "reason": (
                    "live_scan_not_requested"
                    if not run_live_scan
                    else "research_provider_not_configured"
                ),
                "query": self._scan_query(
                    positions=positions, now=now, horizon=horizon
                ),
            },
        }

    async def _positions(self) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(Position, Security, Entity)
                .join(Security, Position.security_id == Security.id)
                .join(Entity, Security.entity_id == Entity.id)
                .where(Position.list_type.in_(["holding", "watchlist", "considering"]))
            )
        ).all()
        positions = [
            {
                "position_id": str(position.id),
                "entity_id": str(entity.id),
                "security_id": str(security.id),
                "ticker": security.ticker,
                "name": entity.name,
                "list_type": position.list_type,
                "weight_pct": float(position.weight_pct or 0.0),
                "market_value": float(position.market_value or 0.0),
                "sector": entity.sector,
                "industry": entity.industry,
            }
            for position, security, entity in rows
        ]
        return sorted(
            positions,
            key=lambda item: (
                item["list_type"] == "holding",
                float(item.get("weight_pct") or 0.0),
                float(item.get("market_value") or 0.0),
            ),
            reverse=True,
        )

    async def _scheduled_events(
        self,
        *,
        positions: list[dict[str, Any]],
        now: datetime,
        horizon: datetime,
    ) -> list[dict[str, Any]]:
        rows = (
            (
                await self.session.execute(
                    select(Event)
                    .where(
                        Event.is_deprecated.is_(False),
                        Event.event_time.is_not(None),
                        Event.event_time >= now,
                        Event.event_time <= horizon,
                    )
                    .order_by(Event.event_time.asc(), Event.created_at.desc())
                    .limit(250)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return []

        event_ids = [event.id for event in rows]
        linked_edges = (
            (
                await self.session.execute(
                    select(Edge).where(
                        Edge.source_type == "event", Edge.source_id.in_(event_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        linked_by_event: dict[str, set[str]] = {}
        for edge in linked_edges:
            linked_by_event.setdefault(str(edge.source_id), set()).add(
                str(edge.target_id)
            )

        output: list[dict[str, Any]] = []
        for event in rows:
            linked_ids = linked_by_event.get(str(event.id), set())
            matched_positions = self._matched_positions(
                event=event, positions=positions, linked_ids=linked_ids
            )
            if not matched_positions:
                continue
            primary = matched_positions[0]
            output.append(
                {
                    "event_id": str(event.id),
                    "ticker": primary.get("ticker"),
                    "name": primary.get("name"),
                    "title": event.title,
                    "description": event.description,
                    "event_type": event.event_type,
                    "event_time": (
                        event.event_time.isoformat() if event.event_time else None
                    ),
                    "weight_pct": primary.get("weight_pct"),
                    "matched_positions": matched_positions[:4],
                }
            )
        return sorted(
            output,
            key=lambda item: (
                item.get("event_time") or "",
                -float(item.get("weight_pct") or 0.0),
            ),
        )

    @staticmethod
    def _matched_positions(
        *,
        event: Event,
        positions: list[dict[str, Any]],
        linked_ids: set[str],
    ) -> list[dict[str, Any]]:
        text = f"{event.title} {event.description or ''}".lower()
        matched: list[dict[str, Any]] = []
        for position in positions:
            ticker = str(position.get("ticker") or "")
            name = str(position.get("name") or "")
            entity_id = str(position.get("entity_id") or "")
            security_id = str(position.get("security_id") or "")
            text_match = bool(
                ticker
                and re.search(
                    rf"(?<![a-z0-9]){re.escape(ticker.lower())}(?![a-z0-9])", text
                )
            ) or bool(name and name.lower() in text)
            linked_match = (
                entity_id in linked_ids
                or security_id in linked_ids
                or str(position.get("position_id") or "") in linked_ids
            )
            if text_match or linked_match:
                matched.append(position)
        return sorted(
            matched,
            key=lambda item: (
                float(item.get("weight_pct") or 0.0),
                float(item.get("market_value") or 0.0),
            ),
            reverse=True,
        )

    def _attention_items(
        self,
        *,
        positions: list[dict[str, Any]],
        scheduled_events: list[dict[str, Any]],
        watchers: list[dict[str, Any]],
        now: datetime,
        horizon: datetime,
    ) -> list[dict[str, Any]]:
        weight_by_ticker = {
            str(position.get("ticker") or "").upper(): float(
                position.get("weight_pct") or 0.0
            )
            for position in positions
        }
        items: list[dict[str, Any]] = []
        for event in scheduled_events:
            event_time = str(event.get("event_time") or "")
            items.append(
                {
                    "source": "scheduled_event",
                    "ticker": event.get("ticker"),
                    "title": event.get("title"),
                    "event_type": event.get("event_type"),
                    "due_at": event_time,
                    "countdown_seconds": self._seconds_until(event_time, now=now),
                    "portfolio_weight_pct": event.get("weight_pct"),
                    "why_it_matters": self._why_event_matters(
                        event, weight_by_ticker=weight_by_ticker
                    ),
                    "investment_lens": self._event_investment_lens(
                        ticker=event.get("ticker"),
                        event_type=event.get("event_type"),
                        title=event.get("title"),
                        description=event.get("description"),
                        objective=None,
                        adjustment_plan=None,
                        weight_pct=event.get("weight_pct"),
                    ),
                }
            )
        for watcher in watchers:
            due_at = watcher.get("deadline")
            ticker = str(watcher.get("ticker") or "").upper()
            if not due_at:
                if self._is_undated_calendar_watch(watcher):
                    items.append(
                        {
                            "source": "active_watch_date_missing",
                            "ticker": ticker or None,
                            "title": watcher.get("objective"),
                            "event_type": watcher.get("condition_type"),
                            "due_at": None,
                            "countdown_seconds": None,
                            "date_status": "missing",
                            "portfolio_weight_pct": weight_by_ticker.get(ticker),
                            "if_it_fires": self._meaningful_text(
                                watcher.get("adjustment_plan")
                            ),
                            "why_it_matters": self._why_undated_watch_matters(
                                watcher,
                                weight_by_ticker=weight_by_ticker,
                            ),
                            "investment_lens": self._event_investment_lens(
                                ticker=ticker or None,
                                event_type=watcher.get("condition_type"),
                                title=watcher.get("objective"),
                                description=None,
                                objective=watcher.get("objective"),
                                adjustment_plan=watcher.get("adjustment_plan"),
                                weight_pct=weight_by_ticker.get(ticker),
                            ),
                        }
                    )
                continue
            due_time = self._parse_time(str(due_at))
            if due_time is not None and due_time > horizon:
                continue
            items.append(
                {
                    "source": "active_watch",
                    "ticker": ticker or None,
                    "title": watcher.get("objective"),
                    "event_type": watcher.get("condition_type"),
                    "due_at": (
                        due_at.isoformat()
                        if hasattr(due_at, "isoformat")
                        else str(due_at)
                    ),
                    "countdown_seconds": watcher.get("countdown_seconds"),
                    "portfolio_weight_pct": weight_by_ticker.get(ticker),
                    "if_it_fires": self._meaningful_text(
                        watcher.get("adjustment_plan")
                    ),
                    "why_it_matters": self._why_watcher_matters(
                        watcher, weight_by_ticker=weight_by_ticker
                    ),
                    "investment_lens": self._event_investment_lens(
                        ticker=ticker or None,
                        event_type=watcher.get("condition_type"),
                        title=watcher.get("objective"),
                        description=None,
                        objective=watcher.get("objective"),
                        adjustment_plan=watcher.get("adjustment_plan"),
                        weight_pct=weight_by_ticker.get(ticker),
                    ),
                }
            )
        ranked_items = sorted(
            items,
            key=lambda item: (
                item.get("countdown_seconds") is None,
                self._countdown_sort_value(item.get("countdown_seconds")),
                -float(item.get("portfolio_weight_pct") or 0.0),
            ),
        )
        return self._dedupe_and_diversify_attention(ranked_items)

    @classmethod
    def _dedupe_and_diversify_attention(
        cls, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        collapsed: list[dict[str, Any]] = []
        duplicate_counts: dict[tuple[str, str, str], int] = {}
        collapsed_indexes: dict[tuple[str, str, str], int] = {}
        for item in items:
            if item.get("source") != "active_watch_date_missing":
                collapsed.append(item)
                continue
            key = (
                str(item.get("source") or ""),
                str(item.get("ticker") or "").upper(),
                str(item.get("event_type") or "").lower(),
            )
            if key in collapsed_indexes:
                duplicate_counts[key] = duplicate_counts.get(key, 1) + 1
                existing = collapsed[collapsed_indexes[key]]
                existing["related_watch_count"] = duplicate_counts[key]
                existing["title"] = cls._merge_watch_titles(
                    existing.get("title"),
                    item.get("title"),
                    duplicate_counts[key],
                )
                continue
            collapsed_indexes[key] = len(collapsed)
            duplicate_counts[key] = 1
            collapsed.append(item)

        dated = [
            item for item in collapsed if item.get("countdown_seconds") is not None
        ]
        undated = [item for item in collapsed if item.get("countdown_seconds") is None]
        first_by_ticker: list[dict[str, Any]] = []
        rest: list[dict[str, Any]] = []
        seen_tickers: set[str] = set()
        for item in undated:
            ticker = str(item.get("ticker") or "").upper()
            if ticker and ticker not in seen_tickers:
                first_by_ticker.append(item)
                seen_tickers.add(ticker)
            else:
                rest.append(item)
        return dated + first_by_ticker + rest

    @staticmethod
    def _merge_watch_titles(existing: Any, incoming: Any, count: int) -> str:
        title = str(existing or incoming or "Active watch")
        suffix = f" (+{count - 1} related active watches)"
        base = re.sub(r"\s\(\+\d+ related active watches\)$", "", title)
        return f"{base}{suffix}"

    @staticmethod
    def _meaningful_text(value: Any) -> str | None:
        text = str(value or "").strip()
        if len(text) < 4:
            return None
        if not any(ch.isalnum() for ch in text):
            return None
        return text

    @classmethod
    def _looks_like_investor_event(cls, *values: Any) -> bool:
        text = " ".join(str(value or "") for value in values).lower()
        if not text.strip():
            return False
        return any(term in text for term in cls.INVESTOR_EVENT_TERMS)

    @classmethod
    def _event_investment_lens(
        cls,
        *,
        ticker: Any,
        event_type: Any,
        title: Any,
        description: Any,
        objective: Any,
        adjustment_plan: Any,
        weight_pct: Any,
    ) -> dict[str, str] | None:
        if not cls._looks_like_investor_event(
            event_type, title, description, objective, adjustment_plan
        ):
            return None
        ticker_text = str(ticker or "the tracked name").upper()
        try:
            weight = float(weight_pct or 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        weight_clause = f" at {weight:.1f}% portfolio weight" if weight else ""
        adjustment = cls._sentence_fragment(adjustment_plan, limit=220)
        description_bit = cls._sentence_fragment(
            description or objective or title, limit=220
        )
        transmission = f"Map the result through {ticker_text}{weight_clause}" + (
            f" and the stored watch plan: {adjustment}." if adjustment else "."
        )
        if description_bit and not adjustment:
            transmission += f" Stored event focus: {description_bit}."
        return {
            "expectation_delta": (
                "Compare pre-event investor expectations, hurdle, consensus, whisper, or guidance bar against what actually happened; "
                "absolute growth is not enough if the setup required overperformance."
            ),
            "market_reaction": (
                "Check price action, volume, analyst estimate revisions, and management guidance changes after the event."
            ),
            "portfolio_transmission": transmission,
            "best_next_check": (
                "Use the official release/transcript plus market reaction and revision data before changing thesis confidence, timing, or sizing."
            ),
        }

    @classmethod
    def _is_undated_calendar_watch(cls, watcher: dict[str, Any]) -> bool:
        condition_type = str(watcher.get("condition_type") or "").lower()
        if condition_type in {"price_above", "price_below", "deadline", "reminder"}:
            return False
        text = " ".join(
            [
                condition_type.replace("_", " "),
                str(watcher.get("objective") or ""),
                str(watcher.get("adjustment_plan") or ""),
            ]
        ).lower()
        return any(term in text for term in cls.CALENDAR_WATCH_TERMS)

    @staticmethod
    def _countdown_sort_value(value: Any) -> int:
        if value is None:
            return 10**9
        try:
            return int(value)
        except (TypeError, ValueError):
            return 10**9

    @classmethod
    def _why_event_matters(
        cls, event: dict[str, Any], *, weight_by_ticker: dict[str, float]
    ) -> str:
        ticker = str(event.get("ticker") or "").upper()
        weight = weight_by_ticker.get(ticker) or float(event.get("weight_pct") or 0.0)
        event_type = str(event.get("event_type") or "event").replace("_", " ")
        event_detail = cls._sentence_fragment(
            event.get("description") or event.get("title")
        )
        detail_clause = f" Stored context: {event_detail}." if event_detail else ""
        if weight:
            return (
                f"{ticker} is {weight:.1f}% of the tracked portfolio; this {event_type} is a dated checkpoint "
                f"for thesis confidence, sizing, or follow-up research priority.{detail_clause}"
            )
        return (
            f"This {event_type} is linked to a tracked name and is a dated checkpoint for thesis confidence, "
            f"sizing, or follow-up research priority.{detail_clause}"
        )

    @classmethod
    def _why_watcher_matters(
        cls, watcher: dict[str, Any], *, weight_by_ticker: dict[str, float]
    ) -> str:
        ticker = str(watcher.get("ticker") or "").upper()
        weight = weight_by_ticker.get(ticker)
        objective = cls._sentence_fragment(watcher.get("objective"))
        focus_clause = (
            f" The stored watch is specifically tracking: {objective}."
            if objective
            else ""
        )
        if weight:
            return (
                f"{ticker} is {weight:.1f}% of the tracked portfolio, so this due watch should trigger a concrete "
                f"thesis or sizing review.{focus_clause}"
            )
        return f"This watch is already active, so the due date should trigger a concrete thesis or sizing review.{focus_clause}"

    @classmethod
    def _why_undated_watch_matters(
        cls, watcher: dict[str, Any], *, weight_by_ticker: dict[str, float]
    ) -> str:
        ticker = str(watcher.get("ticker") or "").upper()
        weight = weight_by_ticker.get(ticker)
        condition_type = str(watcher.get("condition_type") or "catalyst").replace(
            "_", " "
        )
        objective = cls._sentence_fragment(watcher.get("objective"))
        focus_clause = (
            f" The stored watch is specifically tracking: {objective}."
            if objective
            else ""
        )
        return (
            f"{ticker} is {weight:.1f}% of the tracked portfolio; this {condition_type} watch is important, but it has no stored event date, so the calendar needs to be resolved before next-week risk can be trusted.{focus_clause}"
            if weight
            else f"This {condition_type} watch is active, but it has no stored event date, so the calendar needs to be resolved before next-week risk can be trusted.{focus_clause}"
        )

    @classmethod
    def _sentence_fragment(cls, value: Any, *, limit: int = 180) -> str | None:
        text = cls._meaningful_text(value)
        if text is None:
            return None
        text = " ".join(text.split()).strip(" .")
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    async def _run_live_scan(
        self,
        *,
        message: str,
        positions: list[dict[str, Any]],
        now: datetime,
        horizon: datetime,
    ) -> dict[str, Any]:
        query = self._scan_query(positions=positions, now=now, horizon=horizon)
        result = await ResearchService(self.session).run_ad_hoc_request(
            query=query,
            title="Portfolio lookahead catalyst scan",
            metadata_json={
                "trigger": "portfolio_lookahead",
                "requested_via": message[:240],
                "horizon_start": now.date().isoformat(),
                "horizon_end": horizon.date().isoformat(),
                "query": query,
            },
            process_after_ingest=True,
        )
        return {
            "started": result.started,
            "reason": result.reason,
            "evidence_id": str(result.evidence_id) if result.evidence_id else None,
            "processed": result.processed,
            "query": query,
            "title": result.title,
        }

    @staticmethod
    def _scan_query(
        *,
        positions: list[dict[str, Any]],
        now: datetime,
        horizon: datetime,
    ) -> str:
        tickers = [
            str(position.get("ticker") or "").upper()
            for position in positions
            if position.get("list_type") == "holding" and position.get("ticker")
        ][:8]
        if not tickers:
            tickers = [
                str(position.get("ticker") or "").upper()
                for position in positions
                if position.get("ticker")
            ][:8]
        ticker_text = " ".join(tickers) if tickers else "portfolio holdings"
        return (
            f"{ticker_text} upcoming earnings calendar investor events catalysts "
            "investor setup versus actual result expectation delta market reaction revision data portfolio implications "
            f"{now.date().isoformat()} to {horizon.date().isoformat()}"
        )

    async def _resolve_undated_calendar_watches(
        self,
        *,
        watchers: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        now: datetime,
        horizon: datetime,
    ) -> dict[str, Any]:
        candidates = self._calendar_resolution_candidates(
            watchers=watchers, positions=positions
        )
        if not candidates:
            return {
                "attempted": False,
                "updated_count": 0,
                "unresolved_count": 0,
                "items": [],
                "reason": "no_undated_calendar_watches",
            }

        results: list[dict[str, Any]] = []
        updated_watch_ids: list[str] = []
        research = ResearchService(self.session)
        for candidate in candidates[:6]:
            query = self._calendar_search_query(candidate, now=now, horizon=horizon)
            resolution = {
                **candidate,
                "query": query,
                "status": "unresolved",
                "resolved_at": None,
                "source_url": None,
            }
            search_result = await research.search(
                query=query,
                title=f"Calendar lookup: {candidate.get('ticker')} {candidate.get('event_type')}",
                search_depth="basic",
                include_raw_content=False,
                metadata_json={
                    "trigger": "portfolio_lookahead_calendar_resolution",
                    "ticker": candidate.get("ticker"),
                    "event_type": candidate.get("event_type"),
                    "horizon_start": now.date().isoformat(),
                    "horizon_end": horizon.date().isoformat(),
                },
                timeout_seconds=20.0,
            )
            if search_result.reason == "research_provider_not_configured":
                return {
                    "attempted": False,
                    "updated_count": 0,
                    "unresolved_count": len(candidates),
                    "items": candidates,
                    "reason": "research_provider_not_configured",
                }
            if search_result.reason == "rate_limited":
                resolution["status"] = "rate_limited"
                results.append(resolution)
                break
            if search_result.reason != "ok":
                resolution["status"] = search_result.reason
                results.append(resolution)
                continue

            parsed = self._resolved_date_from_search_results(
                search_result.results,
                now=now,
                horizon=horizon,
            )
            if parsed is None:
                results.append(resolution)
                continue

            deadline = parsed["event_time"]
            await self._apply_calendar_deadline(
                watcher_ids=candidate["watcher_ids"],
                deadline=deadline,
                source_url=parsed.get("source_url"),
                query=query,
            )
            updated_watch_ids.extend(candidate["watcher_ids"])
            resolution["status"] = "updated"
            resolution["resolved_at"] = deadline.isoformat()
            resolution["source_url"] = parsed.get("source_url")
            results.append(resolution)

        updated_count = len(set(updated_watch_ids))
        return {
            "attempted": True,
            "updated_count": updated_count,
            "unresolved_count": sum(
                1 for item in results if item.get("status") != "updated"
            ),
            "items": results,
        }

    def _calendar_resolution_candidates(
        self,
        *,
        watchers: list[dict[str, Any]],
        positions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        weight_by_ticker = {
            str(position.get("ticker") or "").upper(): float(
                position.get("weight_pct") or 0.0
            )
            for position in positions
        }
        name_by_ticker = {
            str(position.get("ticker") or "").upper(): str(position.get("name") or "")
            for position in positions
        }
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for watcher in watchers:
            if watcher.get(
                "deadline"
            ) is not None or not self._is_undated_calendar_watch(watcher):
                continue
            ticker = str(watcher.get("ticker") or "").upper()
            if not ticker:
                continue
            event_type = str(watcher.get("condition_type") or "").lower()
            key = (ticker, event_type)
            record = grouped.setdefault(
                key,
                {
                    "ticker": ticker,
                    "name": name_by_ticker.get(ticker) or ticker,
                    "event_type": event_type,
                    "portfolio_weight_pct": weight_by_ticker.get(ticker),
                    "watcher_ids": [],
                    "watch_titles": [],
                },
            )
            record["watcher_ids"].append(str(watcher.get("id")))
            title = self._meaningful_text(watcher.get("objective"))
            if title and title not in record["watch_titles"]:
                record["watch_titles"].append(title)

        return sorted(
            grouped.values(),
            key=lambda item: (
                -float(item.get("portfolio_weight_pct") or 0.0),
                str(item.get("ticker") or ""),
            ),
        )

    @staticmethod
    def _calendar_search_query(
        candidate: dict[str, Any], *, now: datetime, horizon: datetime
    ) -> str:
        ticker = str(candidate.get("ticker") or "").upper()
        name = str(candidate.get("name") or ticker).strip()
        event_type = str(candidate.get("event_type") or "event").replace("_", " ")
        if "earnings" in event_type:
            event_text = "next earnings date investor relations"
        else:
            event_text = f"upcoming {event_type} date investor relations"
        return (
            f"{ticker} {name} {event_text} "
            f"{now.date().isoformat()} to {horizon.date().isoformat()}"
        )

    @classmethod
    def _resolved_date_from_search_results(
        cls,
        results: list[dict[str, Any]],
        *,
        now: datetime,
        horizon: datetime,
    ) -> dict[str, Any] | None:
        for result in results[:5]:
            text = " ".join(
                str(result.get(key) or "")
                for key in ("title", "content", "raw_content")
            )
            event_time = cls._extract_calendar_date(text, now=now, horizon=horizon)
            if event_time is not None:
                return {
                    "event_time": event_time,
                    "source_url": result.get("url"),
                }
        return None

    @classmethod
    def _extract_calendar_date(
        cls, text: str, *, now: datetime, horizon: datetime
    ) -> datetime | None:
        return lookahead_calendar_datetime(text, now=now, horizon=horizon)

    async def _apply_calendar_deadline(
        self,
        *,
        watcher_ids: list[str],
        deadline: datetime,
        source_url: str | None,
        query: str,
    ) -> None:
        parsed_ids = []
        for raw_id in watcher_ids:
            try:
                parsed_ids.append(UUID(str(raw_id)))
            except (TypeError, ValueError):
                continue
        if not parsed_ids:
            return
        watchers = (
            (
                await self.session.execute(
                    select(ActiveWatcher).where(
                        ActiveWatcher.id.in_(parsed_ids),
                        ActiveWatcher.is_active.is_(True),
                        ActiveWatcher.deadline.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not watchers:
            return
        for watcher in watchers:
            watcher.deadline = deadline
            watcher.last_checked_at = datetime.now(UTC)
            metadata = dict(watcher.action_taken_json or {})
            metadata["calendar_resolution"] = {
                "resolved_at": deadline.isoformat(),
                "source_url": source_url,
                "query": query,
            }
            watcher.action_taken_json = metadata
        await self.session.commit()

    @classmethod
    def _seconds_until(cls, value: str, *, now: datetime) -> int | None:
        parsed = cls._parse_time(value)
        if parsed is None:
            return None
        return max(0, int((parsed - now).total_seconds()))

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
