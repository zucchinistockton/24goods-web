"""v12 procurement (review-v12 §2, §4; ADR-038): supplier tables and the app_procurement role.

- ``suppliers``, ``supplier_offers``, ``procurement_jobs``, ``supplier_events``,
  ``supplier_ledger``: tenant_id + ENABLE/FORCE RLS + ``tenant_isolation``.
  Unique: suppliers (tenant_id, code, env); procurement_jobs.idempotency_key (vendor format
  checked) and (tenant_id, order_id, unit_no) while not voided; supplier_events
  (supplier_id, dedup_key); supplier_ledger (supplier_id, external_id).
- Role ``app_procurement`` (LOGIN with ``APP_DB_PROCUREMENT_PASSWORD`` when set, >= 12 chars;
  otherwise created NOLOGIN so existing deployments migrate unchanged; the procurement
  service is opt-in). Grants: full on the five tables; INSERT on audit_log (+ SELECT of its
  id); SELECT on tenants and on the needed columns of orders, products, product_variants.
  No UPDATE on orders, nothing on deliveries, nothing on card_stock or outbox yet (M2's
  migration grants INSERT on those when the job engine first writes them).
  A SELECT-only policy lets it find enabled suppliers across tenants (it then pins the tenant
  for every write, like the delivery worker).
- app_api: SELECT/INSERT on suppliers and UPDATE of the admin-editable columns only; SELECT
  on the other four.
- Supplier codes: never end in ``_sandbox`` (the env picks the credential set), and a
  credential adapter's code is the adapter name or ``<adapter>_<suffix>``.

No new ``delivery_mode`` values yet (M2). The role is left in place on downgrade (roles are
cluster-wide, like app_api/app_delivery in 0001); its grants and policies are removed.

Revision ID: 0023_procurement
Revises: 0022a_user_totp
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "0023_procurement"
down_revision = "0022a_user_totp"
branch_labels = None
depends_on = None

TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
TABLES = ("suppliers", "supplier_offers", "procurement_jobs", "supplier_events", "supplier_ledger")

ROLE_SQL = """
DO $$
DECLARE
    pw text := current_setting('g24.pw_app_procurement', true);
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_procurement') THEN
        IF coalesce(length(pw), 0) >= 12 THEN
            EXECUTE format(
                'CREATE ROLE app_procurement LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                'NOBYPASSRLS NOINHERIT PASSWORD %L', pw);
        ELSE
            CREATE ROLE app_procurement NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                NOBYPASSRLS NOINHERIT;
        END IF;
    ELSIF coalesce(length(pw), 0) >= 12 THEN
        BEGIN
            EXECUTE format('ALTER ROLE app_procurement LOGIN PASSWORD %L', pw);
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'role app_procurement exists and is not manageable by this owner';
        END;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO app_procurement', current_database());
END $$
"""

DDL = [
    """CREATE TABLE suppliers (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        code varchar(32) NOT NULL CHECK (code ~ '^[a-z][a-z0-9_]{1,31}$'
            AND code !~ '_sandbox$'),
        adapter varchar(16) NOT NULL CHECK (adapter IN ('zovocard', 'mock', 'manual')),
        CONSTRAINT ck_suppliers_code_adapter CHECK (adapter IN ('mock', 'manual')
            OR code = adapter OR left(code, length(adapter) + 1) = adapter || '_'),
        label varchar(80) NOT NULL,
        enabled boolean NOT NULL DEFAULT false,
        env varchar(16) NOT NULL DEFAULT 'sandbox' CHECK (env IN ('sandbox', 'production')),
        priority integer NOT NULL DEFAULT 100 CHECK (priority BETWEEN 0 AND 10000),
        settings jsonb NOT NULL DEFAULT '{}',
        min_spendable numeric(20, 6) NOT NULL DEFAULT 0 CHECK (min_spendable >= 0),
        currency varchar(3) NOT NULL DEFAULT 'USD',
        health varchar(16) NOT NULL DEFAULT 'unknown'
            CHECK (health IN ('unknown', 'ok', 'degraded', 'down')),
        health_detail varchar(64),
        last_probe_at timestamptz,
        last_balance numeric(20, 6),
        last_balance_at timestamptz,
        created_by uuid REFERENCES users(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_suppliers_tenant_code_env UNIQUE (tenant_id, code, env),
        CONSTRAINT uq_suppliers_tenant_id UNIQUE (tenant_id, id))""",
    """CREATE TABLE supplier_offers (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        supplier_id uuid NOT NULL,
        product_id uuid NOT NULL REFERENCES products(id),
        variant_id uuid REFERENCES product_variants(id),
        kind varchar(16) NOT NULL CHECK (kind IN ('cdk', 'vcc', 'direct')),
        params jsonb NOT NULL DEFAULT '{}',
        cost_estimate numeric(20, 6),
        currency varchar(3) NOT NULL DEFAULT 'USD',
        priority integer NOT NULL DEFAULT 100,
        enabled boolean NOT NULL DEFAULT false,
        last_check_ok boolean,
        last_check_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_supplier_offers_tenant_id UNIQUE (tenant_id, id),
        FOREIGN KEY (tenant_id, supplier_id) REFERENCES suppliers(tenant_id, id))""",
    """CREATE INDEX ix_supplier_offers_target ON supplier_offers
        (tenant_id, product_id, variant_id, enabled, priority)""",
    """CREATE TABLE procurement_jobs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        order_id uuid NOT NULL REFERENCES orders(id),
        unit_no integer NOT NULL CHECK (unit_no >= 1),
        offer_id uuid,
        supplier_id uuid,
        kind varchar(16) NOT NULL CHECK (kind IN ('cdk', 'vcc', 'direct')),
        state varchar(24) NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'submitting',
            'accepted_pending', 'fulfilled', 'failed', 'needs_manual', 'void_pending',
            'voided')),
        step varchar(16) NOT NULL DEFAULT 'submit',
        idempotency_key varchar(80) NOT NULL
            CHECK (idempotency_key ~ '^[A-Za-z0-9._:-]{1,80}$'),
        client_request_id varchar(80),
        request_hash varchar(64),
        upstream_ref varchar(128),
        error_code varchar(64),
        attempts integer NOT NULL DEFAULT 0,
        next_poll_at timestamptz,
        preflight_expires_at timestamptz,
        cost numeric(20, 6),
        currency varchar(3),
        card_stock_ids uuid[] NOT NULL DEFAULT '{}',
        manual_by uuid REFERENCES users(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_procurement_jobs_idempotency_key UNIQUE (idempotency_key),
        CONSTRAINT uq_procurement_jobs_tenant_id UNIQUE (tenant_id, id),
        FOREIGN KEY (tenant_id, supplier_id) REFERENCES suppliers(tenant_id, id),
        FOREIGN KEY (tenant_id, offer_id) REFERENCES supplier_offers(tenant_id, id))""",
    """CREATE UNIQUE INDEX uq_procurement_jobs_unit ON procurement_jobs
        (tenant_id, order_id, unit_no) WHERE state <> 'voided'""",
    """CREATE INDEX ix_procurement_jobs_poll ON procurement_jobs (state, next_poll_at)
        WHERE state IN ('queued', 'submitting', 'accepted_pending', 'void_pending')""",
    """CREATE TABLE supplier_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        supplier_id uuid NOT NULL,
        dedup_key varchar(200) NOT NULL,
        kind varchar(48) NOT NULL,
        payload jsonb NOT NULL DEFAULT '{}',
        received_at timestamptz NOT NULL DEFAULT now(),
        processed_at timestamptz,
        CONSTRAINT uq_supplier_events_dedup UNIQUE (supplier_id, dedup_key),
        FOREIGN KEY (tenant_id, supplier_id) REFERENCES suppliers(tenant_id, id))""",
    """CREATE INDEX ix_supplier_events_unprocessed ON supplier_events (received_at)
        WHERE processed_at IS NULL""",
    """CREATE TABLE supplier_ledger (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        supplier_id uuid NOT NULL,
        external_id varchar(64) NOT NULL,
        type varchar(48) NOT NULL,
        amount numeric(20, 6) NOT NULL,
        currency varchar(3) NOT NULL,
        balance_after numeric(20, 6),
        job_id uuid,
        occurred_at timestamptz NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_supplier_ledger_external UNIQUE (supplier_id, external_id),
        FOREIGN KEY (tenant_id, supplier_id) REFERENCES suppliers(tenant_id, id),
        FOREIGN KEY (tenant_id, job_id) REFERENCES procurement_jobs(tenant_id, id))""",
    """CREATE INDEX ix_supplier_ledger_tenant_time ON supplier_ledger
        (tenant_id, supplier_id, occurred_at DESC)""",
]

GRANTS = [
    "GRANT USAGE ON SCHEMA public TO app_procurement",
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} TO app_procurement",
    "GRANT INSERT ON audit_log TO app_procurement",
    "GRANT USAGE ON SEQUENCE audit_log_id_seq TO app_procurement",
    # SQLAlchemy inserts return the identity id: that column only, never the rows
    "GRANT SELECT (id) ON audit_log TO app_procurement",
    "GRANT SELECT (id, slug, kind, status) ON tenants TO app_procurement",
    "GRANT SELECT (id, tenant_id, order_no, product_id, variant_id, qty, status, "
    "delivery_mode, currency, paid_at, created_at) ON orders TO app_procurement",
    "GRANT SELECT (id, tenant_id, delivery_mode, status, currency) ON products TO app_procurement",
    "GRANT SELECT (id, tenant_id, product_id, status) ON product_variants TO app_procurement",
    "GRANT SELECT, INSERT ON suppliers TO app_api",
    "GRANT UPDATE (label, env, priority, min_spendable, enabled, health, health_detail, "
    "updated_at) ON suppliers TO app_api",
    "GRANT SELECT ON supplier_offers, procurement_jobs, supplier_events, supplier_ledger "
    "TO app_api",
]


def upgrade() -> None:
    bind = op.get_bind()
    pw = os.environ.get("APP_DB_PROCUREMENT_PASSWORD", "")
    bind.execute(sa.text("SELECT set_config('g24.pw_app_procurement', :p, true)"), {"p": pw})
    try:
        op.execute(ROLE_SQL)
    finally:
        bind.execute(sa.text("SELECT set_config('g24.pw_app_procurement', '', true)"))
    for stmt in DDL:
        op.execute(stmt)
    for t in TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {t} USING ({TENANT_PREDICATE}) "
            f"WITH CHECK ({TENANT_PREDICATE})"
        )
    # the balance/health loop discovers enabled suppliers of every tenant, then pins the tenant
    op.execute(
        "CREATE POLICY procurement_scan ON suppliers FOR SELECT TO app_procurement USING (true)"
    )
    for stmt in GRANTS:
        op.execute(stmt)


def downgrade() -> None:
    for t in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {t}")
    op.execute("REVOKE ALL ON card_stock, outbox, audit_log FROM app_procurement")
    op.execute("REVOKE ALL ON SEQUENCE outbox_id_seq, audit_log_id_seq FROM app_procurement")
    op.execute("REVOKE ALL ON tenants, orders, products, product_variants FROM app_procurement")
    op.execute("REVOKE ALL ON SCHEMA public FROM app_procurement")
    op.execute(
        """DO $$ BEGIN
            EXECUTE format('REVOKE ALL ON DATABASE %I FROM app_procurement', current_database());
        END $$"""
    )
