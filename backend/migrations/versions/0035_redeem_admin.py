"""Redemption admin (issue #31, review-v12 §23): attempt log + site settings.

``redeem_attempts`` (NEW, tenant-scoped, ENABLE + FORCE RLS): one row per attempt through the public
redemption proxy. It holds NO code, session, token or IP: ``code_head`` / ``code_tail`` (a masked
code, at most 4 + 4 characters and never more than a third of the code), ``code_fp`` (the existing
keyed code fingerprint of delivered codes, ``CardSealer.fingerprint``: a match handle, not
reversible without the key) and ``ip_hash`` (keyed hash of the /24 or /48, never the address).

================  =========================================================================
``app_procurement`` INSERT, SELECT, DELETE (the proxy writes; the sweeper deletes rows older
                  than 90 days with an unpinned session, the same policy shape as 0032).
``app_api``       SELECT only (the console reads; nobody can edit or delete a row).
everyone else     nothing.
================  =========================================================================

``tenants.redeem_settings`` (jsonb): the redemption site's maintenance switch, announcement and
per-IP limits. ``app_api`` may UPDATE that one column (the existing own-tenant policy applies);
``app_procurement`` may SELECT it (and nothing else new: ``tenants.settings`` stays unreadable
there), so the proxy reads its settings without the API ever pushing anything.

Revision ID: 0035_redeem_admin
Revises: 0034_job_progress
"""

from __future__ import annotations

from alembic import op

revision = "0035_redeem_admin"
down_revision = "0034_job_progress"
branch_labels = None
depends_on = None

PRED = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
UNPINNED = "NULLIF(current_setting('app.tenant_id', true), '') IS NULL"
T = "redeem_attempts"

UP = [
    f"""CREATE TABLE {T} (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        at timestamptz NOT NULL DEFAULT now(),
        step varchar(12) NOT NULL CHECK (step IN ('preview', 'preflight', 'redeem', 'result')),
        outcome varchar(16) NOT NULL CHECK (outcome IN (
            'ok', 'completed', 'failed', 'error', 'rate_limited', 'invalid', 'maintenance')),
        reason varchar(32),
        code_head varchar(4),
        code_tail varchar(4),
        code_fp varchar(64),
        plan varchar(40),
        ip_hash varchar(32)
    )""",
    f"CREATE INDEX ix_{T}_at ON {T} (tenant_id, at DESC)",
    f"CREATE INDEX ix_{T}_fp ON {T} (tenant_id, code_fp) WHERE code_fp IS NOT NULL",
    f"CREATE INDEX ix_{T}_age ON {T} (at)",
    f"ALTER TABLE {T} ENABLE ROW LEVEL SECURITY",
    f"ALTER TABLE {T} FORCE ROW LEVEL SECURITY",
    f"CREATE POLICY tenant_select ON {T} FOR SELECT USING ({PRED})",
    f"CREATE POLICY tenant_insert ON {T} FOR INSERT WITH CHECK ({PRED})",
    f"CREATE POLICY tenant_delete ON {T} FOR DELETE TO CURRENT_USER USING ({PRED})",
    f"CREATE POLICY procurement_scan ON {T} FOR SELECT TO app_procurement USING ({UNPINNED})",
    f"CREATE POLICY procurement_sweep ON {T} FOR DELETE TO app_procurement USING ({UNPINNED})",
    f"REVOKE ALL ON {T} FROM PUBLIC",
    f"GRANT SELECT ON {T} TO app_api",
    f"GRANT SELECT, INSERT, DELETE ON {T} TO app_procurement",
    "ALTER TABLE tenants ADD COLUMN redeem_settings jsonb",
    "GRANT UPDATE (redeem_settings) ON tenants TO app_api",
    "GRANT SELECT (redeem_settings) ON tenants TO app_procurement",
]

DOWN = [
    "REVOKE SELECT (redeem_settings) ON tenants FROM app_procurement",
    "REVOKE UPDATE (redeem_settings) ON tenants FROM app_api",
    "ALTER TABLE tenants DROP COLUMN redeem_settings",
    f"DROP TABLE {T}",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWN:
        op.execute(stmt)
