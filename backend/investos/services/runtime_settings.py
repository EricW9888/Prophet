from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from investos.config import settings
from investos.core.providers import (
    LLM_PROVIDER_CAPABILITIES,
    llm_provider_capability,
    selectable_llm_providers,
)
from investos.schemas.integrations import (
    GmailIntegrationSettings,
    GmailIntegrationSettingsUpdate,
    IntegrationSettingsResponse,
    IntegrationSettingsUpdate,
    LLMIntegrationSettings,
    LLMIntegrationSettingsUpdate,
    MarketDataIntegrationSettings,
    MarketDataIntegrationSettingsUpdate,
    PaperTradingIntegrationSettings,
    PaperTradingIntegrationSettingsUpdate,
    PlaidIntegrationSettings,
    PlaidIntegrationSettingsUpdate,
    PortfolioIntegrationSettings,
    PortfolioIntegrationSettingsUpdate,
    ResearchIntegrationSettings,
    ResearchIntegrationSettingsUpdate,
)


class MarketDataRuntimeSettings(BaseModel):
    enabled: bool = settings.MARKET_DATA_ENABLED
    provider: str = settings.MARKET_DATA_PROVIDER
    refresh_interval_seconds: int = settings.MARKET_DATA_REFRESH_SECONDS


class PaperTradingRuntimeSettings(BaseModel):
    enabled: bool = True
    provider: str = settings.SHADOW_PAPER_PROVIDER
    slippage_bps: float = settings.SHADOW_PAPER_SLIPPAGE_BPS
    fee_per_order: float = settings.SHADOW_PAPER_FEE_PER_ORDER
    max_buy_order_pct_equity: float = settings.SHADOW_PAPER_MAX_BUY_ORDER_PCT_EQUITY
    allow_fractional: bool = settings.SHADOW_PAPER_ALLOW_FRACTIONAL
    require_regular_session: bool = settings.SHADOW_PAPER_REQUIRE_REGULAR_SESSION


class GmailRuntimeSettings(GmailIntegrationSettings):
    password: str | None = None


class LLMRuntimeSettings(BaseModel):
    provider: str = settings.LLM_PROVIDER
    hosted_base_url: str = settings.NVIDIA_BASE_URL
    hosted_model: str = settings.NVIDIA_MODEL
    api_key: str | None = settings.NVIDIA_API_KEY


class ResearchRuntimeSettings(BaseModel):
    provider: str = "tavily"
    api_key: str | None = settings.TAVILY_API_KEY


class PlaidRuntimeSettings(BaseModel):
    enabled: bool = False
    environment: str = settings.PLAID_ENV
    client_id: str | None = settings.PLAID_CLIENT_ID
    secret: str | None = settings.PLAID_SECRET
    access_token: str | None = None
    item_id: str | None = None


class RuntimeSettings(BaseModel):
    market_data: MarketDataRuntimeSettings = Field(
        default_factory=MarketDataRuntimeSettings
    )
    paper_trading: PaperTradingRuntimeSettings = Field(
        default_factory=PaperTradingRuntimeSettings
    )
    gmail: GmailRuntimeSettings = Field(default_factory=GmailRuntimeSettings)
    plaid: PlaidRuntimeSettings = Field(default_factory=PlaidRuntimeSettings)
    llm: LLMRuntimeSettings = Field(default_factory=LLMRuntimeSettings)
    research: ResearchRuntimeSettings = Field(default_factory=ResearchRuntimeSettings)
    portfolio: PortfolioIntegrationSettings = Field(
        default_factory=PortfolioIntegrationSettings
    )


class RuntimeSecrets(BaseModel):
    llm_api_key: str | None = settings.NVIDIA_API_KEY
    research_api_key: str | None = settings.TAVILY_API_KEY
    gmail_password: str | None = None
    plaid_client_id: str | None = settings.PLAID_CLIENT_ID
    plaid_secret: str | None = settings.PLAID_SECRET
    plaid_access_token: str | None = None


class RuntimeSettingsStore:
    @staticmethod
    def _write_private_json(path: Path, payload: BaseModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            descriptor, temp_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{path.name}.",
            )
            temp_path = Path(temp_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                try:
                    os.fchmod(handle.fileno(), 0o600)
                except (AttributeError, OSError):
                    # Windows relies on the inherited ACL for this private file.
                    pass
                json.dump(
                    payload.model_dump(mode="json"),
                    handle,
                    indent=2,
                    ensure_ascii=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    @classmethod
    def load(cls) -> RuntimeSettings:
        path = Path(settings.RUNTIME_SETTINGS_PATH)
        if not path.exists():
            runtime = RuntimeSettings()
            cls.save(runtime)
            return runtime
        raw = json.loads(path.read_text(encoding="utf-8"))
        runtime = RuntimeSettings.model_validate(raw)
        secrets = cls._load_secrets()
        runtime.llm.api_key = secrets.llm_api_key
        runtime.research.api_key = secrets.research_api_key
        runtime.gmail.password = secrets.gmail_password
        runtime.gmail.password_set = bool(secrets.gmail_password)
        runtime.plaid.client_id = secrets.plaid_client_id
        runtime.plaid.secret = secrets.plaid_secret
        runtime.plaid.access_token = secrets.plaid_access_token
        if cls._needs_secret_migration(raw):
            cls.save(runtime)
        return runtime

    @classmethod
    def save(cls, runtime: RuntimeSettings) -> None:
        path = Path(settings.RUNTIME_SETTINGS_PATH)
        runtime_public = runtime.model_copy(deep=True)
        runtime_public.llm.api_key = None
        runtime_public.research.api_key = None
        runtime_public.gmail.password = None
        runtime_public.plaid.client_id = None
        runtime_public.plaid.secret = None
        runtime_public.plaid.access_token = None
        cls._write_private_json(path, runtime_public)
        cls._save_secrets(
            RuntimeSecrets(
                llm_api_key=runtime.llm.api_key,
                research_api_key=runtime.research.api_key,
                gmail_password=runtime.gmail.password,
                plaid_client_id=runtime.plaid.client_id,
                plaid_secret=runtime.plaid.secret,
                plaid_access_token=runtime.plaid.access_token,
            )
        )

    @classmethod
    async def reset(cls) -> IntegrationSettingsResponse:
        runtime = RuntimeSettings()
        cls.save(runtime)
        return await cls.get_public_settings()

    @classmethod
    async def get_public_settings(
        cls, *, probe: bool = False
    ) -> IntegrationSettingsResponse:
        runtime = cls.load()
        gmail_scoped = bool(
            runtime.gmail.folder.strip().upper() != "INBOX"
            or runtime.gmail.allowed_senders
            or runtime.gmail.allowed_domains
            or runtime.gmail.required_subject_keywords
        )
        gmail_ready = bool(
            runtime.gmail.enabled
            and runtime.gmail.username
            and runtime.gmail.password
            and gmail_scoped
        )
        plaid_credentials = bool(runtime.plaid.client_id and runtime.plaid.secret)
        plaid_ready = bool(
            runtime.plaid.enabled and plaid_credentials and runtime.plaid.access_token
        )

        llm_ready, llm_msg, research_ready, research_msg = (
            cls._configured_provider_status(runtime)
        )
        if probe:
            from investos.core.llm import verify_llm_readiness
            from investos.services.research import ResearchService

            llm_task = asyncio.create_task(verify_llm_readiness())
            research_task = asyncio.create_task(
                ResearchService.verify_research_readiness()
            )
            try:
                llm_ready, llm_msg = await asyncio.wait_for(llm_task, timeout=7.0)
            except Exception as exc:
                llm_ready, llm_msg = False, f"LLM check timed out: {exc}"

            try:
                research_ready, research_msg = await asyncio.wait_for(
                    research_task, timeout=7.0
                )
            except Exception as exc:
                research_ready, research_msg = (
                    False,
                    f"Research check timed out: {exc}",
                )

        return IntegrationSettingsResponse(
            market_data=MarketDataIntegrationSettings(
                enabled=runtime.market_data.enabled,
                provider=runtime.market_data.provider,
                refresh_interval_seconds=runtime.market_data.refresh_interval_seconds,
                ready=True,
                status_message="Active",
            ),
            paper_trading=PaperTradingIntegrationSettings(
                enabled=runtime.paper_trading.enabled,
                provider=runtime.paper_trading.provider,
                slippage_bps=runtime.paper_trading.slippage_bps,
                fee_per_order=runtime.paper_trading.fee_per_order,
                max_buy_order_pct_equity=runtime.paper_trading.max_buy_order_pct_equity,
                allow_fractional=runtime.paper_trading.allow_fractional,
                require_regular_session=runtime.paper_trading.require_regular_session,
                ready=runtime.paper_trading.enabled
                and runtime.paper_trading.provider == "local_simulator",
                status_message=(
                    "Deterministic local paper broker is active; it cannot route real orders."
                    if runtime.paper_trading.enabled
                    else "Paper execution is disabled; Shadow remains analysis-only."
                ),
            ),
            gmail=GmailIntegrationSettings(
                enabled=runtime.gmail.enabled,
                imap_host=runtime.gmail.imap_host,
                imap_port=runtime.gmail.imap_port,
                username=runtime.gmail.username,
                folder=runtime.gmail.folder,
                only_unseen=runtime.gmail.only_unseen,
                fetch_limit=runtime.gmail.fetch_limit,
                allowed_senders=runtime.gmail.allowed_senders,
                allowed_domains=runtime.gmail.allowed_domains,
                required_subject_keywords=runtime.gmail.required_subject_keywords,
                password_set=bool(runtime.gmail.password),
                ready=gmail_ready,
                status_message=(
                    "Ready for scoped broker-confirmation sync."
                    if gmail_ready
                    else (
                        "Disabled."
                        if not runtime.gmail.enabled
                        else "Credentials or a safe mailbox scope are incomplete."
                    )
                ),
            ),
            plaid=PlaidIntegrationSettings(
                enabled=runtime.plaid.enabled,
                environment=runtime.plaid.environment,
                client_id_set=bool(runtime.plaid.client_id),
                secret_set=bool(runtime.plaid.secret),
                access_token_set=bool(runtime.plaid.access_token),
                item_id=runtime.plaid.item_id,
                ready=plaid_ready,
                status_message=(
                    "Connected; automatic holdings reconciliation is available."
                    if plaid_ready
                    else (
                        "Credentials saved; connect a brokerage account with Plaid Link."
                        if runtime.plaid.enabled and plaid_credentials
                        else (
                            "Broker sync is disabled."
                            if not runtime.plaid.enabled
                            else "Plaid client ID and secret are required."
                        )
                    )
                ),
            ),
            llm=LLMIntegrationSettings(
                provider=runtime.llm.provider,
                hosted_base_url=runtime.llm.hosted_base_url,
                hosted_model=runtime.llm.hosted_model,
                available_providers=[
                    capability.public_dict()
                    for capability in selectable_llm_providers(
                        allow_local=settings.LLM_ALLOW_LOCAL_PROVIDER,
                    )
                ],
                api_key_set=bool(runtime.llm.api_key),
                ready=llm_ready,
                status_message=llm_msg,
            ),
            research=ResearchIntegrationSettings(
                provider=runtime.research.provider,
                api_key_set=bool(runtime.research.api_key),
                ready=research_ready,
                status_message=research_msg,
            ),
            portfolio=PortfolioIntegrationSettings(
                default_benchmark_ticker=runtime.portfolio.default_benchmark_ticker,
                remaining_buying_power=runtime.portfolio.remaining_buying_power,
            ),
        )

    @staticmethod
    def _configured_provider_status(
        runtime: RuntimeSettings,
    ) -> tuple[bool, str, bool, str]:
        capability = llm_provider_capability(runtime.llm.provider)
        if capability is None:
            llm_ready = False
            llm_msg = f"Unknown LLM provider: {runtime.llm.provider}."
        elif capability.is_local and not settings.LLM_ALLOW_LOCAL_PROVIDER:
            llm_ready = False
            llm_msg = "Local LLM providers are disabled by policy."
        elif capability.requires_api_key and not runtime.llm.api_key:
            llm_ready = False
            llm_msg = "LLM API key is missing."
        else:
            llm_ready = True
            llm_msg = (
                "Configured. Live availability is checked when Prophet calls "
                "the provider."
            )

        if runtime.research.provider != "tavily":
            research_ready = False
            research_msg = f"Unknown research provider: {runtime.research.provider}."
        elif not runtime.research.api_key:
            research_ready = False
            research_msg = "Research API key is missing."
        else:
            research_ready = True
            research_msg = (
                "Configured. Live availability is checked when Prophet runs "
                "external research."
            )
        return llm_ready, llm_msg, research_ready, research_msg

    @classmethod
    async def update(
        cls, payload: IntegrationSettingsUpdate
    ) -> IntegrationSettingsResponse:
        runtime = cls.load()
        if payload.market_data is not None:
            runtime.market_data = cls._update_market_data(
                runtime.market_data, payload.market_data
            )
        if payload.paper_trading is not None:
            runtime.paper_trading = cls._update_paper_trading(
                runtime.paper_trading, payload.paper_trading
            )
        if payload.gmail is not None:
            runtime.gmail = cls._update_gmail(runtime.gmail, payload.gmail)
        if payload.plaid is not None:
            runtime.plaid = cls._update_plaid(runtime.plaid, payload.plaid)
        if payload.llm is not None:
            runtime.llm = cls._update_llm(runtime.llm, payload.llm)
        if payload.research is not None:
            runtime.research = cls._update_research(runtime.research, payload.research)
        if payload.portfolio is not None:
            runtime.portfolio = cls._update_portfolio(
                runtime.portfolio, payload.portfolio
            )
        cls._validate(runtime)
        cls.save(runtime)
        return await cls.get_public_settings()

    @staticmethod
    def _update_market_data(
        current: MarketDataRuntimeSettings,
        update: MarketDataIntegrationSettingsUpdate,
    ) -> MarketDataRuntimeSettings:
        data = current.model_dump()
        for field in ("enabled", "provider", "refresh_interval_seconds"):
            value = getattr(update, field)
            if value is not None:
                data[field] = value.strip() if isinstance(value, str) else value
        return MarketDataRuntimeSettings.model_validate(data)

    @staticmethod
    def _update_paper_trading(
        current: PaperTradingRuntimeSettings,
        update: PaperTradingIntegrationSettingsUpdate,
    ) -> PaperTradingRuntimeSettings:
        data = current.model_dump()
        for field in (
            "enabled",
            "provider",
            "slippage_bps",
            "fee_per_order",
            "max_buy_order_pct_equity",
            "allow_fractional",
            "require_regular_session",
        ):
            value = getattr(update, field)
            if value is not None:
                data[field] = value.strip() if isinstance(value, str) else value
        return PaperTradingRuntimeSettings.model_validate(data)

    @staticmethod
    def _update_gmail(
        current: GmailRuntimeSettings,
        update: GmailIntegrationSettingsUpdate,
    ) -> GmailRuntimeSettings:
        data = current.model_dump()
        for field in (
            "enabled",
            "imap_host",
            "imap_port",
            "username",
            "password",
            "folder",
            "only_unseen",
            "fetch_limit",
            "allowed_senders",
            "allowed_domains",
            "required_subject_keywords",
        ):
            value = getattr(update, field)
            if value is not None:
                data[field] = value.strip() if isinstance(value, str) else value

        if update.password is not None:
            data["password"] = update.password.strip()
            data["password_set"] = True

        return GmailRuntimeSettings.model_validate(data)

    @staticmethod
    def _update_llm(
        current: LLMRuntimeSettings,
        update: LLMIntegrationSettingsUpdate,
    ) -> LLMRuntimeSettings:
        data = current.model_dump()
        if update.provider is not None:
            data["provider"] = update.provider.strip().lower()
        if update.hosted_base_url is not None:
            data["hosted_base_url"] = update.hosted_base_url.strip()
        if update.hosted_model is not None:
            data["hosted_model"] = update.hosted_model.strip()
        if update.api_key is not None:
            data["api_key"] = update.api_key.strip() or current.api_key
        return LLMRuntimeSettings.model_validate(data)

    @staticmethod
    def _update_plaid(
        current: PlaidRuntimeSettings,
        update: PlaidIntegrationSettingsUpdate,
    ) -> PlaidRuntimeSettings:
        data = current.model_dump()
        for field in ("enabled", "environment", "client_id", "secret", "access_token"):
            value = getattr(update, field)
            if value is None:
                continue
            if isinstance(value, str):
                value = value.strip()
                if field in {"client_id", "secret", "access_token"} and not value:
                    value = data.get(field)
            data[field] = value
        return PlaidRuntimeSettings.model_validate(data)

    @staticmethod
    def _update_research(
        current: ResearchRuntimeSettings,
        update: ResearchIntegrationSettingsUpdate,
    ) -> ResearchRuntimeSettings:
        data = current.model_dump()
        if update.provider is not None:
            data["provider"] = update.provider.strip().lower()
        if update.api_key is not None:
            data["api_key"] = update.api_key.strip() or current.api_key
        return ResearchRuntimeSettings.model_validate(data)

    @staticmethod
    def _update_portfolio(
        current: PortfolioIntegrationSettings,
        update: PortfolioIntegrationSettingsUpdate,
    ) -> PortfolioIntegrationSettings:
        data = current.model_dump()
        if update.default_benchmark_ticker is not None:
            data["default_benchmark_ticker"] = (
                update.default_benchmark_ticker.strip().upper()
            )
        if update.remaining_buying_power is not None:
            data["remaining_buying_power"] = float(update.remaining_buying_power)
        return PortfolioIntegrationSettings.model_validate(data)

    @staticmethod
    def _validate(runtime: RuntimeSettings) -> None:
        market_data = runtime.market_data
        if market_data.refresh_interval_seconds <= 0:
            raise ValueError("Market data refresh interval must be positive.")
        paper = runtime.paper_trading
        if paper.provider != "local_simulator":
            raise ValueError("Paper trading provider must be 'local_simulator'.")
        if paper.slippage_bps < 0 or paper.slippage_bps > 1000:
            raise ValueError("Paper slippage must be between 0 and 1000 basis points.")
        if paper.fee_per_order < 0:
            raise ValueError("Paper order fee cannot be negative.")
        if paper.max_buy_order_pct_equity <= 0 or paper.max_buy_order_pct_equity > 100:
            raise ValueError(
                "Maximum paper buy order size must be between 0 and 100 percent of equity."
            )
        capability = llm_provider_capability(runtime.llm.provider)
        if capability is None:
            supported = ", ".join(LLM_PROVIDER_CAPABILITIES)
            raise ValueError(f"Unknown LLM provider. Supported providers: {supported}.")
        if capability.is_local and not settings.LLM_ALLOW_LOCAL_PROVIDER:
            raise ValueError("Local LLM providers are disabled by policy.")
        if capability.accepts_base_url and not runtime.llm.hosted_base_url.strip():
            raise ValueError(f"A base URL is required for {capability.label}.")
        if capability.accepts_model and not runtime.llm.hosted_model.strip():
            raise ValueError(f"A model identifier is required for {capability.label}.")
        if runtime.research.provider not in {"tavily"}:
            raise ValueError("Research provider must be 'tavily'.")
        if runtime.plaid.environment not in {"sandbox", "development", "production"}:
            raise ValueError(
                "Plaid environment must be sandbox, development, or production."
            )
        if not runtime.portfolio.default_benchmark_ticker.strip():
            raise ValueError("A default benchmark ticker is required.")
        if runtime.portfolio.remaining_buying_power < 0:
            raise ValueError("Remaining buying power cannot be negative.")

    @staticmethod
    def _secrets_path() -> Path:
        runtime_path = Path(settings.RUNTIME_SETTINGS_PATH)
        return runtime_path.with_suffix(f"{runtime_path.suffix}.secrets")

    @classmethod
    def _load_secrets(cls) -> RuntimeSecrets:
        path = cls._secrets_path()
        if not path.exists():
            secrets = RuntimeSecrets()
            cls._save_secrets(secrets)
            return secrets
        raw = json.loads(path.read_text(encoding="utf-8"))
        return RuntimeSecrets.model_validate(raw)

    @classmethod
    def _save_secrets(cls, secrets: RuntimeSecrets) -> None:
        path = cls._secrets_path()
        cls._write_private_json(path, secrets)

    @staticmethod
    def _needs_secret_migration(raw: dict) -> bool:
        llm = raw.get("llm") or {}
        research = raw.get("research") or {}
        plaid = raw.get("plaid") or {}
        return bool(
            llm.get("api_key")
            or research.get("api_key")
            or plaid.get("client_id")
            or plaid.get("secret")
            or plaid.get("access_token")
        )
