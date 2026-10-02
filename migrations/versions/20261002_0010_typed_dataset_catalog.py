"""Add typed reusable bases without reclassifying or merging legacy catalogs.

Existing bases retain their records, status, name, schedules and historical jobs.
An operator can classify them explicitly later. New uploads use typed catalogs.
"""

from alembic import op
import sqlalchemy as sa

revision = "20261002_0010"
down_revision = "20260930_0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("datasets", sa.Column("dataset_type", sa.String(20), nullable=True))
    op.create_check_constraint(
        "ck_datasets_type", "datasets",
        "dataset_type IN ('efetivos','temporarios','comissionados','geral')",
    )
    op.create_table(
        "dataset_memberships",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("dataset_id", sa.BigInteger(), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dataset_record_id", sa.BigInteger(), sa.ForeignKey("dataset_records.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("identity_key", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("dataset_id", "identity_key", name="uq_dataset_memberships_identity"),
        sa.UniqueConstraint("dataset_id", "dataset_record_id", name="uq_dataset_memberships_record"),
    )
    op.create_index("ix_dataset_memberships_dataset_id", "dataset_memberships", ["dataset_id"])
    op.create_index("ix_dataset_memberships_dataset_record_id", "dataset_memberships", ["dataset_record_id"])
    # Legacy keep_all imports intentionally retain every row. Classification
    # replaces these record keys with CPF[/matrícula] keys only when requested.
    op.execute("""
        INSERT INTO dataset_memberships (dataset_id, dataset_record_id, identity_key)
        SELECT dataset_id, id, 'legacy-record:' || id::text
        FROM dataset_records ORDER BY dataset_id, id
    """)
    # PostgreSQL permits multiple NULL types for the unchanged legacy bases.
    op.create_index(
        "uq_datasets_active_type", "datasets", ["municipality_slug", "dataset_type"],
        unique=True, postgresql_where=sa.text("status <> 'archived'"),
    )


def downgrade():
    connection = op.get_bind()
    typed_count = connection.scalar(sa.text("SELECT count(*) FROM datasets WHERE dataset_type IS NOT NULL"))
    if typed_count:
        raise RuntimeError(
            "Há bases classificadas ou complementadas após a migração. "
            "Exporte/restaure esse catálogo antes de remover os vínculos por tipo."
        )
    op.drop_index("uq_datasets_active_type", table_name="datasets")
    op.drop_table("dataset_memberships")
    op.drop_constraint("ck_datasets_type", "datasets", type_="check")
    op.drop_column("datasets", "dataset_type")
