"""Deterministic in-memory supplier for tests and development (never in production).

Behaviour is scripted: ``outcomes`` is a queue of :class:`Outcome` values consumed by
``submit``; when it is empty ``submit`` answers OK. Every submit is recorded per idempotency key,
and a resubmit with the same key replays the first OK result (like a real idempotent vendor), so
engine tests can assert "never charged twice".
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal

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
    SupplierEvent,
    WebhookRejected,
)


class MockSupplier:
    capabilities = frozenset({Capability.CDK, Capability.VCC, Capability.DIRECT, Capability.VOID})

    def __init__(
        self,
        code: str = "mock",
        *,
        env: str = "sandbox",
        spendable: Decimal = Decimal("100.00"),
        purchasable: Iterable[str] = ("plus",),
        outcomes: Iterable[Outcome] = (),
        unit_cost: Decimal = Decimal("1.00"),
        redemption: Money | None = None,
        card_products: Iterable[str] = ("MOCKCARD",),
    ) -> None:
        self.card_products = set(card_products)
        self.redemption = redemption
        self.code = code
        self.env = env
        self.spendable = spendable
        self.purchasable = set(purchasable)
        self.outcomes: deque[Outcome] = deque(outcomes)
        self.unit_cost = unit_cost
        self.charged: dict[str, Result] = {}  # idempotency key -> first OK result
        self.calls: list[tuple[str, str]] = []  # (operation, idempotency key)
        # direct: whether each submit carried a credential (the credential itself is never kept)
        self.credentials_seen: list[bool] = []

    async def aclose(self) -> None:
        return None

    async def probe(self) -> ProbeReport:
        self.calls.append(("probe", ""))
        return ProbeReport(
            supplier=self.code,
            env=self.env,
            ok=True,
            auth_ok=True,
            ip_allowed=True,
            permissions={"cdk": True},
            balance=await self.balance(),
            purchasable_plans=len(self.purchasable),
            card_products=0,
            checked_at=datetime.now(timezone.utc),
        )

    async def balance(self) -> Balance:
        return Balance(spendable=self.spendable, currency="USD", total=self.spendable)

    async def redemption_cost(self, offer: OfferSpec) -> Money | None:
        return self.redemption  # tests: None = "use the offer's configured cost"

    async def availability(self, offer: OfferSpec) -> Availability:
        plan = offer.params.get("plan")
        if offer.kind == "vcc":
            ok = offer.params.get("product_code") in self.card_products
        else:
            ok = (
                offer.kind in ("cdk", "direct")
                and isinstance(plan, str)
                and plan in self.purchasable
            )
        return Availability(
            available=ok,
            reason=None if ok else "not_purchasable",
            cost_estimate=Money(self.unit_cost, "USD") if ok else None,
            checked_at=datetime.now(timezone.utc),
        )

    def _code_for(self, key: str) -> str:
        return "MOCK-" + hashlib.sha256(key.encode()).hexdigest()[:20].upper()

    async def submit(self, job: JobSpec) -> Result:
        self.calls.append(("submit", job.idempotency_key))
        if job.idempotency_key in self.charged:
            return self.charged[job.idempotency_key]
        outcome = self.outcomes.popleft() if self.outcomes else Outcome.OK
        if outcome is not Outcome.OK:
            return Result(outcome=outcome, error_code=f"mock_{outcome.value}")
        self.spendable -= self.unit_cost
        if job.offer.kind == "direct":
            self.credentials_seen.append(bool(job.credential))
            ref = "mock:" + job.idempotency_key
            result = Result(
                outcome=Outcome.OK,
                upstream_ref=ref,
                secrets=(f"mock/{job.offer.params.get('plan')}----m***@example.test----{ref}",),
                cost=Money(self.unit_cost, "USD"),
                account_mask="m***@example.test",
            )
        elif job.offer.kind == "vcc":
            # one sealed structured secret per card: "<pan>----<MM/YY>----<cvv>" (synthetic)
            digest = hashlib.sha256(job.idempotency_key.encode()).hexdigest()
            pan = "4" + "".join(str(int(c, 16) % 10) for c in digest[:15])
            secret = "----".join((pan, "12/30", str(int(digest[20:23], 16) % 900 + 100)))
            result = Result(
                outcome=Outcome.OK,
                upstream_ref="mock:" + job.idempotency_key,
                secrets=(secret,),
                cost=Money(self.unit_cost, "USD"),
                card_mask=f"{pan[:6]}****{pan[-4:]}",
            )
        else:
            result = Result(
                outcome=Outcome.OK,
                upstream_ref="mock:" + job.idempotency_key,
                secrets=(self._code_for(job.idempotency_key),),
                cost=Money(self.unit_cost, "USD"),
            )
        self.charged[job.idempotency_key] = result
        return result

    async def query(self, job: JobSpec) -> Result:
        self.calls.append(("query", job.idempotency_key))
        hit = self.charged.get(job.idempotency_key)
        if hit is not None:
            return hit
        return Result(outcome=Outcome.UNAVAILABLE, error_code="mock_not_found")

    async def void(self, job: JobSpec) -> Result:
        self.calls.append(("void", job.idempotency_key))
        ref = job.upstream_ref or ""
        issued_key = ref.removeprefix("mock:") if ref.startswith("mock:") else job.idempotency_key
        if self.charged.pop(issued_key, None) is None:
            return Result(outcome=Outcome.FATAL, error_code="mock_not_found")
        return Result(outcome=Outcome.OK, upstream_ref="mock:" + issued_key)

    def verify_webhook(self, raw: bytes, headers: Mapping[str, str]) -> list[SupplierEvent]:
        raise WebhookRejected("the mock supplier sends no webhooks")


__all__ = ["MockSupplier"]
