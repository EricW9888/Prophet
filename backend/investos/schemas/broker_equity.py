from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class BrokerStatementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: AwareDatetime
    equity: Decimal = Field(max_digits=24, decimal_places=2)
    cash: Decimal | None = Field(default=None, max_digits=24, decimal_places=2)
    holdings_text: str = Field(max_length=262_144)
    cash_only: bool = False
    currency: Literal["USD"] = "USD"

    @model_validator(mode="after")
    def require_observed_time(self):
        if self.as_of > datetime.now(UTC):
            raise ValueError("The statement time must have already occurred")
        self.as_of = self.as_of.astimezone(UTC)
        return self


class BrokerStatementSave(BaseModel):
    statement: BrokerStatementInput
    preview_token: str = Field(pattern=r"^[0-9a-f]{64}$")


class BrokerEquityPoint(BaseModel):
    id: UUID
    as_of: datetime
    value: Decimal | None
    reasons: list[str]


class BrokerLedgerMarker(BaseModel):
    id: UUID
    at: datetime
    action: str
    ticker: str | None = None
    quantity: Decimal | None = None
    cash: Decimal | None = None
    receipt_type: Literal["transaction", "cash"]


class BrokerEquityHistory(BaseModel):
    points: list[BrokerEquityPoint]
    events: list[BrokerLedgerMarker]
    older_events_omitted: bool
