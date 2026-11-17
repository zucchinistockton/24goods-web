"""upstream_direct: the deletion of a sealed buyer session must not depend on procurement alone
(security review S-M1).

0029 let only ``app_procurement`` delete rows of ``procurement_credentials``. If procurement stops,
sessions would stay. This migration adds a DATABASE backstop for the API role:

- ``app_api`` gets ``DELETE``, restricted by policy to rows OLDER than 6 hours (the hard ceiling of
  ``APP_CRED_MAX_AGE_S``); the API's periodic reconcile deletes them and alerts staff.
- ``tenant_isolation`` (``FOR ALL``) is replaced by per-command policies, because permissive
  policies are OR-ed: a ``FOR ALL`` tenant policy would let the API delete ANY row of its tenant.
  SELECT / INSERT keep the tenant predicate; DELETE is tenant-pinned for ``app_procurement`` and
  the migration owner, and age-bounded for ``app_api``; UPDATE is possible for the owner only.

Revision ID: 0030_cred_delete_backstop
Revises: 0029_procurement_direct
"""

from __future__ import annotations

from alembic import op

revision = "0030_cred_delete_backstop"
down_revision = "0029_procurement_direct"
branch_labels = None
depends_on = None

PRED = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
T = "procurement_credentials"

UP = [
    f"DROP POLICY IF EXISTS tenant_isolation ON {T}",
    f"CREATE POLICY tenant_select ON {T} FOR SELECT USING ({PRED})",
    f"CREATE POLICY tenant_insert ON {T} FOR INSERT WITH CHECK ({PRED})",
    f"CREATE POLICY tenant_update ON {T} FOR UPDATE TO CURRENT_USER USING ({PRED}) "
    f"WITH CHECK ({PRED})",
    f"CREATE POLICY tenant_delete ON {T} FOR DELETE TO app_procurement, CURRENT_USER "
    f"USING ({PRED})",
    f"CREATE POLICY api_expired_delete ON {T} FOR DELETE TO app_api "
    f"USING ({PRED} AND created_at < now() - interval '6 hours')",
    f"GRANT DELETE ON {T} TO app_api",
]

DOWN = [
    f"REVOKE DELETE ON {T} FROM app_api",
    f"DROP POLICY IF EXISTS api_expired_delete ON {T}",
    f"DROP POLICY IF EXISTS tenant_delete ON {T}",
    f"DROP POLICY IF EXISTS tenant_update ON {T}",
    f"DROP POLICY IF EXISTS tenant_insert ON {T}",
    f"DROP POLICY IF EXISTS tenant_select ON {T}",
    f"CREATE POLICY tenant_isolation ON {T} USING ({PRED}) WITH CHECK ({PRED})",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWN:
        op.execute(stmt)
