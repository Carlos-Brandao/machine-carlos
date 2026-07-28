"""Require consignataria for newly created portal credentials.

Revision ID: 20260728_0003
Revises: 20260728_0002
"""

from alembic import op
import sqlalchemy as sa


revision = "20260728_0003"
down_revision = "20260728_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "portal_credentials",
        sa.Column("consignataria", sa.String(length=160), nullable=True),
    )
    op.execute(
        "UPDATE portal_credentials "
        "SET consignataria = NULLIF(settings_json->>'consignataria', '') "
        "WHERE consignataria IS NULL"
    )
    op.create_check_constraint(
        "ck_portal_credentials_consignataria_required",
        "portal_credentials",
        "consignataria IS NOT NULL AND btrim(consignataria) <> ''",
        postgresql_not_valid=True,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_portal_credentials_consignataria_required",
        "portal_credentials",
        type_="check",
    )
    op.drop_column("portal_credentials", "consignataria")
