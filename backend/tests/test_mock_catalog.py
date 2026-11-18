"""The simulator's full catalogue switch (demo showcase): every documented GPT direct plan on
sale; the default fixture the other tests rely on stays unchanged."""

from app.devtools.mock_zovocard import plans_fixture
from app.suppliers.zovocard import direct as dr

DIRECT_PLANS = {"go", "plus", "pro_5x", "pro_20x", "pro_50x"}


def _on_sale(payload: dict) -> set[str]:
    return {
        r["key"]
        for r in payload["registry"]
        if r.get("purchasable") is True and payload["plans"][r["acc_plan_key"]]["enabled"] is True
    }


def test_default_catalogue_unchanged() -> None:
    p = plans_fixture("gpt")
    assert _on_sale(p) == {"go", "plus"}
    assert p["plans"]["pro_20x"]["enabled"] is False
    assert "pro_5x" not in p["plans"] and "pro_50x" not in p["plans"]


def test_full_catalogue_sells_every_direct_plan() -> None:
    p = plans_fixture("gpt", scenarios=True, full=True)
    assert DIRECT_PLANS | {"plus_half"} <= _on_sale(p)
    assert {r["country"] for r in p["payment_regions"]} == {"PH", "US", "JP", "CL", "EG"}
    # only Plus has a half-price key
    assert [k for k in _on_sale(p) if dr.is_half_plan(k)] == ["plus_half"]


def test_full_catalogue_is_gpt_only() -> None:
    assert plans_fixture("claude", full=True) == plans_fixture("claude")
