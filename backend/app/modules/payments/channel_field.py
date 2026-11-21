"""Request field naming a payment channel (kept out of ``channels.py``: the delivery process imports
that module and must not load pydantic)."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator

from app.modules.payments.channels import LABELS


def _known(v: str) -> str:
    if v not in LABELS:
        raise ValueError("unknown payment channel")
    return v


# any channel of the catalogue (whether it is switched on is checked later)
PayChannel = Annotated[str, AfterValidator(_known)]
