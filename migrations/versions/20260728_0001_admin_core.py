"""Create the PostgreSQL administrative and worker core.

Revision ID: 20260728_0001
Revises: None
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260728_0001"
down_revision = None
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("session_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint("role IN ('admin', 'operator', 'viewer')", name="ck_admin_users_role"),
    )
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.BigInteger(), sa.ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("token_prefix", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "scopes", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index("ix_api_tokens_owner_id", "api_tokens", ["owner_id"])
    op.create_index("ix_api_tokens_token_prefix", "api_tokens", ["token_prefix"])

    op.create_table(
        "platforms",
        sa.Column("slug", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("runner", sa.String(64), nullable=False),
        sa.Column("start_hour", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("end_hour", sa.Integer(), nullable=False, server_default="21"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
    )
    op.create_table(
        "municipalities",
        sa.Column("slug", sa.String(80), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("platform_slug", sa.String(64), sa.ForeignKey("platforms.slug", ondelete="RESTRICT"), nullable=False),
        sa.Column("login_url", sa.Text()),
        sa.Column("query_url", sa.Text()),
        sa.Column("max_workers", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "settings_json", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        *_timestamps(),
    )
    op.create_index("ix_municipalities_platform_slug", "municipalities", ["platform_slug"])

    op.create_table(
        "portal_credentials",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("municipality_slug", sa.String(80), sa.ForeignKey("municipalities.slug", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("encryption_context", sa.String(64), nullable=False, unique=True),
        sa.Column("username_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("password_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("max_parallel_sessions", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cooldown_until", sa.DateTime(timezone=True)),
        sa.Column("last_validated_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "settings_json", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        *_timestamps(),
        sa.UniqueConstraint("municipality_slug", "label", name="uq_portal_credentials_label"),
        sa.CheckConstraint("status IN ('active', 'disabled', 'cooldown', 'invalid')", name="ck_portal_credentials_status"),
    )
    op.create_index("ix_portal_credentials_municipality_slug", "portal_credentials", ["municipality_slug"])

    op.create_table(
        "integration_secrets",
        sa.Column("key", sa.String(120), primary_key=True),
        sa.Column("value_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("description", sa.String(240)),
        sa.Column("rotated_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_table(
        "datasets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("municipality_slug", sa.String(80), sa.ForeignKey("municipalities.slug", ondelete="RESTRICT"), nullable=False),
        sa.Column("uploaded_by_id", sa.BigInteger(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="uploading"),
        sa.Column("error_message", sa.Text()),
        *_timestamps(),
        sa.CheckConstraint("status IN ('uploading', 'ready', 'invalid', 'archived')", name="ck_datasets_status"),
    )
    op.create_index("ix_datasets_municipality_slug", "datasets", ["municipality_slug"])
    op.create_index("ix_datasets_uploaded_by_id", "datasets", ["uploaded_by_id"])
    op.create_index("ix_datasets_sha256", "datasets", ["sha256"])

    op.create_table(
        "dataset_records",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("dataset_id", sa.BigInteger(), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("encryption_context", sa.String(64), nullable=False, unique=True),
        sa.Column("cpf_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("cpf_fingerprint", sa.String(64), nullable=False),
        sa.Column("cpf_last4", sa.String(4), nullable=False),
        sa.Column("registration", sa.String(120)),
        sa.Column("source_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "source_data", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.UniqueConstraint("dataset_id", "row_number", name="uq_dataset_records_row"),
    )
    op.create_index("ix_dataset_records_dataset_id", "dataset_records", ["dataset_id"])
    op.create_index("ix_dataset_records_fingerprint", "dataset_records", ["dataset_id", "cpf_fingerprint"])

    op.create_table(
        "automation_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("municipality_slug", sa.String(80), sa.ForeignKey("municipalities.slug", ondelete="RESTRICT"), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), sa.ForeignKey("datasets.id", ondelete="RESTRICT")),
        sa.Column("requested_by_id", sa.BigInteger(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")),
        sa.Column("telegram_user_id", sa.BigInteger()),
        sa.Column("telegram_chat_id", sa.BigInteger()),
        sa.Column("status", sa.String(20), nullable=False, server_default="awaiting_dataset"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("total_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("not_before", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("error_message", sa.Text()),
        *_timestamps(),
        sa.CheckConstraint("status IN ('awaiting_dataset', 'queued', 'running', 'completed', 'failed', 'cancelled')", name="ck_automation_jobs_status"),
    )
    op.create_index("ix_automation_jobs_municipality_slug", "automation_jobs", ["municipality_slug"])
    op.create_index("ix_automation_jobs_dataset_id", "automation_jobs", ["dataset_id"])
    op.create_index("ix_automation_jobs_requested_by_id", "automation_jobs", ["requested_by_id"])
    op.create_index("ix_automation_jobs_queue", "automation_jobs", ["status", "priority", "created_at"])

    op.create_table(
        "job_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("automation_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dataset_record_id", sa.BigInteger(), sa.ForeignKey("dataset_records.id", ondelete="CASCADE"), nullable=False),
        sa.Column("credential_id", sa.BigInteger(), sa.ForeignKey("portal_credentials.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.String(160)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(80)),
        sa.Column("error_message", sa.Text()),
        sa.UniqueConstraint("job_id", "dataset_record_id", name="uq_job_items_record"),
        sa.CheckConstraint("status IN ('pending', 'leased', 'completed', 'failed', 'cancelled')", name="ck_job_items_status"),
    )
    op.create_index("ix_job_items_job_id", "job_items", ["job_id"])
    op.create_index("ix_job_items_credential_id", "job_items", ["credential_id"])
    op.create_index("ix_job_items_claim", "job_items", ["job_id", "status", "lease_expires_at", "id"])

    op.create_table(
        "credential_leases",
        sa.Column("credential_id", sa.BigInteger(), sa.ForeignKey("portal_credentials.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("automation_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("worker_id", sa.String(160), nullable=False, unique=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_credential_leases_job_id", "credential_leases", ["job_id"])
    op.create_index("ix_credential_leases_expires_at", "credential_leases", ["expires_at"])

    op.create_table(
        "consultation_results_v2",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job_item_id", sa.BigInteger(), sa.ForeignKey("job_items.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("credential_id", sa.BigInteger(), sa.ForeignKey("portal_credentials.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("result_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("consulted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "job_events_v2",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("automation_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column(
            "event_data", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_job_events_v2_job_id", "job_events_v2", ["job_id"])
    op.create_index("ix_job_events_v2_created_at", "job_events_v2", ["created_at"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("actor_id", sa.BigInteger(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("target_type", sa.String(80), nullable=False),
        sa.Column("target_id", sa.String(120)),
        sa.Column("ip_address", sa.String(64)),
        sa.Column(
            "details", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    for table in (
        "audit_logs",
        "job_events_v2",
        "consultation_results_v2",
        "credential_leases",
        "job_items",
        "automation_jobs",
        "dataset_records",
        "datasets",
        "integration_secrets",
        "portal_credentials",
        "municipalities",
        "platforms",
        "api_tokens",
        "admin_users",
    ):
        op.drop_table(table)
