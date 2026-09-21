"""Widen automation job status for completed_with_errors.

Revision ID: 20260921_0008
Revises: 20260829_0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260921_0008"
down_revision = "20260829_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "automation_jobs",
        "status",
        existing_type=sa.String(length=20),
        type_=sa.String(length=32),
        existing_nullable=False,
    )


def downgrade() -> None:
    # The legacy schema cannot represent this terminal state.
    op.execute(
        "UPDATE automation_jobs SET status = 'failed' "
        "WHERE status = 'completed_with_errors'"
    )
    op.alter_column(
        "automation_jobs",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
