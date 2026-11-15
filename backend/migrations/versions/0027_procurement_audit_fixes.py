"""v12 M2 payment-audit fixes: send tracking and recovery on procurement jobs.

- ``maybe_sent``: set in the lease commit BEFORE the first supplier call of a send; cleared only
  when the supplier answered "nothing happened" (definite refusal / verified not found). Replaces
  the ``error_code`` heuristic: "switch source", failover and "maybe charged" read this flag.
- ``first_sent_at``: when the first send under the current idempotency key was leased; bounds
  how long a same-key replay is still trusted (``APP_REPLAY_MAX_AGE_S``).
- ``recover``: a job that closed (failed / refunded / hand-fulfilled) while the supplier may hold
  a code for it: the engine asks once under the same key to learn the reference, then disables
  the code. It never seals anything.
- ``lease_until``: set while the engine talks to the supplier about the job; staff "fulfil" and
  "fail" are refused until it passes.
- ``app_api`` may UPDATE ``recover`` (refund / staff fail schedule the recovery); the other new
  columns are engine-owned (``app_procurement`` has full rights on the table already).
- ``app_delivery`` may read the job state columns it needs to ignore a stale ``procure.failed``.

Revision ID: 0027_procurement_audit_fixes
Revises: 0026_procurement_admin_actions
"""

from __future__ import annotations

from alembic import op

revision = "0027_procurement_audit_fixes"
down_revision = "0026_procurement_admin_actions"
branch_labels = None
depends_on = None

UP = [
    """ALTER TABLE procurement_jobs
        ADD COLUMN maybe_sent boolean NOT NULL DEFAULT false,
        ADD COLUMN recover boolean NOT NULL DEFAULT false,
        ADD COLUMN lease_until timestamptz,
        ADD COLUMN first_sent_at timestamptz""",
    # existing rows: anything that may have reached a supplier is flagged (conservative)
    """UPDATE procurement_jobs SET maybe_sent = true, first_sent_at = created_at
        WHERE offer_id IS NOT NULL
          AND NOT (state = 'queued' AND step = 'submit')""",
    """CREATE INDEX ix_procurement_jobs_recover ON procurement_jobs (next_poll_at)
        WHERE recover""",
    "GRANT UPDATE (recover) ON procurement_jobs TO app_api",
    "GRANT SELECT (id, tenant_id, order_id, unit_no, state) ON procurement_jobs TO app_delivery",
]

DOWN = [
    "REVOKE SELECT (id, tenant_id, order_id, unit_no, state) ON procurement_jobs FROM app_delivery",
    "REVOKE UPDATE (recover) ON procurement_jobs FROM app_api",
    "DROP INDEX IF EXISTS ix_procurement_jobs_recover",
    """ALTER TABLE procurement_jobs DROP COLUMN first_sent_at, DROP COLUMN lease_until,
        DROP COLUMN recover, DROP COLUMN maybe_sent""",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWN:
        op.execute(stmt)
