"""zzshu adapter (ADR-054): ``suppliers.adapter`` accepts ``zzshu``.

The code / adapter rule (``ck_suppliers_code_adapter``: code = adapter or ``<adapter>_...``) already
covers it, so only the adapter list changes.

Revision ID: 0041_zzshu_adapter
Revises: 0040_procurement_order_terms
"""

from __future__ import annotations

from alembic import op

revision = "0041_zzshu_adapter"
down_revision = "0040_procurement_order_terms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE suppliers DROP CONSTRAINT IF EXISTS suppliers_adapter_check")
    op.execute(
        "ALTER TABLE suppliers ADD CONSTRAINT suppliers_adapter_check "
        "CHECK (adapter IN ('zovocard', 'zzshu', 'mock', 'manual'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE suppliers DROP CONSTRAINT IF EXISTS suppliers_adapter_check")
    op.execute(
        "ALTER TABLE suppliers ADD CONSTRAINT suppliers_adapter_check "
        "CHECK (adapter IN ('zovocard', 'mock', 'manual'))"
    )
