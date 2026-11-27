"""SMS verification codes (ADR-057): delivery mode ``upstream_sms``, adapter ``herosms``.

* ``products`` / ``orders`` delivery mode accepts ``upstream_sms``; ``suppliers.adapter`` accepts
  ``herosms``.
* ``sms_products``: one SMS configuration per product (routes, limits; admin-edited through the
  API) plus the availability procurement last computed.
* ``sms_sessions``: one per paid SMS order (procurement only writes; API and delivery read).
* ``sms_attempts``: one per rented number. There is NO code column: a code is sealed into
  ``card_stock`` the moment it arrives and only delivery can open it.

Revision ID: 0044_sms_sessions
Revises: 0043_gateway_deposit
"""

from __future__ import annotations

from alembic import op

revision = "0044_sms_sessions"
down_revision = "0043_gateway_deposit"
branch_labels = None
depends_on = None

PRED = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
UNPINNED = "NULLIF(current_setting('app.tenant_id', true), '') IS NULL"

MODES_P = (
    "('auto_card', 'manual', 'upstream_cdk', 'upstream_vcc', 'upstream_direct', 'upstream_sms')"
)
MODES_O = (
    "('auto_card', 'manual', 'system', 'upstream_cdk', 'upstream_vcc', 'upstream_direct', "
    "'upstream_sms')"
)
OLD_P = "('auto_card', 'manual', 'upstream_cdk', 'upstream_vcc', 'upstream_direct')"
OLD_O = "('auto_card', 'manual', 'system', 'upstream_cdk', 'upstream_vcc', 'upstream_direct')"
ADAPTERS = "('zovocard', 'zzshu', 'vmcardio', 'herosms', 'mock', 'manual')"
OLD_ADAPTERS = "('zovocard', 'zzshu', 'vmcardio', 'mock', 'manual')"


def _rls(t: str) -> list[str]:
    return [
        f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_isolation ON {t} USING ({PRED}) WITH CHECK ({PRED})",
        # the SMS loop finds open sessions / configured products across tenants (read only);
        # every write pins the tenant
        f"CREATE POLICY procurement_scan ON {t} FOR SELECT TO app_procurement USING ({UNPINNED})",
        f"REVOKE ALL ON {t} FROM PUBLIC",
    ]


UP = [
    "ALTER TABLE products DROP CONSTRAINT IF EXISTS products_delivery_mode_check",
    "ALTER TABLE products ADD CONSTRAINT products_delivery_mode_check "
    f"CHECK (delivery_mode IN {MODES_P})",
    "ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_delivery_mode_check",
    "ALTER TABLE orders ADD CONSTRAINT orders_delivery_mode_check "
    f"CHECK (delivery_mode IN {MODES_O})",
    "ALTER TABLE suppliers DROP CONSTRAINT IF EXISTS suppliers_adapter_check",
    f"ALTER TABLE suppliers ADD CONSTRAINT suppliers_adapter_check CHECK (adapter IN {ADAPTERS})",
    """CREATE TABLE sms_products (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        product_id uuid NOT NULL REFERENCES products(id),
        supplier_id uuid,
        enabled boolean NOT NULL DEFAULT false,
        params jsonb NOT NULL DEFAULT '{}',
        last_check_ok boolean,
        last_check_at timestamptz,
        last_check_reason varchar(64),
        stock_hint integer NOT NULL DEFAULT 0 CHECK (stock_hint >= 0 AND stock_hint <= 999),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_sms_products_product UNIQUE (product_id),
        FOREIGN KEY (tenant_id, supplier_id) REFERENCES suppliers(tenant_id, id))""",
    "CREATE INDEX ix_sms_products_tenant ON sms_products (tenant_id)",
    *_rls("sms_products"),
    "GRANT SELECT, INSERT ON sms_products TO app_api",
    "GRANT UPDATE (supplier_id, enabled, params, updated_at) ON sms_products TO app_api",
    "GRANT SELECT ON sms_products TO app_procurement",
    "GRANT UPDATE (last_check_ok, last_check_at, last_check_reason, stock_hint, updated_at) "
    "ON sms_products TO app_procurement",
    """CREATE TABLE sms_sessions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        order_id uuid NOT NULL REFERENCES orders(id),
        product_id uuid NOT NULL REFERENCES products(id),
        supplier_id uuid,
        state varchar(16) NOT NULL DEFAULT 'ready'
            CHECK (state IN ('ready', 'active', 'delivered', 'failed')),
        attempts_used integer NOT NULL DEFAULT 0 CHECK (attempts_used >= 0 AND attempts_used <= 20),
        params jsonb NOT NULL DEFAULT '{}',
        start_deadline timestamptz NOT NULL,
        started_at timestamptz,
        window_ends_at timestamptz,
        finished_at timestamptz,
        fail_reason varchar(32),
        version integer NOT NULL DEFAULT 1,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_sms_sessions_order UNIQUE (order_id),
        CONSTRAINT uq_sms_sessions_tenant_id UNIQUE (tenant_id, id),
        FOREIGN KEY (tenant_id, supplier_id) REFERENCES suppliers(tenant_id, id))""",
    "CREATE INDEX ix_sms_sessions_open ON sms_sessions (state) WHERE state IN ('ready', 'active')",
    "CREATE INDEX ix_sms_sessions_day ON sms_sessions (tenant_id, product_id, started_at)",
    *_rls("sms_sessions"),
    "GRANT SELECT ON sms_sessions TO app_api, app_delivery",
    "GRANT SELECT, INSERT, UPDATE ON sms_sessions TO app_procurement",
    """CREATE TABLE sms_attempts (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        session_id uuid NOT NULL,
        order_id uuid NOT NULL REFERENCES orders(id),
        seq integer NOT NULL CHECK (seq >= 1 AND seq <= 20),
        supplier_id uuid NOT NULL,
        upstream_id varchar(64) NOT NULL,
        country integer NOT NULL,
        operator varchar(48) NOT NULL DEFAULT '',
        phone varchar(32) NOT NULL,
        cost numeric(20, 6),
        currency varchar(3) NOT NULL DEFAULT 'USD',
        state varchar(16) NOT NULL DEFAULT 'waiting'
            CHECK (state IN ('waiting', 'code', 'no_code', 'cancelled', 'failed')),
        bought_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz,
        code_at timestamptz,
        closed_at timestamptz,
        upstream_closed boolean NOT NULL DEFAULT false,
        error_code varchar(64),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_sms_attempts_upstream UNIQUE (supplier_id, upstream_id),
        CONSTRAINT uq_sms_attempts_seq UNIQUE (session_id, seq),
        FOREIGN KEY (tenant_id, session_id) REFERENCES sms_sessions(tenant_id, id),
        FOREIGN KEY (tenant_id, supplier_id) REFERENCES suppliers(tenant_id, id))""",
    "CREATE INDEX ix_sms_attempts_waiting ON sms_attempts (state) WHERE state = 'waiting'",
    "CREATE INDEX ix_sms_attempts_route ON sms_attempts (tenant_id, country, bought_at)",
    *_rls("sms_attempts"),
    "GRANT SELECT ON sms_attempts TO app_api",
    "GRANT SELECT, INSERT, UPDATE ON sms_attempts TO app_procurement",
]

DOWN = [
    "DROP TABLE IF EXISTS sms_attempts",
    "DROP TABLE IF EXISTS sms_sessions",
    "DROP TABLE IF EXISTS sms_products",
    "ALTER TABLE suppliers DROP CONSTRAINT IF EXISTS suppliers_adapter_check",
    "ALTER TABLE suppliers ADD CONSTRAINT suppliers_adapter_check "
    f"CHECK (adapter IN {OLD_ADAPTERS})",
    "ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_delivery_mode_check",
    "ALTER TABLE orders ADD CONSTRAINT orders_delivery_mode_check "
    f"CHECK (delivery_mode IN {OLD_O})",
    "ALTER TABLE products DROP CONSTRAINT IF EXISTS products_delivery_mode_check",
    "ALTER TABLE products ADD CONSTRAINT products_delivery_mode_check "
    f"CHECK (delivery_mode IN {OLD_P})",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWN:
        op.execute(stmt)
