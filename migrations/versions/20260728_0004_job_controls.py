"""Add paused status for operator-controlled jobs.

Revision ID: 20260728_0004
Revises: 20260728_0003
"""

from alembic import op


revision = "20260728_0004"
down_revision = "20260728_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_automation_jobs_status", "automation_jobs", type_="check")
    op.create_check_constraint(
        "ck_automation_jobs_status",
        "automation_jobs",
        "status IN ('awaiting_dataset', 'queued', 'running', 'paused', 'completed', 'failed', 'cancelled')",
    )


def downgrade() -> None:
    op.execute("UPDATE automation_jobs SET status = 'queued' WHERE status = 'paused'")
    op.drop_constraint("ck_automation_jobs_status", "automation_jobs", type_="check")
    op.create_check_constraint(
        "ck_automation_jobs_status",
        "automation_jobs",
        "status IN ('awaiting_dataset', 'queued', 'running', 'completed', 'failed', 'cancelled')",
    )
