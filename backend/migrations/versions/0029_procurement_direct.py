"""v12 M3 part 2 (review-v12 §18; ADR-038): ``upstream_direct`` (the supplier subscribes the
BUYER's own account with the account session the buyer supplied at checkout).

- ``products.delivery_mode`` / ``orders.delivery_mode`` accept ``upstream_direct``.
- ``procurement_credentials`` (NEW, tenant-scoped, ENABLE + FORCE RLS, ``tenant_isolation``): the
  buyer's account session, sealed at checkout with the PROCUREMENT public key (a keypair of its
  own, not the card vault's). One row per order (UNIQUE ``order_id``), size-bounded. Grants:

  ================  =====================================================================
  ``app_api``       INSERT, and SELECT of the NON-secret columns only (``id``, ``tenant_id``,
                    ``order_id``, ``product``, ``email_masked``, ``created_at``). It can never
                    read ``blob`` or ``kid`` back (column privilege: a real attempt fails).
  ``app_procurement`` SELECT and DELETE (the engine opens the blob in memory at the supplier
                    call; the sweeper deletes it at a terminal state / after its maximum age).
                    No INSERT and no UPDATE: a credential is written once and then only removed.
  everyone else     nothing (``app_delivery`` has no grant at all).
  ================  =====================================================================

  Scan / sweep policies let ``app_procurement`` list and delete credentials across tenants ONLY
  from a session that is not pinned to a tenant (the sweeper); a pinned session (the engine's call
  path) sees just its own tenant's rows, like every other tenant table.
- ``procurement_jobs.account_mask``: the MASKED account e-mail the supplier's preflight reported
  (``u***@example.com``), for the console and the buyer's delivery text. Never the session.

Revision ID: 0029_procurement_direct
Revises: 0028_procurement_vcc
"""

from __future__ import annotations

from alembic import op

revision = "0029_procurement_direct"
down_revision = "0028_procurement_vcc"
branch_labels = None
depends_on = None

TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"

# the scan / sweep policies apply to UNPINNED sessions only (the sweeper): a session pinned to a
# tenant sees that tenant's rows and nothing else, like every other table
UNPINNED = "NULLIF(current_setting('app.tenant_id', true), '') IS NULL"

UP = [
    "ALTER TABLE products DROP CONSTRAINT IF EXISTS products_delivery_mode_check",
    "ALTER TABLE products ADD CONSTRAINT products_delivery_mode_check CHECK (delivery_mode IN "
    "('auto_card', 'manual', 'upstream_cdk', 'upstream_vcc', 'upstream_direct'))",
    "ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_delivery_mode_check",
    "ALTER TABLE orders ADD CONSTRAINT orders_delivery_mode_check CHECK (delivery_mode IN "
    "('auto_card', 'manual', 'system', 'upstream_cdk', 'upstream_vcc', 'upstream_direct'))",
    "ALTER TABLE procurement_jobs ADD COLUMN account_mask varchar(64)",
    """CREATE TABLE procurement_credentials (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        order_id uuid NOT NULL REFERENCES orders(id),
        product varchar(8) NOT NULL CHECK (product IN ('gpt', 'claude', 'grok')),
        kid varchar(64) NOT NULL,
        blob bytea NOT NULL CHECK (octet_length(blob) BETWEEN 64 AND 140000),
        email_masked varchar(64),
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_procurement_credentials_order UNIQUE (order_id)
    )""",
    "CREATE INDEX ix_procurement_credentials_age ON procurement_credentials (created_at)",
    "ALTER TABLE procurement_credentials ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE procurement_credentials FORCE ROW LEVEL SECURITY",
    f"CREATE POLICY tenant_isolation ON procurement_credentials USING ({TENANT_PREDICATE}) "
    f"WITH CHECK ({TENANT_PREDICATE})",
    f"CREATE POLICY procurement_scan ON procurement_credentials FOR SELECT TO app_procurement "
    f"USING ({UNPINNED})",
    f"CREATE POLICY procurement_sweep ON procurement_credentials FOR DELETE TO app_procurement "
    f"USING ({UNPINNED})",
    "REVOKE ALL ON procurement_credentials FROM PUBLIC",
    "GRANT INSERT ON procurement_credentials TO app_api",
    "GRANT SELECT (id, tenant_id, order_id, product, email_masked, created_at) "
    "ON procurement_credentials TO app_api",
    "GRANT SELECT, DELETE ON procurement_credentials TO app_procurement",
]

DOWN = [
    "DROP TABLE IF EXISTS procurement_credentials",
    "ALTER TABLE procurement_jobs DROP COLUMN account_mask",
    "ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_delivery_mode_check",
    "ALTER TABLE orders ADD CONSTRAINT orders_delivery_mode_check CHECK (delivery_mode IN "
    "('auto_card', 'manual', 'system', 'upstream_cdk', 'upstream_vcc'))",
    "ALTER TABLE products DROP CONSTRAINT IF EXISTS products_delivery_mode_check",
    "ALTER TABLE products ADD CONSTRAINT products_delivery_mode_check CHECK (delivery_mode IN "
    "('auto_card', 'manual', 'upstream_cdk', 'upstream_vcc'))",
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
            IF EXISTS (SELECT 1 FROM products WHERE delivery_mode = 'upstream_direct')
               OR EXISTS (SELECT 1 FROM orders WHERE delivery_mode = 'upstream_direct') THEN
                RAISE EXCEPTION 'upstream_direct products/orders exist: migrate them first';
            END IF;
        END $$"""
    )
    for t in ("products", "orders"):
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
    for stmt in DOWN:
        op.execute(stmt)
