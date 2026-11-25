"""card.zzshu.pro adapter (ADR-054): standby provider for ChatGPT ``direct`` top-ups.

How a zzshu order differs from a ZovoCard one:

* zzshu is paid with a bank card WE hand over per order (``POST /orders/direct`` takes the number,
  expiry, CVV and the buyer's session). Every order gets its OWN new ZovoCard card, opened just for
  it and loaded with ``offer cost x (1 + margin)``; when the order is over (success or failure) the
  leftover goes back to the wallet and the card is deleted. The number / expiry / CVV are read from
  ``GET /cards/{id}`` right before the one POST, held in memory only and dropped; the buyer's
  session likewise. Neither is ever stored, logged, audited, put into an exception or returned.
* zzshu has NO idempotency key. The create is sent at most once per job: a create whose answer is
  lost is AMBIGUOUS and goes to staff (they can look at the zzshu history), never resent.
* A zzshu failure is NOT followed by another source (the card may have been charged): staff decide.

The reference of an order is zzshu's ``card_key`` (staff only); the buyer sees ``order_no``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal
from typing import Any

from app.suppliers import direct_progress as progress
from app.suppliers.base import (
    Availability,
    Balance,
    Capability,
    JobSpec,
    Money,
    OfferSpec,
    Outcome,
    ProbeReport,
    Result,
    SupplierError,
    SupplierEvent,
    WebhookRejected,
)
from app.suppliers.direct_credential import mask_email
from app.suppliers.zovocard import direct as dr
from app.suppliers.zzshu.client import ZzshuClient

log = logging.getLogger("app.suppliers.zzshu")

# our ZovoCard plan key -> zzshu ``planType``. Upgrades are sold at full price (owner rule), so the
# ``plus_to_5x`` / ``to_20x`` upgrade types are never used; plus_half, pro_50x are unsupported.
PLAN_TYPES: dict[str, str] = {"go": "go", "plus": "plus", "pro_5x": "pro5", "pro_20x": "pro20"}
REGIONS: tuple[str, ...] = ("PH", "US", "EG", "CL")  # JP is not offered by zzshu
POINTS_TTL_S = 60.0
STATUS_RECHECK_S = 2.5  # docs: confirm a first ``failed`` after 2-3 s
_REF_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
_NO_RE = re.compile(r"[^A-Za-z0-9_-]")
# zzshu status -> the vocabulary of ``direct_progress`` (buyer steps, staff timeline)
_VENDOR_STATUS = {"pending": "queued", "processing": "running", "success": "completed"}


@dataclass(frozen=True)
class ZzshuSpec:
    plan: str  # our plan key (``plus``)
    plan_type: str  # zzshu planType (``plus``, ``pro5`` ...)
    region: str


# the submit pass that only opened the dedicated card (the engine re-leases the job at once)
CARD_OPENED = "card_opened"


def zzshu_spec(offer: OfferSpec) -> ZzshuSpec | None:
    """Validate a zzshu ``direct`` offer BEFORE any call (None = invalid): GPT only, a plan with a
    full-price zzshu type and an explicit region zzshu offers. No card parameters exist: the card
    is opened per order."""
    if offer.kind != "direct":
        return None
    p = offer.params
    if set(p) - {"product", "plan", "payment_country", dr.PICKED_CARD_KEY}:
        return None  # no card_id / card_mode / currency: nothing here may name a card
    if p.get("product") != "gpt":
        return None
    plan, region = p.get("plan"), p.get("payment_country")
    if not isinstance(plan, str) or plan not in PLAN_TYPES:
        return None
    if not isinstance(region, str) or region not in REGIONS:
        return None
    return ZzshuSpec(plan, PLAN_TYPES[plan], region)


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def need_usd(cost: Decimal, margin_pct: Decimal) -> Decimal:
    """What the dedicated card is loaded with: ``cost x (1 + margin / 100)``, cents up."""
    return (cost * (1 + margin_pct / 100)).quantize(Decimal("0.01"), rounding=ROUND_CEILING)


class ZzshuSupplier:
    capabilities = frozenset({Capability.DIRECT})
    status_recheck_s = STATUS_RECHECK_S  # tests set 0

    def __init__(
        self, code: str, client: ZzshuClient, funding: Any, *, owns_funding: bool = False
    ) -> None:
        self.code = code
        self.client = client
        self.env = client.config.env
        self.funding = funding  # the ZovoCard supplier that opens / reads / sweeps the cards
        self._owns_funding = owns_funding
        self._points_hit: tuple[float, int] | None = None

    async def aclose(self) -> None:
        await self.client.aclose()
        if self._owns_funding:
            await self.funding.aclose()

    # ---- reads
    async def balance(self) -> Balance:
        """The wallet that pays the dedicated cards (zzshu itself is paid in points)."""
        return await self.funding.balance()

    async def points(self, *, fresh: bool = False) -> int:
        """``GET /user`` -> remaining successful activations. Raises ``SupplierError``."""
        hit = self._points_hit
        if hit is not None and not fresh and time.monotonic() - hit[0] < POINTS_TTL_S:
            return hit[1]
        resp = await self.client.get("/user")
        data = resp.data if isinstance(resp.data, Mapping) else {}
        pts = _int(data.get("points"))
        if pts is None:
            raise SupplierError(
                "unreadable points", outcome=Outcome.RETRYABLE, error_code="points_unreadable"
            )
        self._points_hit = (time.monotonic(), pts)
        return pts

    async def probe(self) -> ProbeReport:
        now = datetime.now(timezone.utc)
        errors: list[str] = []
        auth_ok: bool | None = None
        seen: int | None = None
        try:
            pts = await self.points(fresh=True)
            auth_ok = True
            seen = pts
            if pts < 1:
                errors.append("points_exhausted")
        except SupplierError as exc:
            auth_ok = False if exc.supplier_stop else None
            errors.append(exc.reason)
        bal: Balance | None = None
        try:
            bal = await self.balance()
        except Exception as exc:  # the funding wallet is a second dependency: report, don't raise
            errors.append(f"funding_{type(exc).__name__}"[:64])
        return ProbeReport(
            supplier=self.code,
            env=self.env,
            ok=not errors,
            auth_ok=auth_ok,
            balance=bal,
            errors=tuple(errors),
            checked_at=now,
            points=seen,
        )

    async def availability_fresh(self, offer: OfferSpec) -> Availability:
        return await self.availability(offer, fresh=True)

    async def availability(self, offer: OfferSpec, *, fresh: bool = False) -> Availability:
        """Fail closed: needs a supported plan / region, at least one point, the offer's USD cost,
        the funding policy to allow a card of ``cost x (1 + margin)`` and a wallet that holds it."""
        now = datetime.now(timezone.utc)
        spec = zzshu_spec(offer)
        if spec is None:
            return Availability(False, "invalid_offer", checked_at=now)
        try:
            pts = await self.points(fresh=fresh)
        except SupplierError as exc:
            return Availability(False, exc.reason, checked_at=now)
        if pts < 1:
            return Availability(False, "points_exhausted", checked_at=now)
        if offer.cost_usd is None or offer.cost_usd <= 0:
            return Availability(False, dr.COST_UNKNOWN, checked_at=now)
        pol = offer.funding
        need = need_usd(offer.cost_usd, pol.zzshu_margin_pct if pol else Decimal(20))
        cost = Money(offer.cost_usd, "USD")
        if pol is None or not pol.auto_recharge or need > pol.allowance:
            return Availability(False, dr.FUNDING_CAP_REACHED, cost_estimate=cost, checked_at=now)
        wallet = await self.funding._wallet(fresh)
        if wallet is None:
            return Availability(False, "wallet_unreadable", cost_estimate=cost, checked_at=now)
        if max(Decimal(0), wallet - pol.reserved) < need:
            return Availability(False, dr.FUNDS_INSUFFICIENT, cost_estimate=cost, checked_at=now)
        return Availability(True, cost_estimate=cost, checked_at=now)

    # ---- writes
    async def submit(self, job: JobSpec) -> Result:
        """Open a dedicated card, read it, send the ONE create. See the module docstring."""
        spec = zzshu_spec(job.offer)
        if spec is None:
            return Result(Outcome.FATAL, error_code="invalid_offer")
        if job.upstream_ref:
            return await self.query(job)  # the order exists: only ever read it
        if job.prior_send:
            # an earlier send of this job may have reached zzshu (no idempotency key): never again
            return Result(Outcome.FATAL, error_code="create_unconfirmed")
        session = _session_of(job.credential)
        if session is None:
            return Result(Outcome.FATAL, error_code="credential_unreadable")
        cost = job.offer.cost_usd
        if cost is None or cost <= 0:
            return Result(Outcome.FATAL, error_code=dr.COST_UNKNOWN)
        pol = job.offer.funding
        if pol is None or not pol.auto_recharge:
            return Result(Outcome.FATAL, error_code=dr.FUNDING_CAP_REACHED)
        try:
            if await self.points(fresh=True) < 1:
                return Result(Outcome.FATAL, error_code="points_exhausted")
        except SupplierError as exc:
            return _refused(exc)
        need = need_usd(cost, pol.zzshu_margin_pct)
        picked = job.offer.params.get(dr.PICKED_CARD_KEY)
        card_id = str(picked) if isinstance(picked, int) and not isinstance(picked, bool) else None
        last4: str | None = None
        loaded: Decimal | None = None
        opened: tuple[str, Decimal] | None = None
        if card_id is None:  # a retry reuses the card it opened (the engine hands its id back)
            try:
                dc = await self.funding.open_dedicated_card(need, pol, f"zzopen:{job.job_id}")
            except SupplierError as exc:
                return _refused(exc)
            card_id, last4, loaded, opened = dc.id, dc.last4, dc.loaded, (dc.id, dc.spent)

        def out(outcome: Outcome, code: str | None = None, **kw: Any) -> Result:
            return Result(
                outcome,
                error_code=code,
                card_id=card_id,
                card_last4=last4,
                card_loaded=loaded,
                card_opened=opened,
                **kw,
            )

        if opened is not None:
            # security review of #113: the card id must be in the job row BEFORE anything is sent,
            # or a crash / lost lease here would leave a funded card nobody knows about. The engine
            # records it now and hands it back on the next pass (which is still "never sent").
            return out(Outcome.RETRYABLE, CARD_OPENED)
        rd = await self.funding.read_dedicated_card(card_id)
        if rd is None or rd.secret is None or rd.status != "ACTIVE" or rd.available < cost:
            # nothing sent: the card is kept, the engine retries and finds it again by id
            return out(Outcome.RETRYABLE, "card_unreadable")
        last4 = last4 or rd.last4
        body: dict[str, Any] | None = {
            "orderType": "direct",
            "cardNumber": rd.secret.pan,
            "expMonth": rd.secret.month,
            "expYear": rd.secret.year,
            "cvv": rd.secret.cvv,
            "token": session,
            "planType": spec.plan_type,
            "region": spec.region,
        }
        del rd, session
        try:
            resp = await self.client.request("POST", "/orders/direct", json_body=body, creates=True)
        except SupplierError as exc:
            body = None
            if exc.outcome in (Outcome.UNAVAILABLE, Outcome.FATAL):
                # a definite refusal: nothing was created, the card goes back to the wallet
                return await self._finish(out, exc.reason, card_id, supplier_stop=exc.supplier_stop)
            return out(exc.outcome, exc.reason, retry_after=exc.retry_after)
        finally:
            body = None  # drop the card and the session at once
        self._points_hit = None
        data = resp.data if isinstance(resp.data, Mapping) else {}
        ref = data.get("card_key")
        if not isinstance(ref, str) or not _REF_RE.fullmatch(ref):
            # something may exist but we cannot name it: never a blind second create
            return out(Outcome.AMBIGUOUS, "malformed_order")
        status = str(data.get("status") or "processing")
        return out(
            Outcome.ACCEPTED_PENDING,
            None,
            upstream_ref=ref,
            account_mask=mask_email(data.get("email")),
            detail=_VENDOR_STATUS.get(status, "running"),
            vendor=progress.vendor_view({"status": _VENDOR_STATUS.get(status, "running")}),
        )

    async def _finish(
        self, out: Any, code: str, card_id: str, *, supplier_stop: bool = False
    ) -> Result:
        """A definite refusal of the create: the card goes back to the wallet, staff decide."""
        leftover = await self.funding.finish_dedicated_card(card_id)
        res: Result = out(
            Outcome.FATAL, code, card_leftover=leftover, card_swept=leftover is not None
        )
        return replace(res, supplier_stop=True) if supplier_stop else res

    async def query(self, job: JobSpec) -> Result:
        """Read the order by its ``card_key``. No key recorded = a create whose answer was lost:
        staff look at the zzshu history, it is never sent again."""
        spec = zzshu_spec(job.offer)
        if spec is None:
            return Result(Outcome.FATAL, error_code="invalid_offer")
        ref = job.upstream_ref
        picked = job.offer.params.get(dr.PICKED_CARD_KEY)
        card_id = str(picked) if isinstance(picked, int) and not isinstance(picked, bool) else None
        if not ref:
            # an unconfirmed create: the card may hold money zzshu took or will take; staff look
            return Result(Outcome.FATAL, error_code="create_unconfirmed", card_id=card_id)
        try:
            st = await self._status(ref)
            if st is not None and st.status == "failed" and self.status_recheck_s >= 0:
                await asyncio.sleep(self.status_recheck_s)  # docs: confirm a first ``failed``
                st = await self._status(ref)
        except SupplierError as exc:
            if exc.status == 404:
                return Result(Outcome.UNAVAILABLE, error_code="order_not_found", upstream_ref=ref)
            return Result.from_error(exc, upstream_ref=ref)
        if st is None:
            return Result(Outcome.AMBIGUOUS, error_code="malformed_order", upstream_ref=ref)
        if st.plan_type is not None and st.plan_type != spec.plan_type:
            return Result(Outcome.FATAL, error_code="order_mismatch", upstream_ref=ref)
        mask = mask_email(st.email) or job.account_mask
        if job.account_mask and mask and job.account_mask != mask:
            return Result(Outcome.AMBIGUOUS, error_code="order_mismatch", upstream_ref=ref)
        vstatus = _VENDOR_STATUS.get(st.status, "failed" if st.status == "failed" else "running")
        events: list[dict[str, Any]] = []
        if st.status == "failed":
            events.append(
                {"id": f"zzshu-failed-{ref}"[:64], "category": "zzshu", "public_code": "failed",
                 "public_message": st.failure}
            )  # fmt: skip
        vendor = progress.vendor_view({"status": vstatus}, events)
        common: dict[str, Any] = {
            "upstream_ref": ref, "account_mask": mask, "vendor": vendor, "card_id": card_id,
        }  # fmt: skip
        if st.status in ("pending", "processing"):
            return Result(Outcome.ACCEPTED_PENDING, detail=vstatus, **common)
        leftover = await self.funding.finish_dedicated_card(card_id) if card_id else None
        sweep = {"card_leftover": leftover, "card_swept": leftover is not None}
        if st.status == "success":
            detail = "completed" if st.cancelled else "completed:renew_unconfirmed"
            text = "----".join((f"gpt/{spec.plan}", mask or "-", st.order_no or ref))
            return Result(Outcome.OK, detail=detail, secrets=(text,), **common, **sweep)
        if st.status == "failed":
            return Result(
                Outcome.FATAL, error_code="order_failed", detail="failed", **common, **sweep
            )
        # an unknown status is never success: keep reading (the engine's pending bound ends it)
        return Result(Outcome.ACCEPTED_PENDING, detail="running", **common)

    async def _status(self, ref: str) -> _Status | None:
        resp = await self.client.request(
            "POST", "/orders/status", json_body={"cardKey": ref, "includeVerification": False}
        )
        data = resp.data if isinstance(resp.data, Mapping) else None
        if data is None:
            return None
        status = data.get("status")
        if not isinstance(status, str) or not status:
            return None
        plan_type = data.get("plan_type")
        no = data.get("order_no")
        st = _Status(
            status=status.lower()[:16],
            failure=progress.clean_message(data.get("failure_reason"), 120),
            cancelled=data.get("is_subscription_cancelled") in (1, True),
            email=data.get("email") if isinstance(data.get("email"), str) else None,
            plan_type=plan_type if isinstance(plan_type, str) else None,
            order_no=_NO_RE.sub("", str(no))[:40] if no is not None else None,
        )
        del data, resp  # the reply carried the full card number and the session
        return st

    async def void(self, job: JobSpec) -> Result:
        """zzshu orders cannot be cancelled through the API."""
        return Result(Outcome.FATAL, error_code="void_unsupported", upstream_ref=job.upstream_ref)

    def verify_webhook(self, raw: bytes, headers: Mapping[str, str]) -> list[SupplierEvent]:
        raise WebhookRejected("zzshu has no webhooks")


@dataclass(frozen=True)
class _Status:
    status: str
    failure: str | None
    cancelled: bool
    email: str | None
    plan_type: str | None
    order_no: str | None


def _refused(exc: SupplierError) -> Result:
    """A refusal BEFORE anything reached zzshu. A definite "no" is FATAL here (the engine only
    fails over from the pre-check, never after a send): staff at once, nothing was charged."""
    if exc.outcome is Outcome.UNAVAILABLE:
        return Result(Outcome.FATAL, error_code=exc.reason)
    return Result.from_error(exc)


def _session_of(text: str | None) -> dict[str, Any] | None:
    """The buyer's session JSON as the object zzshu wants (``token``). Memory only."""
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None
