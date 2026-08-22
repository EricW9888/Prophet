from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.providers import llm_provider_capability
from investos.models import Base
from investos.models.coverage import UnresolvedQuestion
from investos.models.evidence import RawEvidence
from investos.models.portfolio import Position
from investos.models.profile import Profile
from investos.schemas.automation import AutomationJobStatus
from investos.schemas.integrations import IntegrationSettingsResponse
from investos.schemas.setup import (
    DevelopmentResetResponse,
    SetupStatusResponse,
    SetupStepResponse,
)
from investos.services.runtime_settings import RuntimeSettingsStore


class SetupService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def status(self, jobs: list[AutomationJobStatus]) -> SetupStatusResponse:
        # Configuration status must not hold a database connection while waiting
        # on provider I/O. Live provider checks are explicit integration probes.
        runtime = await RuntimeSettingsStore.get_public_settings()
        positions_count = await self._count(
            select(Position).where(
                Position.list_type == "holding", Position.quantity > 0
            )
        )
        priced_positions_count = await self._count(
            select(Position).where(
                Position.list_type == "holding",
                Position.quantity > 0,
                Position.weight_pct > 0,
            )
        )
        evidence_count = await self._count(select(RawEvidence))
        profile_count = await self._count(select(Profile))
        open_questions = await self._count(
            select(UnresolvedQuestion).where(UnresolvedQuestion.status == "open")
        )
        steps: list[SetupStepResponse] = []
        steps.append(
            SetupStepResponse(
                id="portfolio_bootstrap",
                label="Load portfolio",
                description="Add transactions manually or import a CSV so Prophet has deterministic holdings.",
                status="complete" if positions_count > 0 else "pending",
                status_label=self._status_label(
                    "complete" if positions_count > 0 else "pending"
                ),
                detail=(
                    f"{positions_count} active holdings"
                    if positions_count > 0
                    else "No active holdings yet."
                ),
                hint=(
                    "Holdings are the anchor for prioritizing research, graph relevance, risk, and automation."
                    if positions_count > 0
                    else "Import broker history or add positions before asking portfolio-level questions."
                ),
                action_label="Review portfolio",
                href="/setup",
            )
        )
        steps.append(self._llm_provider_step(runtime))
        steps.append(self._research_provider_step(runtime))
        gmail_scoped = runtime.gmail.folder.strip().upper() != "INBOX" or bool(
            runtime.gmail.allowed_senders
            or runtime.gmail.allowed_domains
            or runtime.gmail.required_subject_keywords
        )
        gmail_credentials_set = bool(
            runtime.gmail.username and runtime.gmail.password_set
        )
        gmail_status = (
            "complete"
            if runtime.gmail.enabled and gmail_scoped and gmail_credentials_set
            else (
                "in_progress"
                if (
                    gmail_scoped or runtime.gmail.username or runtime.gmail.password_set
                )
                else "pending"
            )
        )
        gmail_missing = []
        if not runtime.gmail.username:
            gmail_missing.append("username")
        if not runtime.gmail.password_set:
            gmail_missing.append("app password")
        if not gmail_scoped:
            gmail_missing.append("safe scope")
        steps.append(
            SetupStepResponse(
                id="gmail_scope",
                label="Configure safe inbox access",
                description="Scope Gmail to a dedicated label, sender allowlist, or subject filter before enabling sync.",
                status=gmail_status,
                status_label=self._status_label(gmail_status),
                detail=(
                    f"Enabled on {runtime.gmail.folder}"
                    if runtime.gmail.enabled and gmail_scoped and gmail_credentials_set
                    else (
                        "Ready to sync."
                        if gmail_scoped and gmail_credentials_set
                        else (
                            f"Missing {', '.join(gmail_missing)}."
                            if gmail_missing
                            else "Not configured."
                        )
                    )
                ),
                hint=(
                    "Safe inbox access keeps ingestion restricted to confirmations and research material you meant Prophet to read."
                    if gmail_status == "complete"
                    else "Use a dedicated label, sender/domain allowlist, or subject keywords before enabling sync."
                ),
                action_label="Configure inbox",
                href="/settings",
            )
        )
        plaid_status = (
            "complete"
            if not runtime.plaid.enabled or runtime.plaid.ready
            else (
                "in_progress"
                if runtime.plaid.client_id_set or runtime.plaid.secret_set
                else "pending"
            )
        )
        steps.append(
            SetupStepResponse(
                id="brokerage_sync",
                label="Connect broker reconciliation",
                description="Optionally compare Prophet's evidence-built ledger with an authoritative brokerage holdings snapshot.",
                status=plaid_status,
                status_label=(
                    "Optional"
                    if not runtime.plaid.enabled
                    else self._status_label(plaid_status)
                ),
                detail=runtime.plaid.status_message,
                hint=(
                    "Optional while disabled. Manual statement reconciliation remains available from Portfolio and History."
                    if not runtime.plaid.enabled
                    else (
                        "Save Plaid credentials, connect an account through Plaid Link, and run the first reconciliation."
                        if not runtime.plaid.ready
                        else "Broker holdings are checked every six hours; discrepancies become review items rather than silent ledger edits."
                    )
                ),
                action_label="Open data connections",
                href="/settings",
            )
        )
        market_data_ok = runtime.market_data.enabled and (
            any(
                job.name == "market_data_refresh" and job.last_status == "ok"
                for job in jobs
            )
            or priced_positions_count > 0
        )
        market_data_status = (
            "complete"
            if market_data_ok
            else ("in_progress" if runtime.market_data.enabled else "pending")
        )
        steps.append(
            SetupStepResponse(
                id="live_prices",
                label="Turn on live prices",
                description="Refresh current prices so holdings reflect live market value and unrealized P&L.",
                status=market_data_status,
                status_label=self._status_label(market_data_status),
                detail=(
                    "Live pricing healthy."
                    if market_data_ok
                    else (
                        "Market data is configured but has not completed successfully yet."
                        if runtime.market_data.enabled
                        else "Market data disabled."
                    )
                ),
                hint="Fresh prices drive position weights, P&L context, concentration alerts, and risk-relative reasoning.",
                action_label="Review market data",
                href="/setup/integrations",
            )
        )
        risk_context_ok = bool(runtime.portfolio.default_benchmark_ticker) and any(
            job.name == "risk_refresh" and job.last_status == "ok" for job in jobs
        )
        risk_status = (
            "complete"
            if risk_context_ok
            else (
                "in_progress"
                if runtime.portfolio.default_benchmark_ticker
                else "pending"
            )
        )
        steps.append(
            SetupStepResponse(
                id="benchmark_context",
                label="Set benchmark context",
                description="Pick a default benchmark and compute portfolio-relative risk context.",
                status=risk_status,
                status_label=self._status_label(risk_status),
                detail=(
                    f"Benchmark {runtime.portfolio.default_benchmark_ticker} is active."
                    if risk_context_ok
                    else (
                        f"Benchmark {runtime.portfolio.default_benchmark_ticker} selected, waiting for risk refresh."
                        if runtime.portfolio.default_benchmark_ticker
                        else "No default benchmark selected."
                    )
                ),
                hint="Benchmark context lets Prophet separate idiosyncratic losses from broader market or sector moves.",
                action_label="Open risk view",
                href="/risk",
            )
        )
        research_memory_status = "complete" if evidence_count > 0 else "pending"
        steps.append(
            SetupStepResponse(
                id="research_memory",
                label="Seed research memory",
                description="Ingest notes, confirmations, or other evidence so profiles and reasoning can build state.",
                status=research_memory_status,
                status_label=self._status_label(research_memory_status),
                detail=(
                    f"{evidence_count} evidence records, {profile_count} profiles"
                    if evidence_count > 0
                    else "No evidence ingested yet."
                ),
                hint=(
                    "Stored evidence is the durable memory Prophet uses for theses, graph links, and follow-up checks."
                    if evidence_count > 0
                    else "Add trusted sources, notes, transcripts, filings, or imported research so the system has durable context."
                ),
                action_label="Open sources",
                href="/settings",
            )
        )
        research_loop_status = (
            "complete" if open_questions > 0 or evidence_count > 0 else "pending"
        )
        steps.append(
            SetupStepResponse(
                id="open_questions",
                label="Start autonomous research loop",
                description="Use coverage gaps and unresolved questions to drive ongoing research.",
                status=research_loop_status,
                status_label=self._status_label(research_loop_status),
                detail=(
                    f"{open_questions} open research questions"
                    if open_questions > 0
                    else "Research loop will activate as evidence accumulates."
                ),
                hint=(
                    "Open questions are the work queue for follow-up research and evidence promotion."
                    if open_questions > 0
                    else "Once there is evidence or a portfolio question, Prophet can turn gaps into research work."
                ),
                action_label="Open chat",
                href="/chat",
            )
        )

        completed = sum(1 for step in steps if step.status == "complete")
        ratio = completed / len(steps) if steps else 0.0
        overall = (
            "complete" if ratio == 1 else "in_progress" if ratio > 0 else "not_started"
        )
        next_step = next(
            (step.label for step in steps if step.status != "complete"), None
        )
        return SetupStatusResponse(
            status=overall,
            completion_ratio=ratio,
            next_recommended_step=next_step,
            development_reset_enabled=settings.DEVELOPMENT_RESET_AVAILABLE,
            steps=steps,
        )

    async def reset_development_state(self) -> DevelopmentResetResponse:
        preserved_tables = ["market_calendar"]
        cleared_tables = [
            table.name
            for table in Base.metadata.sorted_tables
            if table.name not in preserved_tables
        ]
        truncate_targets = ", ".join(f'"{table_name}"' for table_name in cleared_tables)
        if truncate_targets:
            await self.session.execute(
                text(f"TRUNCATE TABLE {truncate_targets} RESTART IDENTITY CASCADE")
            )
            await self.session.commit()

        RuntimeSettingsStore.reset()

        storage_root = Path(settings.STORAGE_DIR)
        storage_cleared = False
        if storage_root.exists():
            shutil.rmtree(storage_root, ignore_errors=True)
            storage_cleared = True
        storage_root.mkdir(parents=True, exist_ok=True)

        return DevelopmentResetResponse(
            ok=True,
            detail="Development state reset. User data cleared, runtime settings restored to defaults.",
            reset_at=datetime.now(UTC),
            cleared_tables=cleared_tables,
            preserved_tables=preserved_tables,
            storage_cleared=storage_cleared,
            runtime_settings_reset=True,
        )

    async def _count(self, stmt) -> int:
        count_stmt = select(func.count()).select_from(stmt.subquery())
        return int((await self.session.execute(count_stmt)).scalar_one() or 0)

    @staticmethod
    def _status_label(status: str) -> str:
        return {
            "complete": "Complete",
            "in_progress": "In progress",
            "pending": "Needs setup",
            "not_started": "Not started",
        }.get(status, status.replace("_", " ").title())

    @classmethod
    def _llm_provider_step(
        cls, runtime: IntegrationSettingsResponse
    ) -> SetupStepResponse:
        llm = runtime.llm
        capability = next(
            (
                item
                for item in getattr(llm, "available_providers", [])
                if item.provider == llm.provider
            ),
            llm_provider_capability(llm.provider),
        )
        provider_label = (
            capability.label if capability else cls._provider_label(llm.provider)
        )
        if llm.ready:
            status = "complete"
            detail = llm.status_message or f"{provider_label} is ready."
            hint = "This provider powers structured extraction, analysis passes, and agent routing."
        elif capability and capability.requires_api_key and not llm.api_key_set:
            status = "pending"
            detail = f"{provider_label} API key is missing."
            hint = "Paste a hosted LLM API key in Research settings so analyst and extraction passes can run."
        elif "cooling down" in (llm.status_message or "").casefold():
            status = "complete"
            detail = (
                llm.status_message
                or f"{provider_label} is configured and temporarily cooling down."
            )
            hint = "Configuration is complete. Prophet will resume hosted calls automatically after the provider cooldown."
        elif capability and capability.is_local:
            status = "in_progress"
            detail = (
                llm.status_message or f"Local provider {provider_label} is selected."
            )
            hint = "Prophet will not start local providers automatically; switch to a hosted provider or run the selected local service yourself."
        else:
            status = (
                "in_progress"
                if llm.api_key_set or not (capability and capability.requires_api_key)
                else "pending"
            )
            detail = (
                llm.status_message
                or f"{provider_label} readiness has not been confirmed."
            )
            hint = "Fix the provider configuration until the readiness check succeeds."
        return SetupStepResponse(
            id="llm_provider",
            label="Configure intelligence provider",
            description="Connect the LLM provider used for extraction, reasoning, and agent tools.",
            status=status,
            status_label=cls._status_label(status),
            detail=detail,
            hint=hint,
            action_label="Open research settings",
            href="/settings",
        )

    @classmethod
    def _research_provider_step(
        cls, runtime: IntegrationSettingsResponse
    ) -> SetupStepResponse:
        research = runtime.research
        provider_label = cls._research_provider_label(research.provider)
        if research.ready:
            status = "complete"
            detail = research.status_message or f"{provider_label} is ready."
            hint = "External research can fetch current sources; stored local evidence still remains the durable record."
        elif research.provider == "tavily" and not research.api_key_set:
            status = "pending"
            detail = "Tavily API key is missing."
            hint = "Paste a Tavily key in Research settings so fresh search/current-event checks can run."
        else:
            status = "in_progress" if research.api_key_set else "pending"
            detail = (
                research.status_message
                or f"{provider_label} readiness has not been confirmed."
            )
            hint = "Resolve the provider error or rate-limit state before relying on live web research."
        return SetupStepResponse(
            id="research_provider",
            label="Configure external research API",
            description="Connect the web/search provider used for fresh source discovery.",
            status=status,
            status_label=cls._status_label(status),
            detail=detail,
            hint=hint,
            action_label="Open research settings",
            href="/settings",
        )

    @staticmethod
    def _provider_label(provider: str) -> str:
        return provider.replace("_", " ").title()

    @staticmethod
    def _research_provider_label(provider: str) -> str:
        return provider.replace("_", " ").title()
