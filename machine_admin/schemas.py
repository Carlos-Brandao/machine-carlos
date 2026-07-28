from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class RequestedBy(BaseModel):
    telegram_user_id: int
    telegram_chat_id: int


class BatchRequest(BaseModel):
    prefeituras: list[str] = Field(min_length=1, max_length=50)
    requested_by: RequestedBy


class AcquireCredentialRequest(BaseModel):
    job_id: int
    municipality_slug: str = Field(min_length=1, max_length=80)
    worker_id: str = Field(min_length=3, max_length=160)
    lease_seconds: int = Field(default=120, ge=30, le=600)


class ClaimItemsRequest(BaseModel):
    job_id: int
    credential_id: int
    worker_id: str = Field(min_length=3, max_length=160)
    batch_size: int = Field(default=10, ge=1, le=100)
    lease_seconds: int = Field(default=120, ge=30, le=600)


class WorkerRequest(BaseModel):
    worker_id: str = Field(min_length=3, max_length=160)
    lease_seconds: int = Field(default=120, ge=30, le=600)


class CredentialReportRequest(BaseModel):
    worker_id: str = Field(min_length=3, max_length=160)
    credential_id: int
    outcome: Literal["success", "transient_failure", "invalid_credentials", "portal_unavailable"]
    error_message: str | None = Field(default=None, max_length=500)
    cooldown_seconds: int = Field(default=900, ge=60, le=86_400)


class CompleteItemRequest(BaseModel):
    worker_id: str = Field(min_length=3, max_length=160)
    item_id: int
    status: Literal["completed", "failed"]
    result_data: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=80)
    error_message: str | None = Field(default=None, max_length=2000)
