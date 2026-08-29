"""Persist the transactional SAFE contract for Fortaleza.

Revision ID: 20260829_0007
Revises: 20260818_0006
"""

from __future__ import annotations

from alembic import op


revision = "20260829_0007"
down_revision = "20260818_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # sync_catalog intentionally never overwrites an existing agreement. This
    # migration is therefore the explicit, reviewable transition from the
    # legacy SAFE runner to the transactional adapter. Operator states such as
    # paused/degraded/ready are preserved; only the legacy draft is promoted to
    # homologation.
    op.execute(
        """
        UPDATE municipalities
           SET platform_slug = 'safeconsig',
               login_url = 'https://fortaleza.safeconsig.com.br/safe/login',
               query_url = 'https://fortaleza.safeconsig.com.br/safe/pages/consulta/margem/',
               enabled = TRUE,
               operational_status = CASE
                 WHEN operational_status = 'draft' THEN 'testing'
                 ELSE operational_status
               END,
               input_schema = jsonb_build_object(
                 'version', 1,
                 'required', jsonb_build_array('cpf', 'registration'),
                 'optional', jsonb_build_array(),
                 'deduplication_key', jsonb_build_array('cpf', 'registration')
               ),
               adapter_version = 'safeconsig.v1',
               updated_at = now()
         WHERE slug = 'fortaleza'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE municipalities
           SET operational_status = CASE
                 WHEN operational_status = 'testing' THEN 'draft'
                 ELSE operational_status
               END,
               adapter_version = 'safeconsig.legacy',
               updated_at = now()
         WHERE slug = 'fortaleza'
           AND adapter_version = 'safeconsig.v1'
        """
    )
