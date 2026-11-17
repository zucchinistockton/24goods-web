"""Owner decision 2026-11-17: virtual bank cards are no longer sold (``vcc_sales_enabled``).

Buyers misused cards for $0 authorisations / card-binding checks. While the setting is false:
admin cannot create or activate an ``upstream_vcc`` product, the storefront does not list or
offer one, and no order-creating path accepts one (422 ``vcc_not_sellable``). The engine code
stays dormant and testable. Separately and unconditionally, card data is never revealed to a
buyer (see ``orders.service.reveal_for`` and ``delivery.reveal``).
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, true

from app.core.config import Settings, get_settings
from app.core.errors import Validation
from app.models import Product

VCC_MODE = "upstream_vcc"
CODE = "vcc_not_sellable"


def vcc_sales_enabled(settings: Settings | None = None) -> bool:
    return bool((settings or get_settings()).vcc_sales_enabled)


def ensure_sellable(settings: Settings | None, delivery_mode: str | None) -> None:
    if delivery_mode == VCC_MODE and not vcc_sales_enabled(settings):
        raise Validation("Virtual cards are not sold", code=CODE)


def sellable(settings: Settings | None = None) -> ColumnElement[bool]:
    """WHERE clause hiding virtual-card products from storefront reads."""
    if vcc_sales_enabled(settings):
        return true()
    return Product.delivery_mode != VCC_MODE
