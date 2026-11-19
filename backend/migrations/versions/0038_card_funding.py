"""Funding-card top-ups of direct orders (ADR-048, owner request 2026-11-19).

``procurement_fundings`` (NEW, tenant-scoped, ENABLE + FORCE RLS): one row per automatic top-up
the engine planned for a direct job (``POST /cards/recharge`` from the supplier wallet onto our
funding card). It holds NO card number: the vendor card id and the USD amount only. ``state``:
``planned`` (counted against the daily cap from the moment it is planned) -> ``sent`` (leased to
the adapter; an unclear answer keeps it here, still counted) -> ``done`` (the vendor confirmed) /
``failed`` (a definite refusal: nothing moved); ``planned`` -> ``void`` (never sent: the job picked
again). ``idem_key`` is the key the top-up is sent under (the vendor replays the first answer per
key), unique so one job attempt can never plan two.

================  =========================================================================
``app_procurement`` SELECT, INSERT, UPDATE (the engine plans and settles rows).
``app_api``       SELECT only (the console lists them; nobody can edit or delete a row).
everyone else     nothing.
================  =========================================================================

``tenants.funding_settings`` (jsonb): the automatic top-up switch, caps and BIN priority.
``app_api`` may UPDATE that one column (the console writes it behind a fresh 2FA code);
``app_procurement`` may SELECT it.

``procurement_jobs.fund_amount`` / ``fund_key``: the top-up (USD) planned for the job's picked card
and the key it is sent under; and
``card_candidates`` grows to 120 characters to carry each candidate's top-up in cents
(``"id:last4:cents,..."``).

Revision ID: 0038_card_funding
Revises: 0037_preorders
"""

from __future__ import annotations

from alembic import op

revision = "0038_card_funding"
down_revision = "0037_preorders"
branch_labels = None
depends_on = None

PRED = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
T = "procurement_fundings"

UP = [
    f"""CREATE TABLE {T} (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        supplier_id uuid NOT NULL REFERENCES suppliers(id),
        job_id uuid REFERENCES procurement_jobs(id),
        order_id uuid,
        kind varchar(8) NOT NULL DEFAULT 'recharge' CHECK (kind IN ('recharge')),
        card_id varchar(20) NOT NULL,
        amount numeric(18, 6) NOT NULL CHECK (amount > 0),
        currency varchar(3) NOT NULL DEFAULT 'USD',
        state varchar(8) NOT NULL DEFAULT 'planned'
            CHECK (state IN ('planned', 'sent', 'done', 'failed', 'void')),
        idem_key varchar(80) NOT NULL UNIQUE,
        error varchar(32),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    f"CREATE INDEX ix_{T}_day ON {T} (tenant_id, supplier_id, created_at DESC)",
    f"CREATE INDEX ix_{T}_job ON {T} (tenant_id, job_id)",
    f"ALTER TABLE {T} ENABLE ROW LEVEL SECURITY",
    f"ALTER TABLE {T} FORCE ROW LEVEL SECURITY",
    f"CREATE POLICY tenant_select ON {T} FOR SELECT USING ({PRED})",
    f"CREATE POLICY tenant_insert ON {T} FOR INSERT WITH CHECK ({PRED})",
    f"CREATE POLICY tenant_update ON {T} FOR UPDATE USING ({PRED}) WITH CHECK ({PRED})",
    f"REVOKE ALL ON {T} FROM PUBLIC",
    f"GRANT SELECT ON {T} TO app_api",
    f"GRANT SELECT, INSERT, UPDATE ON {T} TO app_procurement",
    "ALTER TABLE tenants ADD COLUMN funding_settings jsonb",
    "GRANT UPDATE (funding_settings) ON tenants TO app_api",
    "GRANT SELECT (funding_settings) ON tenants TO app_procurement",
    "ALTER TABLE procurement_jobs ADD COLUMN fund_amount numeric(18, 6)",
    "ALTER TABLE procurement_jobs ADD COLUMN fund_key varchar(80)",
    "ALTER TABLE procurement_jobs ALTER COLUMN card_candidates TYPE varchar(120)",
]

DOWN = [
    "ALTER TABLE procurement_jobs ALTER COLUMN card_candidates TYPE varchar(80)"
    " USING left(card_candidates, 80)",
    "ALTER TABLE procurement_jobs DROP COLUMN fund_key",
    "ALTER TABLE procurement_jobs DROP COLUMN fund_amount",
    "REVOKE SELECT (funding_settings) ON tenants FROM app_procurement",
    "REVOKE UPDATE (funding_settings) ON tenants FROM app_api",
    "ALTER TABLE tenants DROP COLUMN funding_settings",
    f"DROP TABLE {T}",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWN:
        op.execute(stmt)
