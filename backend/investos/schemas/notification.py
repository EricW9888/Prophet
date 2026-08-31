from uuid import UUID

from pydantic import BaseModel, Field


class PushSubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=256)
    auth: str = Field(min_length=1, max_length=256)


class PushSubscriptionCreate(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)
    keys: PushSubscriptionKeys


class PushSubscriptionEndpointRequest(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)


class PushNotificationStatusResponse(BaseModel):
    enabled: bool
    ready: bool
    configuration_error: str | None = None
    application_server_key: str | None = None
    active_subscription_count: int


class PushSubscriptionResponse(BaseModel):
    subscription_id: UUID
    active: bool


class PushNotificationTestResponse(BaseModel):
    status: str
    sent: bool
