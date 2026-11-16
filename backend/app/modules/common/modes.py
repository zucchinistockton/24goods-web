"""Delivery modes and how each code path treats them (review-v12 M2).

Framework-free: shared by the API and the delivery worker.

* ``auto_card``    sealed codes from the local pool (card_stock), revealed by delivery.
* ``manual``       staff deliver by hand.
* ``upstream_cdk`` a code bought from a supplier per order unit by the procurement process,
                   sealed into card_stock as ``sold`` for the order, then revealed like
                   auto_card. Never claims local stock.
"""

from __future__ import annotations

DELIVERY_MODES = ("auto_card", "manual", "upstream_cdk")
UPSTREAM_MODES = ("upstream_cdk",)
# deliveries are sealed card_stock rows the buyer reveals through delivery
CODE_MODES = ("auto_card", "upstream_cdk")
# the storefront shows a numeric stock (cards in the pool / supplier availability cap)
STOCKED_MODES = ("auto_card", "upstream_cdk")
# one supplier job per unit; small cap so a single order cannot drain the supplier balance
MAX_UPSTREAM_QTY = 5
