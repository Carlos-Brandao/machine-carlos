"""Persistent operational control. Revision 20260930_0009."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260930_0009"
down_revision = "20260921_0008"
branch_labels = None
depends_on = None


def timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())]


def upgrade():
    op.add_column("automation_jobs", sa.Column("selected_credential_ids", postgresql.JSONB()))
    op.add_column("automation_jobs", sa.Column("max_parallel_accounts", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("automation_jobs", sa.Column("result_version", sa.Integer(), nullable=False, server_default="1"))
    op.drop_constraint("ck_automation_jobs_status", "automation_jobs", type_="check")
    op.create_check_constraint("ck_automation_jobs_status", "automation_jobs", "status IN ('awaiting_dataset','queued','running','pausing','paused','cancelling','completed','completed_with_errors','blocked','failed','cancelled')")
    op.add_column("portal_credentials", sa.Column("login_identity", sa.Text()))
    op.add_column("portal_credentials", sa.Column("login_failure_count", sa.Integer(), nullable=False, server_default="0"))
    op.execute("""UPDATE portal_credentials SET login_failure_count=3, status='invalid'
      WHERE failure_count>=3 AND status IN ('active','cooldown') AND
        (lower(last_error) LIKE '%login%não%confirmado%' OR lower(last_error) LIKE '%usuário ou senha inválidos%')""")
    op.execute("UPDATE portal_credentials SET login_identity=NULLIF(lower(btrim(portal_username)), '')")
    op.execute("""WITH duplicates AS (
      SELECT id, row_number() OVER (PARTITION BY municipality_slug,login_identity ORDER BY id) AS n
      FROM portal_credentials WHERE login_identity IS NOT NULL)
      UPDATE portal_credentials SET status='disabled', login_identity=NULL,
        last_error='Cadastro duplicado do mesmo login; utilize o acesso original.'
      WHERE id IN (SELECT id FROM duplicates WHERE n>1)""")
    op.create_unique_constraint("uq_portal_credentials_identity", "portal_credentials", ["municipality_slug", "login_identity"])
    op.add_column("credential_leases", sa.Column("lease_token", sa.String(64)))
    op.execute("UPDATE credential_leases SET lease_token=md5(random()::text || clock_timestamp()::text || credential_id::text)")
    op.alter_column("credential_leases", "lease_token", nullable=False)
    op.alter_column("credential_leases", "job_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column("job_items", sa.Column("lease_token", sa.String(64)))
    op.add_column("job_items", sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"))
    op.execute("UPDATE job_items SET retry_count=LEAST(attempts,max_attempts)")
    op.drop_constraint("ck_worker_heartbeats_activity_status", "worker_heartbeats", type_="check")
    op.create_check_constraint("ck_worker_heartbeats_activity_status", "worker_heartbeats", "activity_status IN ('starting','idle','busy','backoff','draining','stopped')")
    op.create_table("operational_blocks",
        sa.Column("scope_type",sa.String(20),primary_key=True),sa.Column("scope_key",sa.String(80),primary_key=True),
        sa.Column("blocked_until",sa.DateTime(timezone=True),nullable=False),sa.Column("error_code",sa.String(80)),
        sa.Column("message",sa.Text()),sa.Column("failure_count",sa.Integer(),nullable=False,server_default="0"))
    op.create_table("portal_access_checks",
        sa.Column("id",sa.BigInteger(),primary_key=True),
        sa.Column("credential_id",sa.BigInteger(),sa.ForeignKey("portal_credentials.id",ondelete="RESTRICT"),nullable=False,index=True),
        sa.Column("requested_by_id",sa.BigInteger(),sa.ForeignKey("admin_users.id",ondelete="SET NULL")),
        sa.Column("status",sa.String(20),nullable=False,server_default="queued",index=True),
        sa.Column("worker_id",sa.String(160)),sa.Column("lease_token",sa.String(64)),sa.Column("error_code",sa.String(80)),sa.Column("message",sa.Text()),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column("started_at",sa.DateTime(timezone=True)),sa.Column("finished_at",sa.DateTime(timezone=True)),sa.Column("expires_at",sa.DateTime(timezone=True)))
    op.create_table("job_requests",
        sa.Column("namespace",sa.String(120),primary_key=True),sa.Column("request_key",sa.String(160),primary_key=True),
        sa.Column("payload_hash",sa.String(64),nullable=False),sa.Column("job_id",sa.BigInteger(),sa.ForeignKey("automation_jobs.id",ondelete="RESTRICT"),nullable=False),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()))
    op.create_table("consultation_schedules",
        sa.Column("id",sa.BigInteger(),primary_key=True),sa.Column("name",sa.String(160),nullable=False),
        sa.Column("dataset_id",sa.BigInteger(),sa.ForeignKey("datasets.id",ondelete="RESTRICT"),nullable=False),
        sa.Column("requested_by_id",sa.BigInteger(),sa.ForeignKey("admin_users.id",ondelete="SET NULL")),
        sa.Column("cron_expression",sa.String(120),nullable=False),sa.Column("timezone",sa.String(64),nullable=False,server_default="America/Fortaleza"),
        sa.Column("selected_credential_ids",postgresql.JSONB()),sa.Column("max_parallel_accounts",sa.Integer(),nullable=False,server_default="1"),
        sa.Column("enabled",sa.Boolean(),nullable=False,server_default=sa.true()),sa.Column("misfire_grace_seconds",sa.Integer(),nullable=False,server_default="300"),
        sa.Column("next_run_at",sa.DateTime(timezone=True),nullable=False),sa.Column("last_run_at",sa.DateTime(timezone=True)),*timestamps(),
        sa.CheckConstraint("max_parallel_accounts BETWEEN 1 AND 20",name="ck_schedules_accounts"))
    op.create_index("ix_schedules_due","consultation_schedules",["enabled","next_run_at"])
    op.create_table("schedule_occurrences",
        sa.Column("id",sa.BigInteger(),primary_key=True),sa.Column("schedule_id",sa.BigInteger(),sa.ForeignKey("consultation_schedules.id",ondelete="CASCADE"),nullable=False,index=True),
        sa.Column("scheduled_for",sa.DateTime(timezone=True),nullable=False),sa.Column("status",sa.String(32),nullable=False),
        sa.Column("job_id",sa.BigInteger(),sa.ForeignKey("automation_jobs.id",ondelete="RESTRICT")),sa.Column("message",sa.Text()),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint("schedule_id","scheduled_for",name="uq_schedule_occurrence"))
    op.create_table("export_artifacts",
        sa.Column("id",sa.BigInteger(),primary_key=True),sa.Column("job_id",sa.BigInteger(),sa.ForeignKey("automation_jobs.id",ondelete="RESTRICT"),nullable=False,index=True),
        sa.Column("result_version",sa.Integer(),nullable=False),sa.Column("snapshot_hash",sa.String(64),nullable=False),sa.Column("snapshot_ciphertext",sa.LargeBinary(),nullable=False),
        sa.Column("format",sa.String(8),nullable=False),sa.Column("status",sa.String(20),nullable=False,server_default="queued"),sa.Column("filename",sa.String(255),nullable=False),
        sa.Column("storage_path",sa.Text()),sa.Column("sha256",sa.String(64)),sa.Column("row_count",sa.Integer(),nullable=False,server_default="0"),
        sa.Column("partial",sa.Boolean(),nullable=False,server_default=sa.false()),sa.Column("size_bytes",sa.BigInteger()),sa.Column("error_message",sa.Text()),
        sa.Column("attempts",sa.Integer(),nullable=False,server_default="0"),sa.Column("locked_by",sa.String(64)),sa.Column("locked_until",sa.DateTime(timezone=True)),
        sa.Column("ready_at",sa.DateTime(timezone=True)),*timestamps(),
        sa.UniqueConstraint("job_id","result_version","snapshot_hash","format",name="uq_export_snapshot_format"))
    op.create_index("ix_export_artifacts_due","export_artifacts",["status","locked_until"])
    op.create_table("webhook_endpoints",
        sa.Column("id",sa.BigInteger(),primary_key=True),sa.Column("owner_id",sa.BigInteger(),sa.ForeignKey("admin_users.id",ondelete="CASCADE"),nullable=False,index=True),
        sa.Column("name",sa.String(120),nullable=False),sa.Column("url",sa.Text(),nullable=False),sa.Column("signing_secret_ciphertext",sa.LargeBinary(),nullable=False),
        sa.Column("enabled",sa.Boolean(),nullable=False,server_default=sa.true()),*timestamps())
    op.execute("UPDATE notification_outbox SET status='cancelled', last_error='Integração Telegram removida.' WHERE channel='telegram' AND status IN ('pending','processing','retry')")


def downgrade():
    connection = op.get_bind()
    for table in ("consultation_schedules", "export_artifacts", "portal_access_checks"):
        if connection.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("Downgrade requer preservar e remover os dados operacionais novos explicitamente.")
    for table in ("webhook_endpoints", "export_artifacts", "schedule_occurrences", "consultation_schedules", "job_requests", "portal_access_checks", "operational_blocks"):
        op.drop_table(table)
    op.execute("UPDATE automation_jobs SET status='paused' WHERE status='pausing'")
    op.execute("UPDATE automation_jobs SET status='cancelled' WHERE status='cancelling'")
    op.execute("UPDATE worker_heartbeats SET activity_status='stopped' WHERE activity_status='draining'")
    op.drop_constraint("ck_automation_jobs_status", "automation_jobs",type_="check")
    op.create_check_constraint("ck_automation_jobs_status", "automation_jobs", "status IN ('awaiting_dataset','queued','running','paused','completed','completed_with_errors','blocked','failed','cancelled')")
    op.drop_constraint("ck_worker_heartbeats_activity_status", "worker_heartbeats",type_="check")
    op.create_check_constraint("ck_worker_heartbeats_activity_status", "worker_heartbeats", "activity_status IN ('starting','idle','busy','backoff','stopped')")
    op.drop_constraint("uq_portal_credentials_identity","portal_credentials",type_="unique")
    for table,column in (("portal_credentials","login_identity"),("portal_credentials","login_failure_count"),("job_items","lease_token"),("job_items","retry_count"),("credential_leases","lease_token"),("automation_jobs","selected_credential_ids"),("automation_jobs","max_parallel_accounts"),("automation_jobs","result_version")):
        op.drop_column(table,column)
    op.execute("DELETE FROM credential_leases WHERE job_id IS NULL")
    op.alter_column("credential_leases","job_id",existing_type=sa.BigInteger(),nullable=False)
