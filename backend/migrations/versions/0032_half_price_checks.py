"""Half-price eligibility check before payment (issue #27, review-v12 §20.4).

``half_price_checks`` (NEW, tenant-scoped, ENABLE + FORCE RLS): one row per check a buyer runs for a
half-price plan (``plus_half``, Grok ``*_half``) BEFORE an order can exist. The buyer's account
session is sealed AT ONCE with the procurement public key (same primitive and keypair as
``procurement_credentials``; the AAD binds the blob to the check id, so it opens nowhere else) and
only the procurement process opens it, for the one call that reads eligibility, then nulls it.

Grants (column level; the API can never read ``blob`` / ``kid`` back):

================  =========================================================================
``app_api``       INSERT; SELECT of every column except ``blob`` / ``kid``; UPDATE of
                  ``order_id`` ONLY (consume the check when the order is created). A trigger
                  limits it: NULL -> value, only for an order of the SAME buyer, and only for a
                  USABLE check; a second trigger limits INSERT to ``pending`` / ``manual_pending``
                  rows with no order, reason, decision or account binding (``passed`` /
                  ``manual_pending`` / ``manual_approved``). The staff decision of a manual check
                  goes through ``half_check_decide()`` (SECURITY DEFINER; ``manual_pending`` ->
                  approved / refused only, same tenant as the session pin), so the API role
                  cannot write ``status`` / ``reason`` / ``decided_at`` and cannot turn a refused
                  check into a passed one.
                  DELETE only of a ``pending`` row (the check call to procurement failed).
``app_procurement`` SELECT, UPDATE (``blob``, ``status``, ``reason``, ``detail``,
                  ``email_masked``, ``account_hmac``, ``decided_at``), DELETE; the sweeper (unpinned
                  session) wipes stale blobs and old unused rows.
everyone else     nothing.
================  =========================================================================

Revision ID: 0032_half_price_checks
Revises: 0031_variant_options
"""

from __future__ import annotations

from alembic import op

revision = "0032_half_price_checks"
down_revision = "0031_variant_options"
branch_labels = None
depends_on = None

PRED = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
UNPINNED = "NULLIF(current_setting('app.tenant_id', true), '') IS NULL"
T = "half_price_checks"
OPEN_COLS = (
    "id, tenant_id, user_id, product_id, variant_id, method, vendor_product, plan, country, "
    "status, reason, detail, email_masked, order_id, created_at, decided_at"
)

UP = [
    f"""CREATE TABLE {T} (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        user_id uuid NOT NULL REFERENCES users(id),
        product_id uuid NOT NULL REFERENCES products(id),
        variant_id uuid REFERENCES product_variants(id),
        method varchar(16) NOT NULL
            CHECK (method IN ('vendor_preflight', 'chatgpt_direct', 'manual')),
        vendor_product varchar(8) NOT NULL CHECK (vendor_product IN ('gpt', 'claude', 'grok')),
        plan varchar(40) NOT NULL,
        country varchar(2),
        status varchar(16) NOT NULL DEFAULT 'pending' CHECK (status IN (
            'pending', 'passed', 'ineligible', 'unavailable', 'session_invalid',
            'manual_pending', 'manual_approved', 'manual_refused')),
        reason varchar(48),
        detail jsonb,
        email_masked varchar(64),
        account_hmac varchar(64),
        kid varchar(64),
        blob bytea CHECK (blob IS NULL OR octet_length(blob) BETWEEN 64 AND 140000),
        order_id uuid UNIQUE REFERENCES orders(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        decided_at timestamptz
    )""",
    f"CREATE INDEX ix_{T}_user ON {T} (tenant_id, user_id, created_at)",
    f"CREATE INDEX ix_{T}_blob ON {T} (created_at) WHERE blob IS NOT NULL",
    f"ALTER TABLE {T} ENABLE ROW LEVEL SECURITY",
    f"ALTER TABLE {T} FORCE ROW LEVEL SECURITY",
    f"CREATE POLICY tenant_select ON {T} FOR SELECT USING ({PRED})",
    f"CREATE POLICY tenant_insert ON {T} FOR INSERT WITH CHECK ({PRED})",
    f"CREATE POLICY tenant_update ON {T} FOR UPDATE USING ({PRED}) WITH CHECK ({PRED})",
    f"CREATE POLICY tenant_delete ON {T} FOR DELETE TO app_procurement, CURRENT_USER "
    f"USING ({PRED})",
    f"CREATE POLICY api_pending_delete ON {T} FOR DELETE TO app_api "
    f"USING ({PRED} AND status = 'pending')",
    f"CREATE POLICY procurement_scan ON {T} FOR SELECT TO app_procurement USING ({UNPINNED})",
    f"CREATE POLICY procurement_wipe ON {T} FOR UPDATE TO app_procurement "
    f"USING ({UNPINNED}) WITH CHECK ({UNPINNED})",
    f"CREATE POLICY procurement_sweep ON {T} FOR DELETE TO app_procurement USING ({UNPINNED})",
    f"REVOKE ALL ON {T} FROM PUBLIC",
    f"GRANT INSERT ON {T} TO app_api",
    f"GRANT SELECT ({OPEN_COLS}) ON {T} TO app_api",
    f"GRANT UPDATE (order_id) ON {T} TO app_api",
    f"GRANT DELETE ON {T} TO app_api",
    f"GRANT SELECT, DELETE ON {T} TO app_procurement",
    f"GRANT UPDATE (blob, status, reason, detail, email_masked, account_hmac, "
    f"decided_at) ON {T} TO app_procurement",
    f"""CREATE FUNCTION {T}_guard() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, public, pg_temp AS $$
    BEGIN
        IF current_user = 'app_api' THEN
            IF NEW.order_id IS DISTINCT FROM OLD.order_id AND (
                OLD.order_id IS NOT NULL
                OR OLD.status NOT IN ('passed', 'manual_pending', 'manual_approved')
            ) THEN
                RAISE EXCEPTION 'a half-price check is consumed once, and only when usable';
            END IF;
            IF NEW.order_id IS DISTINCT FROM OLD.order_id AND NOT EXISTS (
                SELECT 1 FROM public.orders o
                WHERE o.id = NEW.order_id AND o.tenant_id = OLD.tenant_id
                  AND o.buyer_id = OLD.user_id
            ) THEN
                RAISE EXCEPTION 'a half-price check is consumed by an order of the same buyer';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status
               OR NEW.reason IS DISTINCT FROM OLD.reason
               OR NEW.decided_at IS DISTINCT FROM OLD.decided_at THEN
                RAISE EXCEPTION 'the API cannot change the outcome of a half-price check';
            END IF;
        END IF;
        RETURN NEW;
    END $$""",  # noqa: S608 (constant table name, no user input)
    f"""CREATE FUNCTION {T}_decide(p_id uuid, p_approve boolean) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    DECLARE
        pinned uuid := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
        new_status text := CASE WHEN p_approve THEN 'manual_approved' ELSE 'manual_refused' END;
        n integer;
    BEGIN
        IF pinned IS NULL THEN
            RAISE EXCEPTION 'half-price decision: no tenant';
        END IF;
        UPDATE {T} SET status = new_status, decided_at = now()
        WHERE id = p_id AND tenant_id = pinned AND status = 'manual_pending';
        GET DIAGNOSTICS n = ROW_COUNT;
        IF n = 0 THEN
            RETURN NULL;
        END IF;
        RETURN new_status;
    END $$""",  # noqa: S608 (constant table name, no user input)
    f"REVOKE ALL ON FUNCTION {T}_decide(uuid, boolean) FROM PUBLIC",
    f"GRANT EXECUTE ON FUNCTION {T}_decide(uuid, boolean) TO app_api",
    f"CREATE TRIGGER {T}_guard BEFORE UPDATE ON {T} FOR EACH ROW EXECUTE FUNCTION {T}_guard()",
    # INSERT guard: the API role may only open a check (never a decided or consumed one). It is
    # tied to the role name, so a startup assertion in the API process (prod) makes sure that it
    # really connects as ``app_api`` (``app.main``).
    f"""CREATE FUNCTION {T}_insert_guard() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, public, pg_temp AS $$
    BEGIN
        IF current_user = 'app_api' AND (
            NEW.status NOT IN ('pending', 'manual_pending')
            OR NEW.order_id IS NOT NULL
            OR NEW.reason IS NOT NULL
            OR NEW.decided_at IS NOT NULL
            OR NEW.account_hmac IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'the API can only open a half-price check';
        END IF;
        RETURN NEW;
    END $$""",
    f"CREATE TRIGGER {T}_insert_guard BEFORE INSERT ON {T} FOR EACH ROW "
    f"EXECUTE FUNCTION {T}_insert_guard()",
]

DOWN = [
    f"DROP TABLE IF EXISTS {T}",
    f"DROP FUNCTION IF EXISTS {T}_guard()",
    f"DROP FUNCTION IF EXISTS {T}_insert_guard()",
    f"DROP FUNCTION IF EXISTS {T}_decide(uuid, boolean)",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWN:
        op.execute(stmt)
