"""One Redis pattern subscription per process, fanned out to in-memory SSE queues."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.errors import RateLimited, Unavailable
from app.modules.realtime.publish import PREFIX, PROC_PROGRESS_CHANNEL

log = logging.getLogger("app.realtime")

HEARTBEAT_S = 15.0
QUEUE_MAX = 200
MAX_SUBSCRIBERS = 256
MAX_TENANT_SUBSCRIBERS = 64
MAX_ACCOUNT_SUBSCRIBERS = 4
MAX_IP_SUBSCRIBERS = 16
# In-flight procurement progress relays; more are dropped (the page refetches). Each relay opens a
# DB session, so keep this far below the API pool (db_pool_size + db_max_overflow = 8): a flood on
# rt:procprogress must never starve real API requests (re-check of #47).
MAX_RELAYS = 2


class Subscription:
    def __init__(self, channels: Iterable[str]):
        self.channels = frozenset(channels)
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.overflowed = False
        self.evicted = False  # replaced by a newer stream of the same account
        self.budget_keys: tuple[tuple[str, str], ...] = ()
        self.account: str | None = None

    def offer(self, item: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.overflowed = True  # slow client: it will be disconnected and re-sync


class RealtimeHub:
    def __init__(
        self,
        redis: Redis,
        on_progress: Callable[[bytes | str], Awaitable[object]] | None = None,
    ):
        self._redis = redis
        self._on_progress = on_progress  # API-side relay of the procurement channel
        self._relays: set[asyncio.Task[object]] = set()
        self._subs: dict[str, set[Subscription]] = {}
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._admitted: set[Subscription] = set()
        self._counts: dict[tuple[str, str], int] = {}
        self._by_account: dict[str, list[Subscription]] = {}  # oldest first

    def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="realtime-hub")

    async def wait_ready(self, timeout: float = 2.0) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        for t in list(self._relays):
            t.cancel()

    def subscribe(
        self,
        channels: Iterable[str],
        *,
        tenant: str | None = None,
        account: str | None = None,
        ip: str | None = None,
    ) -> Subscription:
        if account is not None and self._counts.get(("account", account), 0) >= (
            MAX_ACCOUNT_SUBSCRIBERS
        ):
            # the account's OLDEST stream makes room (a reload, a new tab, or a stream whose
            # client vanished behind a proxy and is not yet noticed): the newest connection of a
            # person always gets live updates instead of a 429 and the polling fallback. Only the
            # same account's own streams are ever closed this way; the cap itself stays.
            self._evict(self._by_account[account][0])
        if len(self._admitted) >= MAX_SUBSCRIBERS:
            raise Unavailable("Realtime capacity reached", headers={"Retry-After": "15"})
        budgets = (
            ("tenant", tenant, MAX_TENANT_SUBSCRIBERS),
            ("account", account, MAX_ACCOUNT_SUBSCRIBERS),
            ("ip", ip, MAX_IP_SUBSCRIBERS),
        )
        keys = []
        for kind, value, limit in budgets:
            if value is not None:
                key = (kind, value)
                if self._counts.get(key, 0) >= limit:
                    raise RateLimited(15, "Realtime connection limit reached")
                keys.append(key)
        self.ensure_started()
        sub = Subscription(channels)
        sub.budget_keys = tuple(keys)
        sub.account = account
        if account is not None:
            self._by_account.setdefault(account, []).append(sub)
        self._admitted.add(sub)
        for key in keys:
            self._counts[key] = self._counts.get(key, 0) + 1
        for ch in sub.channels:
            self._subs.setdefault(ch, set()).add(sub)
        return sub

    def _evict(self, sub: Subscription) -> None:
        """Close a stream from the server side: its generator stops at once (a wake-up item)."""
        sub.evicted = True
        self.unsubscribe(sub)
        with contextlib.suppress(asyncio.QueueFull):
            sub.queue.put_nowait({"event": "_evicted"})

    def unsubscribe(self, sub: Subscription) -> None:
        if sub.account is not None:
            mine = self._by_account.get(sub.account)
            if mine is not None and sub in mine:
                mine.remove(sub)
                if not mine:
                    self._by_account.pop(sub.account, None)
        if sub in self._admitted:
            self._admitted.remove(sub)
            for key in sub.budget_keys:
                n = self._counts[key] - 1
                if n:
                    self._counts[key] = n
                else:
                    self._counts.pop(key)
        for ch in sub.channels:
            subs = self._subs.get(ch)
            if subs is not None:
                subs.discard(sub)
                if not subs:
                    self._subs.pop(ch, None)

    def _relay(self, raw: bytes | str) -> None:
        if self._on_progress is None or len(self._relays) >= MAX_RELAYS:
            return
        task = asyncio.create_task(self._on_progress(raw), name="progress-relay")
        self._relays.add(task)
        task.add_done_callback(self._relays.discard)

    def dispatch(self, channel: str, raw: bytes | str) -> None:
        if channel == PROC_PROGRESS_CHANNEL:
            self._relay(raw)
            return
        subs = self._subs.get(channel)
        if not subs:
            return
        try:
            item = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(item, dict) or "event" not in item:
            return
        for sub in list(subs):
            sub.offer(item)

    async def _run(self) -> None:
        backoff = 0.5
        while True:
            pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.psubscribe(PREFIX + "*")
                self._ready.set()
                backoff = 0.5
                while True:
                    msg = await pubsub.get_message(timeout=30.0)
                    if msg is None or msg.get("type") != "pmessage":
                        continue
                    channel = msg["channel"]
                    if isinstance(channel, bytes):
                        channel = channel.decode()
                    self.dispatch(channel, msg["data"])
            except asyncio.CancelledError:
                raise
            except (RedisError, OSError) as exc:
                self._ready.clear()
                log.warning("realtime hub reconnecting error=%s", type(exc).__name__)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()


async def sse_events(
    hub: RealtimeHub,
    sub: Subscription,
    heartbeat_s: float = HEARTBEAT_S,
    alive: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    """Authorization has a monotonic deadline independent of queue traffic.
    Re-check after waiting and before forwarding, so a busy stream cannot defer revocation."""
    deadline = time.monotonic() + heartbeat_s
    try:
        yield "retry: 4000\n\n"
        while not sub.overflowed and not sub.evicted:
            if time.monotonic() >= deadline:
                if alive is not None and not await alive():
                    break
                deadline = time.monotonic() + heartbeat_s
            try:
                item = await asyncio.wait_for(
                    sub.queue.get(), max(0.001, deadline - time.monotonic())
                )
            except asyncio.TimeoutError:
                if alive is not None and not await alive():
                    break
                deadline = time.monotonic() + heartbeat_s
                yield ": ping\n\n"
                continue
            if sub.evicted:
                break  # a newer stream of the same account took this one's place
            if time.monotonic() >= deadline:
                if alive is not None and not await alive():
                    break
                deadline = time.monotonic() + heartbeat_s
            data = json.dumps(item.get("data"), separators=(",", ":"), default=str)
            yield f"event: {item['event']}\ndata: {data}\n\n"
    finally:
        hub.unsubscribe(sub)
