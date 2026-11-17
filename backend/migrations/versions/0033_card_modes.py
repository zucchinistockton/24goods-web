"""Card modes for direct orders (issue #29, review-v12 §21).

``procurement_jobs`` gets the funding card a direct job actually used and the wait of a job that
has no card yet. No new state: a job that waits for a card stays ``queued`` (error_code
``waiting_for_card``), so the state CHECK of 0023 is unchanged.

* ``card_id``: the vendor card id (numeric text) WE SENT the order with (the in-flight identity of
  a job; never overwritten by what the vendor reports). A card ID is not a card number; the full
  number never reaches this table.
* ``card_last4``: the last four digits of the sent card only (NULL when unknown).
* ``used_card_id`` / ``used_last4``: the card the VENDOR says paid the order (``vendor_switch`` may
  move it). Counted as in flight too when it differs from ``card_id``.
* ``card_candidates``: the usable cards of the last pre-check, ``"id:last4,id:last4"`` (max 3),
  consumed by the lease that sends the order; ``card_candidates_at`` is when they were checked
  (a pick older than 120 s is re-checked, never spent on).
* ``card_attempt``: counts the re-keyed attempts after a verified vendor refusal (0 = the first
  key). The idempotency key and client request id of an attempt derive from it.
* ``card_wait_since``: when the job first waited for a card (bounds the total wait; one staff
  warning audit row is written at that moment).

No new grant is needed: ``app_procurement`` has table-level SELECT / UPDATE and ``app_api`` has
table-level SELECT on ``procurement_jobs`` (0023); every row stays under FORCE RLS.

Revision ID: 0033_card_modes
Revises: 0032_half_price_checks
"""

from __future__ import annotations

from alembic import op

revision = "0033_card_modes"
down_revision = "0032_half_price_checks"
branch_labels = None
depends_on = None

UP = [
    "ALTER TABLE procurement_jobs ADD COLUMN card_id varchar(20)",
    "ALTER TABLE procurement_jobs ADD COLUMN card_last4 varchar(4) "
    "CHECK (card_last4 IS NULL OR card_last4 ~ '^[0-9]{4}$')",
    "ALTER TABLE procurement_jobs ADD COLUMN card_candidates varchar(80)",
    "ALTER TABLE procurement_jobs ADD COLUMN card_wait_since timestamptz",
    "ALTER TABLE procurement_jobs ADD COLUMN used_card_id varchar(20)",
    "ALTER TABLE procurement_jobs ADD COLUMN used_last4 varchar(4) "
    "CHECK (used_last4 IS NULL OR used_last4 ~ '^[0-9]{4}$')",
    "ALTER TABLE procurement_jobs ADD COLUMN card_candidates_at timestamptz",
    "ALTER TABLE procurement_jobs ADD COLUMN card_attempt integer NOT NULL DEFAULT 0 "
    "CHECK (card_attempt >= 0 AND card_attempt <= 20)",
]
DOWN = [
    "ALTER TABLE procurement_jobs DROP COLUMN card_attempt",
    "ALTER TABLE procurement_jobs DROP COLUMN card_candidates_at",
    "ALTER TABLE procurement_jobs DROP COLUMN used_last4",
    "ALTER TABLE procurement_jobs DROP COLUMN used_card_id",
    "ALTER TABLE procurement_jobs DROP COLUMN card_wait_since",
    "ALTER TABLE procurement_jobs DROP COLUMN card_candidates",
    "ALTER TABLE procurement_jobs DROP COLUMN card_last4",
    "ALTER TABLE procurement_jobs DROP COLUMN card_id",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWN:
        op.execute(stmt)
