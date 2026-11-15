"""v12 M2: staff actions on procurement jobs run in the API process (review-v12 §7).

``app_api`` may UPDATE the job bookkeeping columns only (state machine, same idempotency key on
retry, a new key only when switching to another source, ``manual_by``, the sealed card ids of a
manual fulfilment). It still cannot DELETE jobs, create them, or touch cost / currency /
supplier credentials, and the sealing it does for a pasted code uses the vault PUBLIC key only.

Revision ID: 0026_procurement_admin_actions
Revises: 0025_procurement_ledger_breaker
"""

from __future__ import annotations

from alembic import op

revision = "0026_procurement_admin_actions"
down_revision = "0025_procurement_ledger_breaker"
branch_labels = None
depends_on = None

COLS = (
    "state, step, error_code, attempts, next_poll_at, card_stock_ids, manual_by, offer_id, "
    "supplier_id, idempotency_key, request_hash, upstream_ref, upstream_status, "
    "tried_offer_ids, alerted_at, updated_at"
)


def upgrade() -> None:
    op.execute(f"GRANT UPDATE ({COLS}) ON procurement_jobs TO app_api")


def downgrade() -> None:
    op.execute(f"REVOKE UPDATE ({COLS}) ON procurement_jobs FROM app_api")
