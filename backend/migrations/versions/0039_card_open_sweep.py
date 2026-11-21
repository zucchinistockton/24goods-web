"""ADR-049: record automatic card openings and leftover sweeps next to the top-ups.

``procurement_fundings.kind``: ``recharge`` (ADR-048) plus ``open`` (a new funding card opened
from the wallet for a paid order: opening fee + first load, counted against the daily cap) and
``sweep`` (a used-up card's money moved back to the wallet; never counted as spending).

Revision ID: 0039_card_open_sweep
Revises: 0038_card_funding
"""

from __future__ import annotations

from alembic import op

revision = "0039_card_open_sweep"
down_revision = "0038_card_funding"
branch_labels = None
depends_on = None

T = "procurement_fundings"

UP = [
    f"ALTER TABLE {T} DROP CONSTRAINT IF EXISTS {T}_kind_check",
    f"ALTER TABLE {T} ADD CONSTRAINT {T}_kind_check CHECK (kind IN ('recharge', 'open', 'sweep'))",
]

DOWN = [
    "DELETE FROM procurement_fundings WHERE kind IN ('open', 'sweep')",
    f"ALTER TABLE {T} DROP CONSTRAINT IF EXISTS {T}_kind_check",
    f"ALTER TABLE {T} ADD CONSTRAINT {T}_kind_check CHECK (kind IN ('recharge'))",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWN:
        op.execute(stmt)
