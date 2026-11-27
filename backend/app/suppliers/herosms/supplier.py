"""HeroSMS as a ``suppliers`` row (ADR-057): probe, balance and the breaker come from the shared
supplier machinery. SMS orders are never run by the job engine, so the job calls answer FATAL;
the SMS flow (``app.procurement.sms``) uses :attr:`client` directly."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

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
from app.suppliers.herosms.client import HeroSmsClient, SmsError

NOT_A_JOB = "sms_not_a_job"


class HeroSmsSupplier:
    capabilities: frozenset[Capability] = frozenset()

    def __init__(self, code: str, client: HeroSmsClient) -> None:
        self.code = code
        self.env = client.cfg.env
        self.client = client

    async def aclose(self) -> None:
        await self.client.aclose()

    async def probe(self) -> ProbeReport:
        now = datetime.now(timezone.utc)
        try:
            bal = await self.client.balance()
        except SmsError as exc:
            return ProbeReport(
                supplier=self.code,
                env=self.env,
                ok=False,
                auth_ok=False if exc.outcome is Outcome.FATAL else None,
                errors=(exc.reason,),
                checked_at=now,
            )
        return ProbeReport(
            supplier=self.code,
            env=self.env,
            ok=True,
            auth_ok=True,
            permissions={"sms": True},
            balance=Balance(spendable=bal, currency="USD", total=bal),
            checked_at=now,
        )

    async def balance(self) -> Balance:
        bal = await self.client.balance()
        return Balance(spendable=bal, currency="USD", total=bal)

    async def availability(self, offer: OfferSpec) -> Availability:
        # SMS availability lives on sms_products (app.procurement.sms), not on supplier offers
        return Availability(False, reason=NOT_A_JOB, checked_at=datetime.now(timezone.utc))

    async def submit(self, job: JobSpec) -> Result:
        return Result(outcome=Outcome.FATAL, error_code=NOT_A_JOB)

    async def query(self, job: JobSpec) -> Result:
        return Result(outcome=Outcome.FATAL, error_code=NOT_A_JOB)

    async def void(self, job: JobSpec) -> Result:
        return Result(outcome=Outcome.FATAL, error_code=NOT_A_JOB)

    def verify_webhook(self, raw: bytes, headers: Mapping[str, str]) -> list[SupplierEvent]:
        raise WebhookRejected("the shop polls HeroSMS; no webhook")
