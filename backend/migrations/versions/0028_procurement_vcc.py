"""v12 M3 (review-v12 §17; ADR-038): ``upstream_vcc`` delivery (virtual cards bought per order).

- ``products.delivery_mode`` / ``orders.delivery_mode`` accept ``upstream_vcc`` (variants inherit
  the product's mode; the order keeps its purchase-time snapshot).
- ``procurement_jobs.card_mask``: the MASKED card number (first 6 + last 4) of a purchased card,
  for the console job list. The full number, expiry and CVV exist only sealed in ``card_stock``.
- ``supplier_ledger.card_id``: the vendor card id of a card balance-log row (``open_card``,
  ``card_recharge`` ...), which links the row to its ``vcc`` job (``upstream_ref``). The row's own
  ``ref_id`` is the vendor's and is not a card id.

No new table: a card is sealed into ``card_stock`` as ``sold`` exactly like a CDK code (policy
``procurement_insert_sold`` of 0024), the job row is covered by the existing ``app_procurement`` /
``app_api`` grants and every table involved already has FORCE ROW LEVEL SECURITY. The one new
grant: ``app_api`` may UPDATE ``card_mask`` (a hand-fulfilled card job shows its masked number
like an automatic one).

Revision ID: 0028_procurement_vcc
Revises: 0027_procurement_audit_fixes
"""

from __future__ import annotations

from alembic import op

revision = "0028_procurement_vcc"
down_revision = "0027_procurement_audit_fixes"
branch_labels = None
depends_on = None

UP = [
    "ALTER TABLE products DROP CONSTRAINT IF EXISTS products_delivery_mode_check",
    "ALTER TABLE products ADD CONSTRAINT products_delivery_mode_check "
    "CHECK (delivery_mode IN ('auto_card', 'manual', 'upstream_cdk', 'upstream_vcc'))",
    "ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_delivery_mode_check",
    "ALTER TABLE orders ADD CONSTRAINT orders_delivery_mode_check "
    "CHECK (delivery_mode IN ('auto_card', 'manual', 'system', 'upstream_cdk', 'upstream_vcc'))",
    "ALTER TABLE procurement_jobs ADD COLUMN card_mask varchar(24)",
    "ALTER TABLE supplier_ledger ADD COLUMN card_id varchar(64)",
    "CREATE INDEX ix_supplier_ledger_card ON supplier_ledger (supplier_id, card_id) "
    "WHERE card_id IS NOT NULL",
    "GRANT UPDATE (card_mask) ON procurement_jobs TO app_api",
]

DOWN = [
    "REVOKE UPDATE (card_mask) ON procurement_jobs FROM app_api",
    "DROP INDEX IF EXISTS ix_supplier_ledger_card",
    "ALTER TABLE supplier_ledger DROP COLUMN card_id",
    "ALTER TABLE procurement_jobs DROP COLUMN card_mask",
    "ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_delivery_mode_check",
    "ALTER TABLE orders ADD CONSTRAINT orders_delivery_mode_check "
    "CHECK (delivery_mode IN ('auto_card', 'manual', 'system', 'upstream_cdk'))",
    "ALTER TABLE products DROP CONSTRAINT IF EXISTS products_delivery_mode_check",
    "ALTER TABLE products ADD CONSTRAINT products_delivery_mode_check "
    "CHECK (delivery_mode IN ('auto_card', 'manual', 'upstream_cdk'))",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    # FORCE RLS hides every row from the (unpinned) owner, so lift it for the check (0022 lesson).
    for t in ("products", "orders"):
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM products WHERE delivery_mode = 'upstream_vcc')
               OR EXISTS (SELECT 1 FROM orders WHERE delivery_mode = 'upstream_vcc') THEN
                RAISE EXCEPTION 'upstream_vcc products/orders exist: migrate them first';
            END IF;
        END $$"""
    )
    for t in ("products", "orders"):
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
    for stmt in DOWN:
        op.execute(stmt)
