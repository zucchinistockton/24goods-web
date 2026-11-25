"""card.zzshu.pro adapter (ADR-054): standby provider for ChatGPT direct top-ups."""

from app.suppliers.zzshu.client import PRODUCTION_BASE_URL, ZzshuClient, ZzshuConfig
from app.suppliers.zzshu.supplier import ZzshuSupplier, zzshu_spec

__all__ = ["PRODUCTION_BASE_URL", "ZzshuClient", "ZzshuConfig", "ZzshuSupplier", "zzshu_spec"]
