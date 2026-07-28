"""Store portal credentials in clear text at the operator's request.

Revision ID: 20260728_0005
Revises: 20260728_0004
"""

import sqlalchemy as sa
from alembic import op


revision = "20260728_0005"
down_revision = "20260728_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("portal_credentials", sa.Column("portal_username", sa.Text(), nullable=True))
    op.add_column("portal_credentials", sa.Column("portal_password", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("portal_credentials", "portal_password")
    op.drop_column("portal_credentials", "portal_username")
