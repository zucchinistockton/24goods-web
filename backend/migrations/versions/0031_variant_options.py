"""Plan x region variants (issue #27, review-v12 §20): ``product_variants.options``.

A variant may carry structured option attributes ``{"plan": "<vendor plan key>", "region": "<ISO
country>"}`` (NULL = a plain variant). The buyer UI shows two selectors (plan, then region) when
every active variant of a product has options. No new table: each combination is an ordinary
variant with its own price, floor, stock and supplier sources. ``app_api`` already holds table
level grants on ``product_variants``; RLS (FORCE) is unchanged. A partial unique index keeps one
variant per ``(product, plan, region)``.

Revision ID: 0031_variant_options
Revises: 0030_cred_delete_backstop
"""

from __future__ import annotations

from alembic import op

revision = "0031_variant_options"
down_revision = "0030_cred_delete_backstop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE product_variants ADD COLUMN options jsonb")
    # a generated variant starts with a placeholder price: false until staff set one explicitly
    op.execute("ALTER TABLE product_variants ADD COLUMN price_set boolean NOT NULL DEFAULT true")
    op.execute(
        "ALTER TABLE product_variants ADD CONSTRAINT product_variants_options_shape CHECK "
        "(options IS NULL OR (jsonb_typeof(options) = 'object' "
        "AND options ? 'plan' AND options ? 'region'))"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_product_variants_options ON product_variants "
        "(tenant_id, product_id, (options->>'plan'), (options->>'region')) "
        "WHERE options IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_product_variants_options")
    op.execute(
        "ALTER TABLE product_variants DROP CONSTRAINT IF EXISTS product_variants_options_shape"
    )
    op.execute("ALTER TABLE product_variants DROP COLUMN IF EXISTS options")
    op.execute("ALTER TABLE product_variants DROP COLUMN IF EXISTS price_set")
