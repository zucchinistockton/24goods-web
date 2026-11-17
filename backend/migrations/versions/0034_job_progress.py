"""Progress document of a procurement job (issue #30, review-v12 §22).

``procurement_jobs.progress`` (jsonb, NULL until a vendor answer changed something) holds, for
``upstream_direct`` jobs only:

* ``v``: the FURTHEST vendor state seen (the buyer's step; never moves backwards); ``raw``: the
  latest one (staff view, failure reason); both are the vendor ``status`` / ``stage`` /
  ``payment_state`` / ``funding_state`` (short machine tokens);
* ``events``: the staff timeline, one entry per CHANGE (poll or webhook), at most 200, newest
  last: time, source, the vendor fields, the vendor ``public_code`` and a SANITISED
  ``public_message`` (no URLs, control characters or card numbers, 160 chars);
* ``seen``: the vendor timeline event ids already recorded (de-duplication, at most 400);
* ``reached``: when each buyer-facing step was first reached; ``pub``: the last step whose
  notification Redis
  accepted (a notification goes out on a step CHANGE only, and is retried if it failed).

No session, code, card number or raw vendor body is ever written here. The column rides on the
existing table: ``app_procurement`` has table-level UPDATE and ``app_api`` table-level SELECT
(0023) and every row stays under FORCE RLS, so no new policy is needed.

No new grant: ``app_procurement`` never reads ``orders.buyer_id``. After a step changed it only
publishes ``{tenant_id, order_id}`` on the single Redis channel ``rt:procprogress``; the API
resolves the buyer and relays the buyer-facing event itself (ADR-043).

Revision ID: 0034_job_progress
Revises: 0033_card_modes
"""

from __future__ import annotations

from alembic import op

revision = "0034_job_progress"
down_revision = "0033_card_modes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE procurement_jobs ADD COLUMN progress jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE procurement_jobs DROP COLUMN progress")
