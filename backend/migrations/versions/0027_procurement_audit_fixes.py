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
- ``supplier_offers.cost_known``: the supplier itself named a redemption cost for the offer
  (set by the procurement process on an availability check). Together with ``cost_estimate`` it
  decides whether an offer may be enabled: the balance breaker fails closed on an unknown cost.
- ``card_stock_guard``: one more narrow exception for ``app_api`` (L2): a SEALED procurement code
  (``sold``) may go to ``void`` when its procurement job is being voided (``void_pending``,
  ``voided`` or ``failed`` in the same transaction) and only the void bookkeeping columns change,
  so a voided code can never be delivered.
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

GUARD_V12 = """CREATE OR REPLACE FUNCTION card_stock_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
    BEGIN
        IF current_user = 'app_api' THEN
            -- v11: move an AVAILABLE product-level card into a variant of the SAME product
            IF OLD.status = 'available' AND NEW.status = 'available'
               AND OLD.variant_id IS NULL AND NEW.variant_id IS NOT NULL
               AND (to_jsonb(NEW) - 'variant_id')
                   IS NOT DISTINCT FROM (to_jsonb(OLD) - 'variant_id')
               AND EXISTS (SELECT 1 FROM product_variants v
                           WHERE v.id = NEW.variant_id AND v.product_id = NEW.product_id
                             AND v.tenant_id = NEW.tenant_id AND v.status = 'active') THEN
                RETURN NEW;
            END IF;
            -- v12: a sealed procurement code whose job is being voided
            IF OLD.status = 'sold' AND NEW.status = 'void'
               AND NEW.void_reason LIKE 'procurement\\_void%'
               AND (to_jsonb(NEW) - 'status' - 'void_reason' - 'voided_at' - 'voided_by')
                   IS NOT DISTINCT FROM
                   (to_jsonb(OLD) - 'status' - 'void_reason' - 'voided_at' - 'voided_by')
               AND EXISTS (SELECT 1 FROM procurement_jobs j
                           WHERE j.tenant_id = OLD.tenant_id AND j.order_id = OLD.order_id
                             AND OLD.id = ANY (j.card_stock_ids)
                             AND j.state IN ('void_pending', 'voided', 'failed')) THEN
                RETURN NEW;
            END IF;
            IF OLD.status NOT IN ('available', 'reserved') OR NEW.status <> 'void' THEN
                RAISE EXCEPTION 'app_api may only void available or reserved cards'
                    USING ERRCODE = '42501';
            END IF;
            IF (to_jsonb(NEW) - 'status' - 'void_reason' - 'voided_at' - 'voided_by'
                    - 'replaced_by')
               IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'void_reason' - 'voided_at'
                    - 'voided_by' - 'replaced_by') THEN
                RAISE EXCEPTION 'app_api may only change void bookkeeping columns'
                    USING ERRCODE = '42501';
            END IF;
        END IF;
        RETURN NEW;
    END $$"""

GUARD_V11 = """CREATE OR REPLACE FUNCTION card_stock_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
    BEGIN
        IF current_user = 'app_api' THEN
            -- v11: move an AVAILABLE product-level card into a variant of the SAME product;
            -- nothing else may change (sealed secret, status, order untouched)
            IF OLD.status = 'available' AND NEW.status = 'available'
               AND OLD.variant_id IS NULL AND NEW.variant_id IS NOT NULL
               AND (to_jsonb(NEW) - 'variant_id')
                   IS NOT DISTINCT FROM (to_jsonb(OLD) - 'variant_id')
               AND EXISTS (SELECT 1 FROM product_variants v
                           WHERE v.id = NEW.variant_id AND v.product_id = NEW.product_id
                             AND v.tenant_id = NEW.tenant_id AND v.status = 'active') THEN
                RETURN NEW;
            END IF;
            IF OLD.status NOT IN ('available', 'reserved') OR NEW.status <> 'void' THEN
                RAISE EXCEPTION 'app_api may only void available or reserved cards'
                    USING ERRCODE = '42501';
            END IF;
            IF (to_jsonb(NEW) - 'status' - 'void_reason' - 'voided_at' - 'voided_by'
                    - 'replaced_by')
               IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'void_reason' - 'voided_at'
                    - 'voided_by' - 'replaced_by') THEN
                RAISE EXCEPTION 'app_api may only change void bookkeeping columns'
                    USING ERRCODE = '42501';
            END IF;
        END IF;
        RETURN NEW;
    END $$"""


UP = [
    """ALTER TABLE procurement_jobs
        ADD COLUMN maybe_sent boolean NOT NULL DEFAULT false,
        ADD COLUMN recover boolean NOT NULL DEFAULT false,
        ADD COLUMN lease_until timestamptz,
        ADD COLUMN first_sent_at timestamptz""",
    "ALTER TABLE supplier_offers ADD COLUMN cost_known boolean NOT NULL DEFAULT false",
    # existing rows: anything that may have reached a supplier is flagged (conservative)
    """UPDATE procurement_jobs SET maybe_sent = true, first_sent_at = created_at
        WHERE offer_id IS NOT NULL
          AND NOT (state = 'queued' AND step = 'submit')""",
    """CREATE INDEX ix_procurement_jobs_recover ON procurement_jobs (next_poll_at)
        WHERE recover""",
    GUARD_V12,
    "GRANT UPDATE (recover) ON procurement_jobs TO app_api",
    "GRANT SELECT (id, tenant_id, order_id, unit_no, state) ON procurement_jobs TO app_delivery",
]

DOWN = [
    "REVOKE SELECT (id, tenant_id, order_id, unit_no, state) ON procurement_jobs FROM app_delivery",
    "REVOKE UPDATE (recover) ON procurement_jobs FROM app_api",
    "DROP INDEX IF EXISTS ix_procurement_jobs_recover",
    "ALTER TABLE supplier_offers DROP COLUMN cost_known",
    """ALTER TABLE procurement_jobs DROP COLUMN first_sent_at, DROP COLUMN lease_until,
        DROP COLUMN recover, DROP COLUMN maybe_sent""",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    op.execute(GUARD_V11)
    for stmt in DOWN:
        op.execute(stmt)
