"""Manual "supplier": staff fulfil by hand. Every job step answers needs-manual.

Keeps the manual exit a first-class source (review-v12 §0: automation never removes it). It
holds no credentials and never makes a network call.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal

from app.suppliers.base import (
    Availability,
    Balance,
    Capability,
    JobSpec,
    OfferSpec,
    Outcome,
    ProbeReport,
    Result,
    SupplierEvent,
    WebhookRejected,
)

NEEDS_MANUAL = "manual_required"


class ManualSupplier:
    capabilities: frozenset[Capability] = frozenset()

    def __init__(self, code: str = "manual", *, env: str = "production") -> None:
        self.code = code
        self.env = env

    async def aclose(self) -> None:
        return None

    async def probe(self) -> ProbeReport:
        return ProbeReport(
            supplier=self.code,
            env=self.env,
            ok=True,
            permissions={"manual": True},
            checked_at=datetime.now(timezone.utc),
        )

    async def balance(self) -> Balance:
        return Balance(spendable=Decimal(0), currency="USD")

    async def availability(self, offer: OfferSpec) -> Availability:
        return Availability(True, checked_at=datetime.now(timezone.utc))

    async def submit(self, job: JobSpec) -> Result:
        return Result(outcome=Outcome.FATAL, error_code=NEEDS_MANUAL)

    async def query(self, job: JobSpec) -> Result:
        return Result(outcome=Outcome.FATAL, error_code=NEEDS_MANUAL)

    async def void(self, job: JobSpec) -> Result:
        return Result(outcome=Outcome.FATAL, error_code=NEEDS_MANUAL)

    def verify_webhook(self, raw: bytes, headers: Mapping[str, str]) -> list[SupplierEvent]:
        raise WebhookRejected("the manual supplier has no webhooks")


__all__ = ["NEEDS_MANUAL", "ManualSupplier"]
