"""Pre-orders (owner request 2026-11-19, plan U10).

``products.preorder`` (jsonb, NULL = not a pre-order): ``{"min_days", "max_days", "ends_at",
"note": {"en", "zh-CN"}}``, validated by ``app.modules.catalog.preorder.normalize``. Sold orders
keep their window in the existing ``orders.terms`` snapshot (``terms["preorder"]``), so no
orders column is needed. ``app_api`` already holds table-level SELECT/INSERT/UPDATE on
products (0001).

Revision ID: 0037_preorders
Revises: 0036_support_sessions
"""

from __future__ import annotations

from alembic import op

revision = "0037_preorders"
down_revision = "0036_support_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE products ADD COLUMN preorder jsonb")
    op.execute(
        "ALTER TABLE products ADD CONSTRAINT ck_products_preorder_object "
        "CHECK (preorder IS NULL OR jsonb_typeof(preorder) = 'object')"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE products DROP CONSTRAINT IF EXISTS ck_products_preorder_object")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS preorder")
