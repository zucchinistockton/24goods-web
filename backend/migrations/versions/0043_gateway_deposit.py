"""Per-gateway deposit guard (owner request 2026-11-26).

Some EPay operators (Xiaodou) require a merchant deposit of ``max(floor, highest single payment of
the last 30 days x pct)``; a payment that raises the due amount starts a 48 h grace period, after
which new orders are refused until the deposit is paid in full. These nullable settings let routing
skip a gateway for a payment larger than its deposit covers:

* ``deposit_minor``       deposit already paid at the gateway (CNY minor units);
* ``deposit_pct``         the gateway's percentage of the largest single payment (1..100);
* ``deposit_floor_minor`` the gateway's minimum deposit (CNY minor units).

All NULL = the feature is off for that gateway (the behaviour before this migration).
``app_api`` may UPDATE only these three columns on top of the routing columns it already has.

Revision ID: 0043_gateway_deposit
Revises: 0042_vmcardio_adapter
"""

from __future__ import annotations

from alembic import op

revision = "0043_gateway_deposit"
down_revision = "0042_vmcardio_adapter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE payment_gateways "
        "ADD COLUMN deposit_minor bigint "
        "CHECK (deposit_minor >= 0 AND deposit_minor <= 1000000000), "
        "ADD COLUMN deposit_pct smallint CHECK (deposit_pct BETWEEN 1 AND 100), "
        "ADD COLUMN deposit_floor_minor bigint "
        "CHECK (deposit_floor_minor >= 0 AND deposit_floor_minor <= 1000000000)"
    )
    op.execute(
        "GRANT UPDATE (deposit_minor, deposit_pct, deposit_floor_minor) "
        "ON payment_gateways TO app_api"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE payment_gateways DROP COLUMN IF EXISTS deposit_floor_minor, "
        "DROP COLUMN IF EXISTS deposit_pct, DROP COLUMN IF EXISTS deposit_minor"
    )
