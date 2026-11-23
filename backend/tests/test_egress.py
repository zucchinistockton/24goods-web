"""app/payments/egress.py: direct first, relay only when the direct connection cannot be opened."""

from __future__ import annotations

import httpx
import pytest

from app.payments import egress


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    egress.reset_state()
    monkeypatch.setattr(egress, "relays", lambda: {"pay.example.cn": ("198.51.100.7", 20443)})
    yield
    egress.reset_state()


def never_dispatched(exc):
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


class Net:
    """Fake network: which URLs connect, what DNS returns, and a log of attempts."""

    def __init__(self, dns="203.0.113.1", up=()):
        self.dns, self.up, self.calls, self.lookups = dns, set(up), [], 0

    async def fresh_ip(self):
        self.lookups += 1
        return self.dns

    async def exchange(self, url, connect):
        self.calls.append((url, connect))
        if url in self.up:
            return {"ok": url}
        raise httpx.ConnectTimeout("no route")


DIRECT = "https://203.0.113.1/api.php"
RELAY = "https://198.51.100.7:20443/api.php"


async def send(net, host="pay.example.cn"):
    return await egress.send(
        scheme="https",
        host=host,
        port=443,
        path="/api.php",
        fresh_ip=net.fresh_ip,
        exchange=net.exchange,
        never_dispatched=never_dispatched,
    )


def test_parse_relays():
    got = egress.parse_relays("pay.a.cn=1.2.3.4:20443, b.cn=[::1]:9")
    assert got == {"pay.a.cn": ("1.2.3.4", 20443)}
    assert egress.parse_relays("") == {}


async def test_direct_first_and_address_remembered():
    net = Net(up={DIRECT})
    assert await send(net) == {"ok": DIRECT}
    assert await send(net) == {"ok": DIRECT}
    assert [u for u, _ in net.calls] == [DIRECT, DIRECT]
    assert net.lookups == 1  # second call reused the remembered address, no DNS


async def test_relay_only_when_direct_cannot_connect_and_then_sticky():
    net = Net(up={RELAY})
    assert await send(net) == {"ok": RELAY}
    assert net.calls == [(DIRECT, egress.FIRST_PATH_CONNECT_S), (RELAY, 1.5)]
    net.calls.clear()
    net.up = {DIRECT, RELAY}
    assert await send(net) == {"ok": RELAY}  # sticks with the relay while it works
    assert [u for u, _ in net.calls] == [RELAY]
    net.calls.clear()
    net.up = {DIRECT}  # relay fails -> direct, and direct becomes the preference again
    assert await send(net) == {"ok": DIRECT}
    assert net.calls == [(RELAY, egress.FIRST_PATH_CONNECT_S), (DIRECT, 1.5)]
    net.calls.clear()
    assert await send(net) == {"ok": DIRECT}
    assert [u for u, _ in net.calls] == [DIRECT]


async def test_no_fallback_after_the_request_may_have_been_sent():
    net = Net()

    async def exchange(url, connect):
        net.calls.append(url)
        raise httpx.ReadTimeout("sent, no answer")

    with pytest.raises(httpx.ReadTimeout):
        await egress.send(
            scheme="https",
            host="pay.example.cn",
            port=443,
            path="/api.php",
            fresh_ip=net.fresh_ip,
            exchange=exchange,
            never_dispatched=never_dispatched,
        )
    assert net.calls == [DIRECT]  # never retried over the relay


async def test_moved_address_is_found_by_a_fresh_lookup():
    net = Net(up={DIRECT})
    await send(net)
    net.dns, net.up = "203.0.113.9", {"https://203.0.113.9/api.php"}  # gateway moved, relay down
    assert await send(net) == {"ok": "https://203.0.113.9/api.php"}
    assert net.lookups == 2
    net.calls.clear()
    await send(net)
    assert [u for u, _ in net.calls] == ["https://203.0.113.9/api.php"]  # new address remembered


async def test_everything_down_raises_connect_error_and_without_relay_direct_only(monkeypatch):
    with pytest.raises(httpx.ConnectTimeout):
        await send(Net())
    monkeypatch.setattr(egress, "relays", lambda: {})
    net = Net(up=set())
    with pytest.raises(httpx.ConnectTimeout):
        await send(net, host="other.example.cn")
    assert len(net.calls) == 1 and net.calls[0][0] == DIRECT
    assert 1.9 < net.calls[0][1] <= 2.0  # half of the 4 s connect deadline (TCP + TLS phases)


async def test_dns_failure_still_tries_the_relay():
    net = Net(up={RELAY})

    async def no_dns():
        raise httpx.ConnectError("dns down")

    out = await egress.send(
        scheme="https",
        host="pay.example.cn",
        port=443,
        path="/api.php",
        fresh_ip=no_dns,
        exchange=net.exchange,
        never_dispatched=never_dispatched,
    )
    assert out == {"ok": RELAY}


async def test_connect_deadline_is_never_overrun(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(egress.time, "monotonic", lambda: clock[0])
    net = Net()

    async def slow_fail(url, connect):
        net.calls.append((url, connect))
        clock[0] += 2 * connect  # TCP + TLS phases both time out
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(httpx.ConnectTimeout):
        await egress.send(
            scheme="https",
            host="pay.example.cn",
            port=443,
            path="/api.php",
            fresh_ip=net.fresh_ip,
            exchange=slow_fail,
            never_dispatched=never_dispatched,
            connect_deadline_s=4.0,
        )
    assert clock[0] <= 4.0  # the caller's outer timeout (> deadline) never cuts a connect


async def test_remembered_address_expires(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(egress.time, "monotonic", lambda: clock[0])
    net = Net(up={DIRECT})
    await send(net)
    clock[0] += egress.IP_TTL_S + 1
    await send(net)
    assert net.lookups == 2


async def test_a_query_leaves_the_relay_enough_time_for_its_handshake():
    """2026-11-22 production: direct to Xiaodou blocked, relay TLS about 0.5 s. With the old 1.8 s
    query budget the relay got 0.4 s per phase and every status query failed; the query budget
    (zpay.QUERY_CONNECT_S) must leave the relay at least one second per connect phase."""
    from app.payments import zpay

    net = Net(up={RELAY})

    async def slow_relay(url, connect):
        net.calls.append((url, connect))
        if url == RELAY and connect is not None and connect >= 0.6:  # handshake needs ~0.5 s
            return {"ok": url}
        raise httpx.ConnectTimeout("too little time" if url == RELAY else "no route")

    got = await egress.send(
        scheme="https",
        host="pay.example.cn",
        port=443,
        path="/api.php",
        fresh_ip=net.fresh_ip,
        exchange=slow_relay,
        never_dispatched=never_dispatched,
        connect_deadline_s=zpay.QUERY_CONNECT_S,
    )
    assert got == {"ok": RELAY}
    assert net.calls[-1][1] >= 1.0
    assert zpay.QUERY_CONNECT_S < zpay.QUERY_TIMEOUT_S  # the outer timeout never cuts a connect


async def test_relay_only_skips_dns_and_direct_and_gives_the_relay_the_whole_budget(monkeypatch):
    """Owner 2026-11-23: production cannot reach Xiaodou directly; APP_ZPAY_RELAY_ONLY uses the
    relay alone: no lookup on this host, no direct attempt, half the connect budget per phase."""
    monkeypatch.setattr(egress, "relay_only", lambda: True)
    net = Net(up={DIRECT, RELAY})
    assert await send(net) == {"ok": RELAY}
    assert await send(net) == {"ok": RELAY}
    assert [u for u, _ in net.calls] == [RELAY, RELAY]
    assert net.lookups == 0
    assert net.calls[0][1] == pytest.approx(2.0, abs=0.05)  # default 4.0 s deadline / 2 phases
    # the relay is down: nothing else is tried, a connect error (provably not sent) comes back
    net2 = Net(up={DIRECT})
    with pytest.raises(httpx.ConnectTimeout):
        await send(net2)
    assert [u for u, _ in net2.calls] == [RELAY] and net2.lookups == 0


async def test_relay_only_does_nothing_for_a_host_without_a_relay(monkeypatch):
    monkeypatch.setattr(egress, "relay_only", lambda: True)
    net = Net(up={"https://203.0.113.1/api.php"})
    assert await send(net, host="other.example.cn") == {"ok": "https://203.0.113.1/api.php"}
