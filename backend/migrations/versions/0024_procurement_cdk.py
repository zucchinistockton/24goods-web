"""v12 M2 (review-v12 §2, §4, §6; ADR-038): ``upstream_cdk`` delivery and the narrow write grants.

- ``products.delivery_mode`` / ``orders.delivery_mode`` accept ``upstream_cdk`` (the order keeps
  its purchase-time snapshot as today). Variants inherit the product's mode (ADR-037).
- ``procurement_routing`` (``priority`` | ``round_robin``) on products (variant-less sales) and
  on product_variants: how several offers of the same target are ordered.
- ``suppliers``: breaker columns (``breaker_open``, ``breaker_reason``, ``breaker_at``).
- ``procurement_jobs``: ``tried_offer_ids`` (failover never revisits an offer),
  ``upstream_status`` (last known vendor status, e.g. CDK unused/consumed), ``alerted_at``.
- ``app_procurement``:
  * INSERT on ``card_stock``, restricted by a RESTRICTIVE policy to rows that are already
    ``status = 'sold'`` with an ``order_id`` (procured secrets never enter the local pool);
    still no SELECT on card_stock at all.
  * outbox: consumes ``procure.requested`` / ``supplier.event`` (SELECT across tenants for those
    topics only, UPDATE of the bookkeeping columns), INSERTs only ``procure.fulfilled`` /
    ``procure.failed`` (RESTRICTIVE policies).
  * SELECT across tenants on ``procurement_jobs`` (the poller), then pins the tenant to write.
- ``app_delivery``: INSERT on outbox restricted to ``procure.requested``.
- ``app_api``: INSERT on ``supplier_events`` (webhook), CRUD on ``supplier_offers`` (admin),
  UPDATE of ``procurement_routing`` (table grants on products/product_variants already cover it).

Revision ID: 0024_procurement_cdk
Revises: 0023_procurement
"""

from __future__ import annotations

from alembic import op

revision = "0024_procurement_cdk"
down_revision = "0023_procurement"
branch_labels = None
depends_on = None

PROC_CONSUMES = "('procure.requested', 'supplier.event')"
PROC_EMITS = "('procure.fulfilled', 'procure.failed')"
PROC_TOPICS = "('procure.requested', 'supplier.event', 'procure.fulfilled', 'procure.failed')"

UP = [
    "ALTER TABLE products DROP CONSTRAINT IF EXISTS products_delivery_mode_check",
    "ALTER TABLE products ADD CONSTRAINT products_delivery_mode_check "
    "CHECK (delivery_mode IN ('auto_card', 'manual', 'upstream_cdk'))",
    "ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_delivery_mode_check",
    "ALTER TABLE orders ADD CONSTRAINT orders_delivery_mode_check "
    "CHECK (delivery_mode IN ('auto_card', 'manual', 'system', 'upstream_cdk'))",
    "ALTER TABLE products ADD COLUMN procurement_routing varchar(16) NOT NULL DEFAULT 'priority' "
    "CHECK (procurement_routing IN ('priority', 'round_robin'))",
    "ALTER TABLE product_variants ADD COLUMN procurement_routing varchar(16) NOT NULL "
    "DEFAULT 'priority' CHECK (procurement_routing IN ('priority', 'round_robin'))",
    """ALTER TABLE suppliers ADD COLUMN breaker_open boolean NOT NULL DEFAULT false,
        ADD COLUMN breaker_reason varchar(64), ADD COLUMN breaker_at timestamptz""",
    """ALTER TABLE procurement_jobs ADD COLUMN tried_offer_ids uuid[] NOT NULL DEFAULT '{}',
        ADD COLUMN upstream_status varchar(32), ADD COLUMN alerted_at timestamptz""",
    "CREATE INDEX ix_procurement_jobs_order ON procurement_jobs (tenant_id, order_id)",
    "CREATE INDEX ix_procurement_jobs_upstream ON procurement_jobs (supplier_id, upstream_ref)",
    # ---- app_procurement: sealed, already-sold card rows only
    "GRANT INSERT ON card_stock TO app_procurement",
    "CREATE POLICY procurement_insert_sold ON card_stock AS RESTRICTIVE FOR INSERT "
    "TO app_procurement WITH CHECK (status = 'sold' AND order_id IS NOT NULL)",
    # ---- app_procurement: outbox consumer + emitter of two topics
    "GRANT SELECT, INSERT ON outbox TO app_procurement",
    "GRANT UPDATE (processed_at, attempts, last_error, available_at) ON outbox TO app_procurement",
    "GRANT USAGE ON SEQUENCE outbox_id_seq TO app_procurement",
    f"CREATE POLICY procurement_drain ON outbox FOR SELECT TO app_procurement "
    f"USING (topic IN {PROC_CONSUMES})",
    f"CREATE POLICY procurement_topics ON outbox AS RESTRICTIVE FOR ALL TO app_procurement "
    f"USING (topic IN {PROC_TOPICS}) WITH CHECK (topic IN {PROC_TOPICS})",
    f"CREATE POLICY procurement_emits ON outbox AS RESTRICTIVE FOR INSERT TO app_procurement "
    f"WITH CHECK (topic IN {PROC_EMITS})",
    "CREATE POLICY procurement_scan ON procurement_jobs FOR SELECT TO app_procurement USING (true)",
    "GRANT SELECT (procurement_routing) ON products, product_variants TO app_procurement",
    # ---- app_delivery: may hand an order to procurement, nothing else
    "GRANT INSERT ON outbox TO app_delivery",
    "GRANT USAGE ON SEQUENCE outbox_id_seq TO app_delivery",
    "CREATE POLICY delivery_emits ON outbox AS RESTRICTIVE FOR INSERT TO app_delivery "
    "WITH CHECK (topic = 'procure.requested')",
    # ---- app_api
    "GRANT INSERT ON supplier_events TO app_api",
    "GRANT INSERT, UPDATE, DELETE ON supplier_offers TO app_api",
]

DOWN = [
    "REVOKE INSERT, UPDATE, DELETE ON supplier_offers FROM app_api",
    "REVOKE INSERT ON supplier_events FROM app_api",
    "DROP POLICY IF EXISTS delivery_emits ON outbox",
    "REVOKE INSERT ON outbox FROM app_delivery",
    "DROP POLICY IF EXISTS procurement_scan ON procurement_jobs",
    "DROP POLICY IF EXISTS procurement_emits ON outbox",
    "DROP POLICY IF EXISTS procurement_topics ON outbox",
    "DROP POLICY IF EXISTS procurement_drain ON outbox",
    "REVOKE ALL ON outbox FROM app_procurement",
    "REVOKE ALL ON SEQUENCE outbox_id_seq FROM app_procurement",
    "DROP POLICY IF EXISTS procurement_insert_sold ON card_stock",
    "REVOKE ALL ON card_stock FROM app_procurement",
    "REVOKE SELECT (procurement_routing) ON products, product_variants FROM app_procurement",
    "DROP INDEX IF EXISTS ix_procurement_jobs_upstream",
    "DROP INDEX IF EXISTS ix_procurement_jobs_order",
    "ALTER TABLE procurement_jobs DROP COLUMN tried_offer_ids, DROP COLUMN upstream_status, "
    "DROP COLUMN alerted_at",
    "ALTER TABLE suppliers DROP COLUMN breaker_open, DROP COLUMN breaker_reason, "
    "DROP COLUMN breaker_at",
    "ALTER TABLE product_variants DROP COLUMN procurement_routing",
    "ALTER TABLE products DROP COLUMN procurement_routing",
    "ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_delivery_mode_check",
    "ALTER TABLE orders ADD CONSTRAINT orders_delivery_mode_check "
    "CHECK (delivery_mode IN ('auto_card', 'manual', 'system'))",
    "ALTER TABLE products DROP CONSTRAINT IF EXISTS products_delivery_mode_check",
    "ALTER TABLE products ADD CONSTRAINT products_delivery_mode_check "
    "CHECK (delivery_mode IN ('auto_card', 'manual'))",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    # A downgrade with upstream rows left would violate the restored CHECKs: refuse loudly.
    # FORCE RLS hides every row from the (unpinned) owner, so lift it for the check (0022 lesson).
    for t in ("products", "orders"):
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM products WHERE delivery_mode = 'upstream_cdk')
               OR EXISTS (SELECT 1 FROM orders WHERE delivery_mode = 'upstream_cdk') THEN
                RAISE EXCEPTION 'upstream_cdk products/orders exist: migrate them first';
            END IF;
        END $$"""
    )
    for t in ("products", "orders"):
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
    for stmt in DOWN:
        op.execute(stmt)
