from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SecurityBase(BaseModel):
    ticker: str
    asset_class: str
    name: str


class SecurityCreate(SecurityBase):
    pass


class SecurityResponse(SecurityBase):
    id: UUID
    entity_id: UUID | None = None

    model_config = ConfigDict(from_attributes=True)
