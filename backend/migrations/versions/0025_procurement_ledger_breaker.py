"""v12 M2: cost-ledger linking columns and the balance-breaker kind (review-v12 §6).

- ``supplier_ledger``: ``ref_id`` (the vendor's reference number, used to link a CDK purchase
  row to its job) and ``card_mask`` (masked card number only, never the full PAN).
- ``suppliers.breaker_kind``: ``hard`` (a supplier refusal: stays open until staff reset it) or
  ``balance`` (spendable balance below the threshold: closes by itself when the balance is
  back). NULL while the breaker is closed.
- ``app_api`` may UPDATE the breaker columns (staff "reset breaker"); procurement already
  holds full rights on ``suppliers``.

Revision ID: 0025_procurement_ledger_breaker
Revises: 0024_procurement_cdk
"""

from __future__ import annotations

from alembic import op

revision = "0025_procurement_ledger_breaker"
down_revision = "0024_procurement_cdk"
branch_labels = None
depends_on = None

UP = [
    "ALTER TABLE supplier_ledger ADD COLUMN ref_id varchar(64), ADD COLUMN card_mask varchar(24)",
    "CREATE INDEX ix_supplier_ledger_ref ON supplier_ledger (supplier_id, ref_id) "
    "WHERE ref_id IS NOT NULL",
    "ALTER TABLE suppliers ADD COLUMN breaker_kind varchar(8) "
    "CHECK (breaker_kind IN ('hard', 'balance'))",
    "UPDATE suppliers SET breaker_kind = 'hard' WHERE breaker_open",
    "GRANT UPDATE (breaker_open, breaker_reason, breaker_at, breaker_kind) ON suppliers TO app_api",
]

DOWN = [
    "REVOKE UPDATE (breaker_open, breaker_reason, breaker_at, breaker_kind) "
    "ON suppliers FROM app_api",
    "ALTER TABLE suppliers DROP COLUMN breaker_kind",
    "DROP INDEX IF EXISTS ix_supplier_ledger_ref",
    "ALTER TABLE supplier_ledger DROP COLUMN card_mask, DROP COLUMN ref_id",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWN:
        op.execute(stmt)
