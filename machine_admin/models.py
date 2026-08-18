"""Modelos relacionais do painel, da fila e dos workers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AdminUser(TimestampMixin, Base):
    __tablename__ = "admin_users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'operator', 'viewer')", name="ck_admin_users_role"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="operator")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiToken(TimestampMixin, Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner: Mapped[AdminUser] = relationship()


class Platform(TimestampMixin, Base):
    __tablename__ = "platforms"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    runner: Mapped[str] = mapped_column(String(64), nullable=False)
    start_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    end_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=21)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Municipality(TimestampMixin, Base):
    __tablename__ = "municipalities"
    __table_args__ = (
        CheckConstraint(
            "operational_status IN ('draft', 'testing', 'ready', 'degraded', 'paused', 'retired')",
            name="ck_municipalities_operational_status",
        ),
        CheckConstraint(
            "jsonb_typeof(schedule_policy) = 'object' AND "
            "jsonb_typeof(schedule_policy->'weekdays') = 'array'",
            name="ck_municipalities_schedule_policy",
        ),
        Index(
            "ix_municipalities_operational_catalog",
            "operational_status",
            "enabled",
            "platform_slug",
        ),
    )

    slug: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    platform_slug: Mapped[str] = mapped_column(
        ForeignKey("platforms.slug", ondelete="RESTRICT"), nullable=False, index=True
    )
    login_url: Mapped[str | None] = mapped_column(Text)
    query_url: Mapped[str | None] = mapped_column(Text)
    max_workers: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    operational_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft"
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="America/Fortaleza"
    )
    input_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    schedule_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {
            "weekdays": [0, 1, 2, 3, 4],
            "start_hour": None,
            "end_hour": None,
        },
    )
    adapter_version: Mapped[str | None] = mapped_column(String(64))
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    platform: Mapped[Platform] = relationship()


class PortalCredential(TimestampMixin, Base):
    __tablename__ = "portal_credentials"
    __table_args__ = (
        UniqueConstraint("municipality_slug", "label", name="uq_portal_credentials_label"),
        CheckConstraint(
            "status IN ('active', 'disabled', 'cooldown', 'invalid')",
            name="ck_portal_credentials_status",
        ),
        CheckConstraint(
            "portal_profile IS NULL OR btrim(portal_profile) <> ''",
            name="ck_portal_credentials_profile_nonblank",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    municipality_slug: Mapped[str] = mapped_column(
        ForeignKey("municipalities.slug", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    encryption_context: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    username_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    password_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    portal_username: Mapped[str | None] = mapped_column(Text)
    portal_password: Mapped[str | None] = mapped_column(Text)
    # ``consignataria`` permanece durante a transição porque workers e telas
    # legados ainda o consomem. Novas regras usam ``portal_profile`` e só o
    # exigem quando o portal realmente oferece mais de um perfil.
    consignataria: Mapped[str | None] = mapped_column(String(160))
    portal_profile: Mapped[str | None] = mapped_column(String(160))
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    max_parallel_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    municipality: Mapped[Municipality] = relationship()


class IntegrationSecret(TimestampMixin, Base):
    __tablename__ = "integration_secrets"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    description: Mapped[str | None] = mapped_column(String(240))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Dataset(TimestampMixin, Base):
    __tablename__ = "datasets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('uploading', 'ready', 'invalid', 'archived')",
            name="ck_datasets_status",
        ),
        CheckConstraint(
            "duplicate_policy IN ('reject', 'keep_first', 'keep_all')",
            name="ck_datasets_duplicate_policy",
        ),
        Index(
            "ix_datasets_catalog",
            "municipality_slug",
            "status",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    municipality_slug: Mapped[str] = mapped_column(
        ForeignKey("municipalities.slug", ondelete="RESTRICT"), nullable=False, index=True
    )
    uploaded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_policy: Mapped[str] = mapped_column(
        String(20), nullable=False, default="keep_first"
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    custom_columns: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="uploading")
    error_message: Mapped[str | None] = mapped_column(Text)


class DatasetRecord(Base):
    __tablename__ = "dataset_records"
    __table_args__ = (
        UniqueConstraint("dataset_id", "row_number", name="uq_dataset_records_row"),
        Index("ix_dataset_records_fingerprint", "dataset_id", "cpf_fingerprint"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    encryption_context: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    cpf_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    cpf_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    cpf_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    registration: Mapped[str | None] = mapped_column(String(120))
    source_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    source_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class Job(TimestampMixin, Base):
    __tablename__ = "automation_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('awaiting_dataset', 'queued', 'running', 'paused', 'completed', 'completed_with_errors', 'blocked', 'failed', 'cancelled')",
            name="ck_automation_jobs_status",
        ),
        CheckConstraint(
            "total_items >= 0 AND completed_items >= 0 AND failed_items >= 0 "
            "AND found_items >= 0 AND not_found_items >= 0 "
            "AND retryable_items >= 0 AND permanent_items >= 0",
            name="ck_automation_jobs_nonnegative_counters",
        ),
        Index("ix_automation_jobs_queue", "status", "priority", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    municipality_slug: Mapped[str] = mapped_column(
        ForeignKey("municipalities.slug", ondelete="RESTRICT"), nullable=False, index=True
    )
    dataset_id: Mapped[int | None] = mapped_column(
        ForeignKey("datasets.id", ondelete="RESTRICT"), index=True
    )
    requested_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), index=True
    )
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    found_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_found_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retryable_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    permanent_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class JobItem(Base):
    __tablename__ = "job_items"
    __table_args__ = (
        UniqueConstraint("job_id", "dataset_record_id", name="uq_job_items_record"),
        CheckConstraint(
            "status IN ('pending', 'leased', 'completed', 'failed', 'cancelled')",
            name="ck_job_items_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('found', 'not_found', 'retryable_error', "
            "'permanent_error', 'credential_error', 'portal_unavailable', "
            "'integration_unavailable')",
            name="ck_job_items_outcome",
        ),
        CheckConstraint(
            "attempts >= 0 AND max_attempts > 0",
            name="ck_job_items_attempt_limits",
        ),
        Index("ix_job_items_claim", "job_id", "status", "lease_expires_at", "id"),
        Index(
            "ix_job_items_retry_ready",
            "job_id",
            "status",
            "next_attempt_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("automation_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_record_id: Mapped[int] = mapped_column(
        ForeignKey("dataset_records.id", ondelete="CASCADE"), nullable=False
    )
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("portal_credentials.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    outcome: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    last_error_category: Mapped[str | None] = mapped_column(String(40))


class JobItemAttempt(Base):
    """Histórico imutável das tentativas de cada item consultável."""

    __tablename__ = "job_item_attempts"
    __table_args__ = (
        UniqueConstraint(
            "job_item_id", "attempt_number", name="uq_job_item_attempt_number"
        ),
        CheckConstraint("attempt_number > 0", name="ck_job_item_attempt_number"),
        CheckConstraint(
            "status IN ('started', 'found', 'not_found', 'retryable_error', "
            "'permanent_error', 'credential_error', 'portal_unavailable', "
            "'integration_unavailable', 'abandoned')",
            name="ck_job_item_attempt_status",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_job_item_attempt_duration",
        ),
        Index("ix_job_item_attempts_item_started", "job_item_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_item_id: Mapped[int] = mapped_column(
        ForeignKey("job_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("portal_credentials.id", ondelete="SET NULL"), index=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(160), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    error_category: Mapped[str | None] = mapped_column(String(40))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    details_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CredentialLease(Base):
    __tablename__ = "credential_leases"

    credential_id: Mapped[int] = mapped_column(
        ForeignKey("portal_credentials.id", ondelete="CASCADE"), primary_key=True
    )
    job_id: Mapped[int] = mapped_column(
        ForeignKey("automation_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    worker_id: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class WorkerHeartbeat(Base):
    """Presença e saúde observável dos processos executores."""

    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        CheckConstraint(
            "health_status IN ('healthy', 'degraded', 'unhealthy', 'stopping')",
            name="ck_worker_heartbeats_health_status",
        ),
        CheckConstraint(
            "activity_status IN ('starting', 'idle', 'busy', 'backoff', 'stopped')",
            name="ck_worker_heartbeats_activity_status",
        ),
        Index(
            "ix_worker_heartbeats_health",
            "health_status",
            "expires_at",
        ),
    )

    worker_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    platform_slug: Mapped[str] = mapped_column(
        ForeignKey("platforms.slug", ondelete="RESTRICT"), nullable=False, index=True
    )
    municipality_slug: Mapped[str | None] = mapped_column(
        ForeignKey("municipalities.slug", ondelete="SET NULL"), index=True
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("automation_jobs.id", ondelete="SET NULL"), index=True
    )
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("portal_credentials.id", ondelete="SET NULL"), index=True
    )
    health_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="healthy"
    )
    activity_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="starting"
    )
    adapter_version: Mapped[str | None] = mapped_column(String(64))
    hostname: Mapped[str | None] = mapped_column(String(255))
    process_id: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    details_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class ConsultationResult(Base):
    __tablename__ = "consultation_results_v2"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_item_id: Mapped[int] = mapped_column(
        ForeignKey("job_items.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("portal_credentials.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    result_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    attempt_number: Mapped[int | None] = mapped_column(Integer)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    consulted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class JobEvent(Base):
    __tablename__ = "job_events_v2"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("automation_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    event_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


class NotificationOutbox(TimestampMixin, Base):
    """Entrega durável e idempotente de notificações externas."""

    __tablename__ = "notification_outbox"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('telegram', 'email', 'webhook')",
            name="ck_notification_outbox_channel",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'retry', 'sent', 'failed', 'cancelled')",
            name="ck_notification_outbox_status",
        ),
        CheckConstraint(
            "attempts >= 0 AND max_attempts > 0",
            name="ck_notification_outbox_attempt_limits",
        ),
        Index(
            "ix_notification_outbox_claim",
            "status",
            "next_attempt_at",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    deduplication_key: Mapped[str] = mapped_column(
        String(160), nullable=False, unique=True
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("automation_jobs.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    locked_by: Mapped[str | None] = mapped_column(String(160))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(120))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
