from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class RequestedBy(BaseModel):
    telegram_user_id: int
    telegram_chat_id: int


class JobSelection(BaseModel):
    municipality_slug: str = Field(min_length=1, max_length=80)
    dataset_id: int = Field(gt=0)


class BatchRequest(BaseModel):
    # ``prefeituras`` permanece durante a atualização do bot antigo, mas novos
    # clientes devem sempre enviar uma base explícita em ``jobs``.
    jobs: list[JobSelection] = Field(default_factory=list, max_length=50)
    prefeituras: list[str] = Field(default_factory=list, max_length=50)
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


class WorkerStatusRequest(BaseModel):
    worker_id: str = Field(min_length=3, max_length=160)
    platform_slug: str = Field(min_length=1, max_length=64)
    municipality_slug: str | None = Field(default=None, max_length=80)
    job_id: int | None = None
    credential_id: int | None = None
    health_status: Literal["healthy", "degraded", "unhealthy", "stopping"] = "healthy"
    activity_status: Literal["starting", "idle", "busy", "backoff", "stopped"] = "idle"
    adapter_version: str | None = Field(default=None, max_length=64)
    hostname: str | None = Field(default=None, max_length=255)
    process_id: int | None = Field(default=None, ge=1)
    last_error: str | None = Field(default=None, max_length=1000)
    ttl_seconds: int = Field(default=90, ge=30, le=600)
    details: dict[str, Any] = Field(default_factory=dict)


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
    outcome: Literal[
        "found",
        "not_found",
        "retryable_error",
        "permanent_error",
        "credential_error",
        "portal_unavailable",
        "integration_unavailable",
    ] | None = None
    result_data: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=80)
    error_message: str | None = Field(default=None, max_length=2000)
    stage: str | None = Field(default=None, max_length=80)
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    details: dict[str, Any] = Field(default_factory=dict)


class RequeueItemRequest(BaseModel):
    """Devolve um item alugado quando a infraestrutura externa está indisponível."""

    worker_id: str = Field(min_length=3, max_length=160)
    item_id: int
    reason: str = Field(min_length=1, max_length=500)
    outcome: Literal[
        "retryable_error", "credential_error", "portal_unavailable", "integration_unavailable"
    ] = "retryable_error"
    error_code: str | None = Field(default=None, max_length=80)
    stage: str | None = Field(default=None, max_length=80)
    retry_after_seconds: int | None = Field(default=None, ge=5, le=86_400)
