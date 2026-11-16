"""Delivery modes and how each code path treats them (review-v12 M2).

Framework-free: shared by the API and the delivery worker.

* ``auto_card``    sealed codes from the local pool (card_stock), revealed by delivery.
* ``manual``       staff deliver by hand.
* ``upstream_cdk`` a code bought from a supplier per order unit by the procurement process,
                   sealed into card_stock as ``sold`` for the order, then revealed like
                   auto_card. Never claims local stock.
* ``upstream_vcc`` a virtual card bought from a supplier per order unit (open, optional top-up,
                   reveal). PAN / expiry / CVV are ONE sealed secret ``<pan>----<MM/YY>----<cvv>``
                   in card_stock (``sold`` for the order), revealed like any card code.
"""

from __future__ import annotations

DELIVERY_MODES = ("auto_card", "manual", "upstream_cdk", "upstream_vcc")
UPSTREAM_MODES = ("upstream_cdk", "upstream_vcc")
# deliveries are sealed card_stock rows the buyer reveals through delivery
CODE_MODES = ("auto_card", "upstream_cdk", "upstream_vcc")
# the storefront shows a numeric stock (cards in the pool / supplier availability cap)
STOCKED_MODES = ("auto_card", "upstream_cdk", "upstream_vcc")
# one supplier job per unit; small cap so a single order cannot drain the supplier balance
MAX_UPSTREAM_QTY = 5
