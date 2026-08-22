from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from investos.core.providers import LLM_PROVIDER_CAPABILITIES

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NVIDIA_PROVIDER = LLM_PROVIDER_CAPABILITIES["nvidia_nim"]
OLLAMA_PROVIDER = LLM_PROVIDER_CAPABILITIES["ollama"]


class Settings(BaseSettings):
    PROJECT_NAME: str = "Prophet"
    API_V1_STR: str = "/api"
    FRONTEND_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"
    API_ALLOW_NON_LOOPBACK: bool = False
    ENVIRONMENT: str = "development"

    POSTGRES_USER: str = "investos"
    POSTGRES_PASSWORD: str
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_PORT: str = "5432"
    POSTGRES_DB: str = "investos"

    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    # Storage
    STORAGE_DIR: str = str(PROJECT_ROOT / "data" / "storage")
    MEDIA_TEMP_DIR: str | None = None
    MEDIA_TEMP_RETENTION_HOURS: int = 24
    MEDIA_STORE_RAW_MEDIA: bool = False
    MEDIA_MAX_DOWNLOAD_MB: int = 512
    INGESTION_MAX_UPLOAD_MB: int = 25
    INGESTION_MAX_NOTE_CHARS: int = 2_000_000
    PORTFOLIO_IMPORT_MAX_MB: int = 5
    URL_FETCH_TIMEOUT_SECONDS: float = 20.0
    URL_FETCH_MAX_REDIRECTS: int = 5
    URL_FETCH_MAX_RESPONSE_MB: int = 2
    URL_FETCH_ALLOWED_PORTS: str = "80,443"
    BACKUP_ENABLED: bool = True
    BACKUP_DIR: str = str(PROJECT_ROOT / "backups")
    BACKUP_KEEP_COUNT: int = 7
    BACKUP_MAX_TOTAL_MB: int = 1024

    LLM_PROVIDER: str = "nvidia_nim"
    LLM_TIMEOUT_SECONDS: int = 120
    LLM_STRUCTURED_MAX_TOKENS: int = 12000
    # Global throttle for hosted LLM providers (shared across backfill + all
    # background automation jobs) to stay under provider rate quotas.
    LLM_RATE_LIMIT_PER_MINUTE: int = 30
    LLM_MAX_CONCURRENCY: int = 2
    LLM_ALLOW_LOCAL_PROVIDER: bool = False
    LLM_RECOVERY_PROVIDERS: str = ""
    CORROBORATION_MIN_INDEPENDENT_SOURCES: int = 2
    CORROBORATION_NEAR_DUPLICATE_MAX_DISTANCE: int = 3
    CORROBORATION_LINEAGE_BACKFILL_BATCH_SIZE: int = 100
    INTEGRITY_INVESTMENT_EDGE_REPAIR_BATCH_SIZE: int = 1000
    REASONING_INDEPENDENT_REVIEW_ENABLED: bool = True
    SHADOW_LLM_TIMEOUT_SECONDS: int = 45
    SHADOW_PAPER_PROVIDER: str = "local_simulator"
    SHADOW_PAPER_SLIPPAGE_BPS: float = 5.0
    SHADOW_PAPER_FEE_PER_ORDER: float = 0.0
    SHADOW_PAPER_MAX_BUY_ORDER_PCT_EQUITY: float = 25.0
    SHADOW_PAPER_ALLOW_FRACTIONAL: bool = True
    SHADOW_PAPER_REQUIRE_REGULAR_SESSION: bool = True
    SHADOW_LESSON_MIN_RUNS: int = 3
    SHADOW_LESSON_MATERIAL_ALPHA: float = 0.01
    SHADOW_LESSON_VALIDATION_CONSISTENCY: float = 0.67
    SHADOW_LESSON_STALE_DAYS: int = 180
    SHADOW_LESSON_CONTEXT_LIMIT: int = 6
    SHADOW_LESSON_RECONCILE_BATCH_SIZE: int = 25
    SHADOW_REFRESH_INTERVAL_SECONDS: int = 300
    SHADOW_EVIDENCE_EVENT_BATCH_SIZE: int = 20
    SHADOW_HORIZON_SHORT_DAYS: int = 7
    SHADOW_HORIZON_ADAPTIVE_DAYS: int = 14
    SHADOW_HORIZON_MEDIUM_DAYS: int = 30
    SHADOW_HORIZON_LONG_DAYS: int = 90
    SHADOW_MIN_CHECKPOINT_INTERVAL_SECONDS: int = 21600
    AGENT_DYNAMIC_ANALYSIS_LENS_LIMIT: int = 4
    AGENT_HISTORICAL_ANALOGY_CONTEXT_LIMIT: int = 4
    OLLAMA_BASE_URL: str = OLLAMA_PROVIDER.default_base_url
    OLLAMA_MODEL: str | None = None
    NVIDIA_BASE_URL: str = NVIDIA_PROVIDER.default_base_url
    NVIDIA_MODEL: str = NVIDIA_PROVIDER.default_model
    NVIDIA_API_KEY: str | None = None
    TAVILY_API_KEY: str | None = None
    CODEX_BIN: str = "codex"
    CODEX_MODEL: str | None = None
    CODEX_SANDBOX: str = "read-only"
    CODEX_TIMEOUT_SECONDS: int = 120
    AUTOMATION_ENABLED: bool = True
    AUTOMATION_POLL_SECONDS: int = 300
    AUTOMATION_STARTUP_CATCHUP_DELAY_SECONDS: int = 30
    INVESTMENT_OBJECT_BACKFILL_ENABLED: bool = True
    INVESTMENT_OBJECT_BACKFILL_INTERVAL_SECONDS: int = 43200
    INVESTMENT_OBJECT_BACKFILL_SCAN_LIMIT: int = 300
    INVESTMENT_OBJECT_BACKFILL_BATCH_SIZE: int = 2
    INVESTMENT_OBJECT_BACKFILL_MIN_CONFIDENCE: float = 0.8
    MARKET_SETUP_ASSESSMENT_INTERVAL_SECONDS: int = 21600
    MARKET_SETUP_ASSESSMENT_BATCH_SIZE: int = 5
    MARKET_SETUP_ASSESSMENT_SCAN_LIMIT: int = 500
    MARKET_SETUP_ASSESSMENT_MIN_CONFIDENCE: float = 0.8
    MARKET_SETUP_ASSESSMENT_GRACE_HOURS: int = 6
    MARKET_SETUP_ASSESSMENT_RETRY_HOURS: int = 24
    MARKET_SETUP_ASSESSMENT_RESEARCH_LIMIT: int = 1
    PATTERN_DISCOVERY_ENABLED: bool = True
    PATTERN_DISCOVERY_INTERVAL_SECONDS: int = 21600
    PATTERN_DISCOVERY_LOOKBACK_DAYS: int = 45
    PATTERN_DISCOVERY_MAX_SIGNALS: int = 80
    PATTERN_DISCOVERY_MIN_CONFIDENCE: float = 0.78
    PATTERN_DISCOVERY_DEDUP_DAYS: int = 45
    PATTERN_DISCOVERY_DEDUP_TICKER_CONTAINMENT: float = 0.75
    PATTERN_DISCOVERY_DEDUP_EVIDENCE_CONTAINMENT: float = 0.5
    PATTERN_DISCOVERY_DEDUP_TOKEN_CONTAINMENT: float = 0.35
    PATTERN_DISCOVERY_LLM_TIMEOUT_SECONDS: int = 45
    SOURCE_CLAIM_ASSESSMENT_INTERVAL_SECONDS: int = 21600
    SOURCE_CLAIM_ASSESSMENT_BATCH_SIZE: int = 20
    SOURCE_CLAIM_ASSESSMENT_SCAN_LIMIT: int = 500
    SOURCE_CLAIM_ASSESSMENT_MIN_CONFIDENCE: float = 0.8
    SOURCE_CLAIM_ASSESSMENT_RETRY_HOURS: int = 24
    SOURCE_CLAIM_ASSESSMENT_RETRY_SHARE: float = 0.25
    SOURCE_CLAIM_ASSESSMENT_RESEARCH_LIMIT: int = 1
    RUNTIME_SETTINGS_PATH: str = str(PROJECT_ROOT / "data" / "runtime_settings.json")
    PLAID_CLIENT_ID: str | None = None
    PLAID_SECRET: str | None = None
    PLAID_ENV: str = "sandbox"
    MARKET_DATA_ENABLED: bool = True
    MARKET_DATA_PROVIDER: str = "yahoo_finance"
    MARKET_DATA_REFRESH_SECONDS: int = 60
    # Destructive development tooling is opt-in and remains loopback-only even
    # when enabled. Shared deployments need a real authentication boundary.
    DEV_RESET_ENABLED: bool = False

    @property
    def DEVELOPMENT_RESET_AVAILABLE(self) -> bool:
        return (
            self.DEV_RESET_ENABLED
            and self.ENVIRONMENT.strip().casefold() == "development"
        )

    @property
    def URL_FETCH_ALLOWED_PORT_SET(self) -> frozenset[int]:
        ports: set[int] = set()
        for value in self.URL_FETCH_ALLOWED_PORTS.split(","):
            try:
                port = int(value.strip())
            except ValueError as exc:
                raise ValueError(
                    "URL_FETCH_ALLOWED_PORTS must contain integers."
                ) from exc
            if not 1 <= port <= 65535:
                raise ValueError("URL_FETCH_ALLOWED_PORTS contains an invalid port.")
            ports.add(port)
        if not ports:
            raise ValueError("URL_FETCH_ALLOWED_PORTS must not be empty.")
        return frozenset(ports)

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        case_sensitive=True,
    )


settings = Settings()
