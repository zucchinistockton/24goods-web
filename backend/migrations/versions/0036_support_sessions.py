"""Buyer-consented session sharing with support (owner request 2026-11-18, review-v12 §26).

``procurement_credentials.share_with_support`` (boolean, default false): the buyer's consent, given
at checkout (or with a re-supplied session). ``app_api`` writes it with the row (its INSERT grant
covers the table) and may read it back (column grant).

``support_sessions`` (NEW, tenant-scoped, ENABLE + FORCE RLS): when a consented direct order goes
to staff (``needs_manual``) or fails, procurement copies the SEALED session (the same blob, AAD =
tenant + order id, still openable by procurement only) here, so it outlives the 6 h ceiling of
``procurement_credentials`` for the support window (72 h, swept by procurement). Support reads it
through procurement's internal route; each read is counted here and audited by the API.

================  =========================================================================
``app_procurement`` SELECT, INSERT, DELETE, UPDATE (revealed_count, last_revealed_at); the
                  sweeper deletes with an unpinned session (same policy shape as 0029).
``app_api``       SELECT of the non-secret columns only (never ``blob`` / ``kid``).
everyone else     nothing.
================  =========================================================================

Revision ID: 0036_support_sessions
Revises: 0035_redeem_admin
"""

from __future__ import annotations

from alembic import op

revision = "0036_support_sessions"
down_revision = "0035_redeem_admin"
branch_labels = None
depends_on = None

PRED = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
UNPINNED = "NULLIF(current_setting('app.tenant_id', true), '') IS NULL"
T = "support_sessions"

UP = [
    "ALTER TABLE procurement_credentials ADD COLUMN share_with_support boolean NOT NULL "
    "DEFAULT false",
    "GRANT SELECT (share_with_support) ON procurement_credentials TO app_api",
    f"""CREATE TABLE {T} (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        order_id uuid NOT NULL REFERENCES orders(id),
        product varchar(8) NOT NULL CHECK (product IN ('gpt', 'claude', 'grok')),
        kid varchar(64) NOT NULL,
        blob bytea NOT NULL CHECK (octet_length(blob) BETWEEN 64 AND 140000),
        email_masked varchar(64),
        reason varchar(48),
        revealed_count integer NOT NULL DEFAULT 0,
        last_revealed_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_support_sessions_order UNIQUE (order_id)
    )""",
    f"CREATE INDEX ix_{T}_age ON {T} (created_at)",
    f"ALTER TABLE {T} ENABLE ROW LEVEL SECURITY",
    f"ALTER TABLE {T} FORCE ROW LEVEL SECURITY",
    f"CREATE POLICY tenant_isolation ON {T} USING ({PRED}) WITH CHECK ({PRED})",
    f"CREATE POLICY procurement_scan ON {T} FOR SELECT TO app_procurement USING ({UNPINNED})",
    f"CREATE POLICY procurement_sweep ON {T} FOR DELETE TO app_procurement USING ({UNPINNED})",
    f"REVOKE ALL ON {T} FROM PUBLIC",
    f"GRANT SELECT (id, tenant_id, order_id, product, email_masked, reason, revealed_count, "
    f"last_revealed_at, created_at) ON {T} TO app_api",
    f"GRANT SELECT, INSERT, DELETE ON {T} TO app_procurement",
    f"GRANT UPDATE (revealed_count, last_revealed_at) ON {T} TO app_procurement",
]

DOWN = [
    f"DROP TABLE IF EXISTS {T}",
    "ALTER TABLE procurement_credentials DROP COLUMN IF EXISTS share_with_support",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWN:
        op.execute(stmt)
