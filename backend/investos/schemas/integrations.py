from typing import Any

from pydantic import BaseModel, Field


class MarketDataIntegrationSettings(BaseModel):
    enabled: bool = True
    provider: str = "yahoo_finance"
    refresh_interval_seconds: int = 60
    ready: bool = True
    status_message: str | None = None


class GmailIntegrationSettings(BaseModel):
    enabled: bool = False
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    username: str = ""
    folder: str = "INBOX"
    only_unseen: bool = True
    fetch_limit: int = 50
    allowed_senders: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    required_subject_keywords: list[str] = Field(default_factory=list)
    password_set: bool = False
    ready: bool = False
    status_message: str | None = None


class GmailIntegrationSettingsUpdate(BaseModel):
    enabled: bool | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    username: str | None = None
    password: str | None = None
    folder: str | None = None
    only_unseen: bool | None = None
    fetch_limit: int | None = None
    allowed_senders: list[str] | None = None
    allowed_domains: list[str] | None = None
    required_subject_keywords: list[str] | None = None


class GmailIntegrationTestRequest(GmailIntegrationSettings):
    password: str | None = None


class GmailIntegrationTestResponse(BaseModel):
    ok: bool
    detail: str
    matched_messages: int = 0
    scanned_messages: int = 0
    sample_subjects: list[str] = Field(default_factory=list)


class MarketDataIntegrationSettingsUpdate(BaseModel):
    enabled: bool | None = None
    provider: str | None = None
    refresh_interval_seconds: int | None = None


class PaperTradingIntegrationSettings(BaseModel):
    enabled: bool = True
    provider: str = "local_simulator"
    slippage_bps: float = 5.0
    fee_per_order: float = 0.0
    max_buy_order_pct_equity: float = 25.0
    allow_fractional: bool = True
    require_regular_session: bool = True
    ready: bool = True
    status_message: str | None = None


class PaperTradingIntegrationSettingsUpdate(BaseModel):
    enabled: bool | None = None
    provider: str | None = None
    slippage_bps: float | None = None
    fee_per_order: float | None = None
    max_buy_order_pct_equity: float | None = None
    allow_fractional: bool | None = None
    require_regular_session: bool | None = None


class PlaidIntegrationSettings(BaseModel):
    enabled: bool = False
    environment: str = "sandbox"
    client_id_set: bool = False
    secret_set: bool = False
    access_token_set: bool = False
    item_id: str | None = None
    ready: bool = False
    status_message: str | None = None


class PlaidIntegrationSettingsUpdate(BaseModel):
    enabled: bool | None = None
    environment: str | None = None
    client_id: str | None = None
    secret: str | None = None
    access_token: str | None = None


class PlaidPublicTokenExchangeRequest(BaseModel):
    public_token: str


class LLMProviderCapabilityResponse(BaseModel):
    provider: str
    label: str
    is_local: bool
    requires_api_key: bool
    accepts_model: bool
    accepts_base_url: bool
    supports_streaming: bool
    default_model: str = ""
    default_base_url: str = ""


class LLMIntegrationSettings(BaseModel):
    provider: str = "nvidia_nim"
    hosted_base_url: str = "https://integrate.api.nvidia.com/v1"
    hosted_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    available_providers: list[LLMProviderCapabilityResponse] = Field(
        default_factory=list
    )
    api_key_set: bool = False
    ready: bool = False
    status_message: str | None = None


class LLMIntegrationSettingsUpdate(BaseModel):
    provider: str | None = None
    hosted_base_url: str | None = None
    hosted_model: str | None = None
    api_key: str | None = None


class ResearchIntegrationSettings(BaseModel):
    provider: str = "tavily"
    api_key_set: bool = False
    ready: bool = False
    status_message: str | None = None


class ResearchUsageRequestEntry(BaseModel):
    timestamp: str
    provider: str | None = None
    query: str | None = None
    title: str | None = None
    search_depth: str | None = None
    status: str
    result_count: int | None = None
    top_url: str | None = None
    request_id: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence_id: str | None = None
    processed: bool | None = None


class ResearchUsageSnapshot(BaseModel):
    provider: str
    ready: bool
    status_message: str
    key: str | dict[str, Any] | None = None
    account: dict[str, Any] | None = None
    recent_requests: list[ResearchUsageRequestEntry] = Field(default_factory=list)


class ResearchIntegrationSettingsUpdate(BaseModel):
    provider: str | None = None
    api_key: str | None = None


class PortfolioIntegrationSettings(BaseModel):
    default_benchmark_ticker: str = "SPY"
    remaining_buying_power: float = 0.0


class PortfolioIntegrationSettingsUpdate(BaseModel):
    default_benchmark_ticker: str | None = None
    remaining_buying_power: float | None = None


class IntegrationSettingsResponse(BaseModel):
    market_data: MarketDataIntegrationSettings
    paper_trading: PaperTradingIntegrationSettings
    gmail: GmailIntegrationSettings
    plaid: PlaidIntegrationSettings
    llm: LLMIntegrationSettings
    research: ResearchIntegrationSettings
    portfolio: PortfolioIntegrationSettings


class IntegrationSettingsUpdate(BaseModel):
    market_data: MarketDataIntegrationSettingsUpdate | None = None
    paper_trading: PaperTradingIntegrationSettingsUpdate | None = None
    gmail: GmailIntegrationSettingsUpdate | None = None
    plaid: PlaidIntegrationSettingsUpdate | None = None
    llm: LLMIntegrationSettingsUpdate | None = None
    research: ResearchIntegrationSettingsUpdate | None = None
    portfolio: PortfolioIntegrationSettingsUpdate | None = None
