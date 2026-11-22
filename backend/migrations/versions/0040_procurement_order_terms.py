"""Upgrades (owner 2026-11-22): procurement reads ``orders.terms`` for the ``upgrade`` snapshot.

A direct order bought as an UPGRADE of the buyer's running plan stores what the buyer paid for in
``terms.upgrade``; the engine passes its USD difference to the adapter as the cost ceiling and
never buys the full plan for such an order. ``terms`` holds purchase terms only (after-sales,
subscription, pre-order window, upgrade): no buyer data.

Revision ID: 0040_procurement_order_terms
Revises: 0039_card_open_sweep
"""

from __future__ import annotations

from alembic import op

revision = "0040_procurement_order_terms"
down_revision = "0039_card_open_sweep"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT (terms) ON orders TO app_procurement")


def downgrade() -> None:
    op.execute("REVOKE SELECT (terms) ON orders FROM app_procurement")
