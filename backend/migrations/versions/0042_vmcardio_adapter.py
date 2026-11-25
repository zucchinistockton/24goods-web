"""vmcardio adapter (ADR-055): ``suppliers.adapter`` accepts ``vmcardio``; card ids get room.

* ``suppliers.adapter`` list gains ``vmcardio`` (the ``ck_suppliers_code_adapter`` rule already
  covers its code).
* ``procurement_jobs.card_id`` and ``procurement_fundings.card_id`` grow to 64: the dedicated card
  of a zzshu order opened at vmcardio is ``vm:<vm_card_id>`` (a ~22 character vendor id).
* ``procurement_jobs.card_open_seq`` (the number of the job's dedicated-card open request, bumped
  after a definite "nothing created" answer) and ``card_open_issuer`` (the issuer asked and not yet
  known to have opened nothing: a card may exist there), see ``CardOpenState``.

Revision ID: 0042_vmcardio_adapter
Revises: 0041_zzshu_adapter
"""

from __future__ import annotations

from alembic import op

revision = "0042_vmcardio_adapter"
down_revision = "0041_zzshu_adapter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE suppliers DROP CONSTRAINT IF EXISTS suppliers_adapter_check")
    op.execute(
        "ALTER TABLE suppliers ADD CONSTRAINT suppliers_adapter_check "
        "CHECK (adapter IN ('zovocard', 'zzshu', 'vmcardio', 'mock', 'manual'))"
    )
    op.execute("ALTER TABLE procurement_jobs ALTER COLUMN card_id TYPE varchar(64)")
    op.execute("ALTER TABLE procurement_fundings ALTER COLUMN card_id TYPE varchar(64)")
    op.execute(
        "ALTER TABLE procurement_jobs ADD COLUMN card_open_seq integer NOT NULL DEFAULT 0 "
        "CHECK (card_open_seq >= 0 AND card_open_seq < 1000)"
    )
    op.execute("ALTER TABLE procurement_jobs ADD COLUMN card_open_issuer varchar(32)")


def downgrade() -> None:
    op.execute("ALTER TABLE procurement_jobs DROP COLUMN IF EXISTS card_open_issuer")
    op.execute("ALTER TABLE procurement_jobs DROP COLUMN IF EXISTS card_open_seq")
    op.execute("ALTER TABLE procurement_fundings ALTER COLUMN card_id TYPE varchar(20)")
    op.execute("ALTER TABLE procurement_jobs ALTER COLUMN card_id TYPE varchar(20)")
    op.execute("ALTER TABLE suppliers DROP CONSTRAINT IF EXISTS suppliers_adapter_check")
    op.execute(
        "ALTER TABLE suppliers ADD CONSTRAINT suppliers_adapter_check "
        "CHECK (adapter IN ('zovocard', 'zzshu', 'mock', 'manual'))"
    )
