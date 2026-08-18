"""Add the operational domain checkpoint and durable execution metadata.

Revision ID: 20260818_0006
Revises: 20260728_0005
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260818_0006"
down_revision = "20260728_0005"
branch_labels = None
depends_on = None


OPERATIONAL_STATUSES = (
    "draft",
    "testing",
    "ready",
    "degraded",
    "paused",
    "retired",
)

ITEM_OUTCOMES = (
    "found",
    "not_found",
    "retryable_error",
    "permanent_error",
    "credential_error",
    "portal_unavailable",
    "integration_unavailable",
)


def _json_default(value: str) -> sa.TextClause:
    return sa.text(f"'{value}'::jsonb")


def upgrade() -> None:
    # Platform remains the physical table name for compatibility, but its
    # domain meaning is now explicitly "processadora".
    op.execute(
        "COMMENT ON TABLE platforms IS "
        "'Processadoras dos portais; platform e mantido como nome fisico legado.'"
    )

    op.add_column(
        "municipalities",
        sa.Column(
            "operational_status",
            sa.String(length=20),
            nullable=False,
            server_default="draft",
        ),
    )
    op.add_column(
        "municipalities",
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            server_default="America/Fortaleza",
        ),
    )
    op.add_column(
        "municipalities",
        sa.Column(
            "input_schema",
            postgresql.JSONB(),
            nullable=False,
            server_default=_json_default("{}"),
        ),
    )
    op.add_column(
        "municipalities",
        sa.Column(
            "schedule_policy",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text(
                "jsonb_build_object("
                "'weekdays', jsonb_build_array(0, 1, 2, 3, 4), "
                "'start_hour', NULL, 'end_hour', NULL)"
            ),
        ),
    )
    op.add_column(
        "municipalities",
        sa.Column("adapter_version", sa.String(length=64), nullable=True),
    )
    op.execute(
        """
        UPDATE municipalities
           SET operational_status = CASE
                 WHEN slug IN ('boa-vista', 'gov-am', 'paulista') THEN 'ready'
                 WHEN slug = 'itabuna' THEN 'testing'
                 ELSE 'draft'
               END,
               timezone = CASE
                 WHEN slug = 'boa-vista' THEN 'America/Boa_Vista'
                 WHEN slug = 'gov-am' THEN 'America/Manaus'
                 ELSE 'America/Fortaleza'
               END,
               adapter_version = CASE platform_slug
                 WHEN 'rf1' THEN 'rf1.v1'
                 WHEN 'facil' THEN 'facil.v1'
                 WHEN 'consiglog' THEN 'consiglog.v1'
                 WHEN 'safeconsig' THEN 'safeconsig.legacy'
                 WHEN 'grid' THEN 'grid.legacy'
                 ELSE NULL
               END,
               input_schema = CASE
                 WHEN slug = 'paulista' THEN
                   jsonb_build_object(
                     'version', 1,
                     'required', jsonb_build_array('cpf', 'registration'),
                     'optional', jsonb_build_array(),
                     'deduplication_key', jsonb_build_array('cpf', 'registration')
                   )
                 WHEN slug IN ('boa-vista', 'gov-am') THEN
                   jsonb_build_object(
                     'version', 1,
                     'required', jsonb_build_array('cpf'),
                     'optional', jsonb_build_array('registration'),
                     'deduplication_key', jsonb_build_array('cpf')
                   )
                 ELSE
                   jsonb_build_object(
                     'version', 1,
                     'required', jsonb_build_array('cpf'),
                     'optional', jsonb_build_array('registration'),
                     'deduplication_key', jsonb_build_array('cpf', 'registration')
                   )
               END,
               schedule_policy = CASE
                 WHEN slug = 'boa-vista' THEN
                   jsonb_build_object(
                     'weekdays', jsonb_build_array(0, 1, 2, 3, 4, 5, 6),
                     'start_hour', 0,
                     'end_hour', 24
                   )
                 ELSE
                   jsonb_build_object(
                     'weekdays', jsonb_build_array(0, 1, 2, 3, 4),
                     'start_hour', NULL,
                     'end_hour', NULL
                   )
               END
        """
    )
    op.create_check_constraint(
        "ck_municipalities_operational_status",
        "municipalities",
        "operational_status IN ('draft', 'testing', 'ready', 'degraded', 'paused', 'retired')",
    )
    op.create_check_constraint(
        "ck_municipalities_schedule_policy",
        "municipalities",
        "jsonb_typeof(schedule_policy) = 'object' AND "
        "jsonb_typeof(schedule_policy->'weekdays') = 'array'",
    )
    op.create_index(
        "ix_municipalities_operational_catalog",
        "municipalities",
        ["operational_status", "enabled", "platform_slug"],
    )

    # New code uses an optional portal profile. The required consignataria
    # constraint is removed, while its column remains available to old workers.
    op.add_column(
        "portal_credentials",
        sa.Column("portal_profile", sa.String(length=160), nullable=True),
    )
    op.execute(
        "UPDATE portal_credentials "
        "SET portal_profile = COALESCE(NULLIF(btrim(consignataria), ''), "
        "NULLIF(btrim(settings_json->>'consignataria'), ''))"
    )
    op.drop_constraint(
        "ck_portal_credentials_consignataria_required",
        "portal_credentials",
        type_="check",
    )
    op.alter_column(
        "portal_credentials",
        "consignataria",
        existing_type=sa.String(length=160),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_portal_credentials_profile_nonblank",
        "portal_credentials",
        "portal_profile IS NULL OR btrim(portal_profile) <> ''",
    )

    op.add_column(
        "datasets", sa.Column("display_name", sa.String(length=160), nullable=True)
    )
    op.execute(
        "UPDATE datasets SET display_name = left("
        "COALESCE(NULLIF(btrim(original_filename), ''), 'Base ' || id::text), 160)"
    )
    op.alter_column(
        "datasets",
        "display_name",
        existing_type=sa.String(length=160),
        nullable=False,
    )
    op.add_column(
        "datasets",
        sa.Column(
            "duplicate_policy",
            sa.String(length=20),
            nullable=False,
            server_default="keep_all",
        ),
    )
    # A adição usa keep_all para preservar semanticamente bases históricas;
    # novas inserções passam a seguir a política oficial keep_first.
    op.alter_column(
        "datasets",
        "duplicate_policy",
        existing_type=sa.String(length=20),
        server_default="keep_first",
    )
    op.add_column(
        "datasets",
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=_json_default("{}"),
        ),
    )
    op.create_check_constraint(
        "ck_datasets_duplicate_policy",
        "datasets",
        "duplicate_policy IN ('reject', 'keep_first', 'keep_all')",
    )
    op.create_index(
        "ix_datasets_catalog",
        "datasets",
        ["municipality_slug", "status", "created_at"],
    )

    op.drop_constraint("ck_automation_jobs_status", "automation_jobs", type_="check")
    op.create_check_constraint(
        "ck_automation_jobs_status",
        "automation_jobs",
        "status IN ('awaiting_dataset', 'queued', 'running', 'paused', 'completed', "
        "'completed_with_errors', 'blocked', 'failed', 'cancelled')",
    )
    for column in ("found_items", "not_found_items", "retryable_items", "permanent_items"):
        op.add_column(
            "automation_jobs",
            sa.Column(column, sa.Integer(), nullable=False, server_default="0"),
        )
    op.create_check_constraint(
        "ck_automation_jobs_nonnegative_counters",
        "automation_jobs",
        "total_items >= 0 AND completed_items >= 0 AND failed_items >= 0 "
        "AND found_items >= 0 AND not_found_items >= 0 "
        "AND retryable_items >= 0 AND permanent_items >= 0",
    )

    op.add_column(
        "job_items", sa.Column("outcome", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "job_items",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column(
        "job_items",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "job_items",
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "job_items",
        sa.Column("last_error_category", sa.String(length=40), nullable=True),
    )
    op.create_check_constraint(
        "ck_job_items_outcome",
        "job_items",
        "outcome IS NULL OR outcome IN ('found', 'not_found', 'retryable_error', "
        "'permanent_error', 'credential_error', 'portal_unavailable', "
        "'integration_unavailable')",
    )
    op.create_check_constraint(
        "ck_job_items_attempt_limits",
        "job_items",
        "attempts >= 0 AND max_attempts > 0",
    )
    op.create_index(
        "ix_job_items_retry_ready",
        "job_items",
        ["job_id", "status", "next_attempt_at", "id"],
    )
    # Mantém o retorno da tentativa anterior para auditoria, mas permite
    # marcá-lo como obsoleto durante um retry manual sem apagá-lo.
    op.add_column(
        "consultation_results_v2",
        sa.Column("attempt_number", sa.Integer(), nullable=True),
    )
    op.add_column(
        "consultation_results_v2",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE consultation_results_v2 AS result "
        "SET attempt_number = item.attempts "
        "FROM job_items AS item WHERE item.id = result.job_item_id"
    )

    op.create_table(
        "job_item_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_item_id",
            sa.BigInteger(),
            sa.ForeignKey("job_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "credential_id",
            sa.BigInteger(),
            sa.ForeignKey("portal_credentials.id", ondelete="SET NULL"),
        ),
        sa.Column("worker_id", sa.String(length=160)),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="started"),
        sa.Column("error_category", sa.String(length=40)),
        sa.Column("error_code", sa.String(length=80)),
        sa.Column("error_message", sa.Text()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column(
            "details_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=_json_default("{}"),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "job_item_id", "attempt_number", name="uq_job_item_attempt_number"
        ),
        sa.CheckConstraint("attempt_number > 0", name="ck_job_item_attempt_number"),
        sa.CheckConstraint(
            "status IN ('started', 'found', 'not_found', 'retryable_error', "
            "'permanent_error', 'credential_error', 'portal_unavailable', "
            "'integration_unavailable', 'abandoned')",
            name="ck_job_item_attempt_status",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_job_item_attempt_duration",
        ),
    )
    op.create_index(
        "ix_job_item_attempts_job_item_id", "job_item_attempts", ["job_item_id"]
    )
    op.create_index(
        "ix_job_item_attempts_credential_id", "job_item_attempts", ["credential_id"]
    )
    op.create_index(
        "ix_job_item_attempts_worker_id", "job_item_attempts", ["worker_id"]
    )
    op.create_index(
        "ix_job_item_attempts_item_started",
        "job_item_attempts",
        ["job_item_id", "started_at"],
    )

    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=160), primary_key=True),
        sa.Column(
            "platform_slug",
            sa.String(length=64),
            sa.ForeignKey("platforms.slug", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "municipality_slug",
            sa.String(length=80),
            sa.ForeignKey("municipalities.slug", ondelete="SET NULL"),
        ),
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey("automation_jobs.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "credential_id",
            sa.BigInteger(),
            sa.ForeignKey("portal_credentials.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "health_status", sa.String(length=20), nullable=False, server_default="healthy"
        ),
        sa.Column(
            "activity_status", sa.String(length=20), nullable=False, server_default="starting"
        ),
        sa.Column("adapter_version", sa.String(length=64)),
        sa.Column("hostname", sa.String(length=255)),
        sa.Column("process_id", sa.Integer()),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "details_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=_json_default("{}"),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "health_status IN ('healthy', 'degraded', 'unhealthy', 'stopping')",
            name="ck_worker_heartbeats_health_status",
        ),
        sa.CheckConstraint(
            "activity_status IN ('starting', 'idle', 'busy', 'backoff', 'stopped')",
            name="ck_worker_heartbeats_activity_status",
        ),
    )
    for column in ("platform_slug", "municipality_slug", "job_id", "credential_id"):
        op.create_index(
            f"ix_worker_heartbeats_{column}", "worker_heartbeats", [column]
        )
    op.create_index(
        "ix_worker_heartbeats_last_seen_at", "worker_heartbeats", ["last_seen_at"]
    )
    op.create_index(
        "ix_worker_heartbeats_expires_at", "worker_heartbeats", ["expires_at"]
    )
    op.create_index(
        "ix_worker_heartbeats_health",
        "worker_heartbeats",
        ["health_status", "expires_at"],
    )

    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("deduplication_key", sa.String(length=160), nullable=False, unique=True),
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey("automation_jobs.id", ondelete="CASCADE"),
        ),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("recipient", sa.String(length=255)),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column(
            "payload_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=_json_default("{}"),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("locked_by", sa.String(length=160)),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "channel IN ('telegram', 'email', 'webhook')",
            name="ck_notification_outbox_channel",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'retry', 'sent', 'failed', 'cancelled')",
            name="ck_notification_outbox_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts > 0",
            name="ck_notification_outbox_attempt_limits",
        ),
    )
    op.create_index("ix_notification_outbox_job_id", "notification_outbox", ["job_id"])
    op.create_index(
        "ix_notification_outbox_next_attempt_at",
        "notification_outbox",
        ["next_attempt_at"],
    )
    op.create_index(
        "ix_notification_outbox_claim",
        "notification_outbox",
        ["status", "next_attempt_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("notification_outbox")
    op.drop_table("worker_heartbeats")
    op.drop_table("job_item_attempts")

    op.drop_column("consultation_results_v2", "superseded_at")
    op.drop_column("consultation_results_v2", "attempt_number")

    op.drop_index("ix_job_items_retry_ready", table_name="job_items")
    op.drop_constraint("ck_job_items_attempt_limits", "job_items", type_="check")
    op.drop_constraint("ck_job_items_outcome", "job_items", type_="check")
    for column in (
        "last_error_category",
        "last_attempt_at",
        "next_attempt_at",
        "max_attempts",
        "outcome",
    ):
        op.drop_column("job_items", column)

    op.drop_constraint(
        "ck_automation_jobs_nonnegative_counters", "automation_jobs", type_="check"
    )
    op.execute(
        "UPDATE automation_jobs SET status = CASE "
        "WHEN status = 'completed_with_errors' THEN 'failed' "
        "WHEN status = 'blocked' THEN 'paused' ELSE status END"
    )
    op.drop_constraint("ck_automation_jobs_status", "automation_jobs", type_="check")
    op.create_check_constraint(
        "ck_automation_jobs_status",
        "automation_jobs",
        "status IN ('awaiting_dataset', 'queued', 'running', 'paused', "
        "'completed', 'failed', 'cancelled')",
    )
    for column in ("permanent_items", "retryable_items", "not_found_items", "found_items"):
        op.drop_column("automation_jobs", column)

    op.drop_index("ix_datasets_catalog", table_name="datasets")
    op.drop_constraint("ck_datasets_duplicate_policy", "datasets", type_="check")
    for column in ("metadata_json", "duplicate_policy", "display_name"):
        op.drop_column("datasets", column)

    op.drop_constraint(
        "ck_portal_credentials_profile_nonblank", "portal_credentials", type_="check"
    )
    op.execute(
        "UPDATE portal_credentials SET consignataria = COALESCE("
        "NULLIF(btrim(consignataria), ''), NULLIF(btrim(portal_profile), ''), "
        "'LEGACY_UNSPECIFIED')"
    )
    op.create_check_constraint(
        "ck_portal_credentials_consignataria_required",
        "portal_credentials",
        "consignataria IS NOT NULL AND btrim(consignataria) <> ''",
        postgresql_not_valid=True,
    )
    op.drop_column("portal_credentials", "portal_profile")

    op.drop_index("ix_municipalities_operational_catalog", table_name="municipalities")
    op.drop_constraint(
        "ck_municipalities_operational_status", "municipalities", type_="check"
    )
    op.drop_constraint(
        "ck_municipalities_schedule_policy", "municipalities", type_="check"
    )
    for column in (
        "adapter_version",
        "schedule_policy",
        "input_schema",
        "timezone",
        "operational_status",
    ):
        op.drop_column("municipalities", column)
    op.execute("COMMENT ON TABLE platforms IS NULL")
