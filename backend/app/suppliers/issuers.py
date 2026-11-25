"""Ordered issuers of the dedicated card of a zzshu order (ADR-054, ADR-055).

``CardIssuers`` stands in for the single ZovoCard supplier a ``ZzshuSupplier`` used to hold: the
same few methods (wallet, open / read / finish a dedicated card), routed over an ordered list of
issuers. The order comes from the funding setting ``zzshu_card_issuers``
(``FundingPolicy.card_issuers``, supplier codes) and only names issuers configured in this
process.

Fallback rule: the next issuer is tried ONLY after a definite "nothing opened" answer (UNAVAILABLE,
or a coded refusal before any card exists). A RETRYABLE / AMBIGUOUS answer may mean a card exists:
it is raised as is, the engine retries with the same key, and a later issuer that answered
AMBIGUOUS / RETRYABLE is asked first on that retry (in memory; a restart forgets it and falls
back to the configured order, whose first issuer simply answers "nothing opened" again in
practice).

A card id belongs to its issuer: ``vm:<id>`` is vmcardio, a plain number is ZovoCard.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from app.suppliers.base import Balance, FundingPolicy, Outcome, SupplierError
from app.suppliers.vmcardio.supplier import is_vm_card
from app.suppliers.zovocard.supplier import DedicatedCard, DedicatedRead

log = logging.getLogger("app.suppliers.issuers")

STICKY_MAX = 256


def nothing_opened(exc: SupplierError) -> bool:
    """A definite refusal: no card exists, the next issuer may try."""
    if exc.outcome is Outcome.UNAVAILABLE:
        return True
    return exc.outcome is Outcome.FATAL and bool(exc.error_code)


class CardIssuers:
    """``issuers``: ``(supplier code, supplier)`` pairs in the adapter's default order."""

    def __init__(self, issuers: Sequence[tuple[str, Any]], *, owns: bool = False) -> None:
        if not issuers:
            raise ValueError("at least one issuer")
        self.issuers = list(issuers)
        self._owns = owns
        self._sticky: dict[str, str] = {}  # open key -> issuer code that answered AMBIGUOUS

    async def aclose(self) -> None:
        if self._owns:
            for _code, sup in self.issuers:
                await sup.aclose()

    def _ordered(self, pol: FundingPolicy | None) -> list[tuple[str, Any]]:
        names = pol.card_issuers if pol is not None else ()
        if not names:
            return list(self.issuers)
        by_code = dict(self.issuers)
        return [(c, by_code[c]) for c in names if c in by_code]

    def _of(self, card_id: str) -> Any:
        want = "vmcardio" if is_vm_card(card_id) else "zovocard"
        for _code, sup in self.issuers:
            if getattr(sup, "adapter_name", None) == want:
                return sup
        return None

    # ---- reads
    async def balance(self) -> Balance:
        return await self.issuers[0][1].balance()

    async def _wallet(self, fresh: bool) -> Decimal | None:
        """The most any issuer's wallet holds (None = none readable)."""
        best: Decimal | None = None
        for _code, sup in self.issuers:
            w = await sup._wallet(fresh)
            if w is not None and (best is None or w > best):
                best = w
        return best

    # ---- the dedicated card
    async def open_dedicated_card(
        self, need: Decimal, pol: FundingPolicy, key: str
    ) -> DedicatedCard:
        order = self._ordered(pol)
        if not order:
            raise SupplierError(
                "no card issuer configured", outcome=Outcome.UNAVAILABLE, error_code="no_issuer"
            )
        sticky = self._sticky.get(key)
        if sticky is not None:
            order = sorted(order, key=lambda e: e[0] != sticky)  # stable: the sticky one first
        first_error: SupplierError | None = None
        for code, sup in order:
            try:
                card = await sup.open_dedicated_card(need, pol, key)
            except SupplierError as exc:
                if not nothing_opened(exc):
                    if (
                        exc.outcome in (Outcome.AMBIGUOUS, Outcome.RETRYABLE)
                        and code != order[0][0]
                    ):
                        if len(self._sticky) >= STICKY_MAX:
                            self._sticky.clear()
                        self._sticky[key] = code
                    raise
                log.info("card issuer %s opened nothing reason=%s", code, exc.reason)
                first_error = first_error or exc
                continue
            self._sticky.pop(key, None)
            return card
        assert first_error is not None
        raise first_error

    async def read_dedicated_card(self, card_id: str) -> DedicatedRead | None:
        sup = self._of(card_id)
        return await sup.read_dedicated_card(card_id) if sup is not None else None

    async def finish_dedicated_card(self, card_id: str) -> Decimal | None:
        sup = self._of(card_id)
        return await sup.finish_dedicated_card(card_id) if sup is not None else None
