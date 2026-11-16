"""Optional two-factor login (TOTP): ``user_totp``.

One row per user who started or finished enrolment. The secret is stored sealed (AES-256-GCM under
a key derived from the settings key, bound to tenant and user); recovery codes are stored as keyed
hashes. ``last_step`` is the last accepted time step, so a code is accepted once. ``confirmed_at``
NULL means the enrolment was started but no code has been verified yet: login ignores such rows.

RLS: ENABLE + FORCE + ``tenant_isolation``. Only app_api gets access; the delivery worker and the
other roles never see the table.

The revision id sorts between 0022 and the v12 procurement chain: ``0023_procurement`` revises
this one.

Revision ID: 0022a_user_totp
Revises: 0022_v11_content_ops
"""

from __future__ import annotations

from alembic import op

revision = "0022a_user_totp"
down_revision = "0022_v11_content_ops"
branch_labels = None
depends_on = None

TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("""CREATE TABLE user_totp (
        user_id uuid PRIMARY KEY REFERENCES users(id),
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        secret_sealed bytea NOT NULL,
        confirmed_at timestamptz,
        last_step bigint,
        recovery_hashes text[] NOT NULL DEFAULT '{}',
        created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("ALTER TABLE user_totp ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_totp FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON user_totp USING ({TENANT_PREDICATE}) "
        f"WITH CHECK ({TENANT_PREDICATE})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON user_totp TO app_api")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_totp")
