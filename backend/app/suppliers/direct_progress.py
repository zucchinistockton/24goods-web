"""Progress of an ``upstream_direct`` order / CDK redemption (issue #30, review-v12 §22).

Pure functions, no I/O, shared by the procurement process (records the staff timeline), the API
(buyer view) and the redemption proxy (``progress`` of a result read).

* :func:`map_progress` maps OUR job state plus the vendor's ``status`` / ``stage`` /
  ``payment_state`` / ``funding_state`` to one buyer-facing step. It never raises: an unknown
  vendor value maps to the nearest SAFE step (never to "completed", never to "failed").
* :func:`vendor_view` reduces a vendor order + events to a whitelist; :func:`record` appends
  the CHANGES to a job's capped, de-duplicated staff timeline. Free vendor text
  (``public_message``) is sanitised by :func:`clean_message`.

Vendor vocabulary beyond the documented order statuses (vendor §6.18, §7.5) is inferred and
marked [I] below.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# the buyer-facing steps in display order, with the percentage each stands for
STEP_PERCENT: dict[str, int] = {
    "paid": 10,
    "half_check": 15,
    "waiting_supplier": 20,
    "sent": 30,
    "queued": 40,
    "funding": 55,
    "processing": 70,
    "payment_submitted": 85,
    "completed": 100,
}
OUR_STEPS = ("paid", "half_check", "waiting_supplier", "sent")
VENDOR_STEPS = ("queued", "funding", "processing", "payment_submitted", "completed")
FAILURE_STEPS = ("failed", "refunded", "attention")
ALL_STEPS = (*STEP_PERCENT, *FAILURE_STEPS)

MAX_ENTRIES = 200
MAX_SEEN = 400
MSG_CAP = 160

# vendor order status (documented, vendor §6.18.5 / §7.5)
_QUEUED = frozenset({"queued", "awaiting_card"})
_FUNDING = frozenset({"funding_pending"})
_PROCESSING = frozenset({"dispatching", "running", "requires_action", "pending"})
_PAID = frozenset({"plus_paid"})
_DONE = frozenset({"completed"})
_FAIL_PAYMENT = frozenset({"declined", "failed_precharge"})
_FAIL_CANCEL = frozenset({"cancelled"})
_FAIL_OTHER = frozenset({"failed", "rejected", "expired"})
_FAILED = _FAIL_PAYMENT | _FAIL_CANCEL | _FAIL_OTHER

_RANK = {s: i for i, s in enumerate(VENDOR_STEPS)}
_CTRL = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")
# Every pattern below is linear: the input is truncated first (see ``clean_message``), URL- and
# address-like TOKENS are dropped as a whole with fixed-literal searches (no nested or overlapping
# ``\S+`` runs, so no polynomial backtracking), and the digit run is bounded.
_TLD = re.compile(r"[a-z0-9-]\.(?:com|net|org|io|cn|me|co|app|xyz|top)(?![a-z0-9])")
_SCHEME = re.compile(r"(?:https?|ftp|wss?)://|(?:^|[^a-z0-9])www\.")
_DIGITS = re.compile(r"\d[\d \-]{6,40}\d")
_LONG = re.compile(r"[A-Za-z0-9_\-+/=]{24,}")
_CODE = re.compile(r"[^A-Za-z0-9_.:-]")
_FIELDS = ("status", "stage", "payment_state", "funding_state")


def _link_like(token: str) -> bool:
    t = token.lower()
    return "@" in t or "://" in t or bool(_SCHEME.search(t)) or bool(_TLD.search(t))


def clean_message(value: Any, cap: int = MSG_CAP) -> str | None:
    """Vendor free text made safe to store and show staff: control characters removed, no URLs,
    e-mail addresses, long digit runs (card numbers) or token-like strings, length-capped.
    The input is cut to ``4 * cap`` characters BEFORE any pattern runs (bounded work)."""
    if not isinstance(value, str):
        return None
    s = _CTRL.sub(" ", value[: 4 * cap])
    s = " ".join(t for t in s.split() if not _link_like(t))
    s = _DIGITS.sub("****", s)
    s = _LONG.sub("", s)
    s = " ".join(s.split())[:cap].strip()
    return s or None


def clean_code(value: Any, cap: int = 48) -> str | None:
    """A machine token ``[A-Za-z0-9_.:-]`` (vendor status / stage / public_code)."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    s = _CODE.sub("_", str(value)[: 4 * cap].strip())[:cap]
    return s or None


def _low(value: Any) -> str:
    return (clean_code(value) or "").lower()


@dataclass(frozen=True)
class Progress:
    step: str  # one of ALL_STEPS
    percent: int  # 0-100
    terminal: bool  # nothing more will change (completed / failed / refunded)
    outcome: str  # active | completed | failed | attention
    reason: str | None = None  # coarse buyer-safe reason key (failed / attention only)


def vendor_rank(
    status: Any, stage: Any = None, payment_state: Any = None, funding_state: Any = None
) -> str:
    """The furthest NON-terminal vendor step the fields support. Unknown values contribute
    nothing; with nothing known the step is ``queued`` [I] (accepted by us, the vendor has not
    said more). ``completed`` is only ever reached through ``status`` (see :func:`map_progress`)."""
    s, g, p, f = _low(status), _low(stage), _low(payment_state), _low(funding_state)
    best = 0  # index into VENDOR_STEPS; 0 = queued
    if s in _FUNDING:
        best = max(best, _RANK["funding"])
    elif s in _PROCESSING or "review" in s:  # [I] the "various review" statuses keep processing
        best = max(best, _RANK["processing"])
    elif s in _PAID or s in _DONE:
        best = max(best, _RANK["payment_submitted"])
    # [I] stage substrings (the vendor documents only cdk_accepted and completed)
    if "fund" in g or "card_select" in g or "awaiting_card" in g:
        best = max(best, _RANK["funding"])
    if any(k in g for k in ("dispatch", "running", "processing", "action", "pending")):
        best = max(best, _RANK["processing"])
    if "paid" in g or "payment_submitted" in g or "submitted" in g:
        best = max(best, _RANK["payment_submitted"])
    # [I] funding_state: card_ready = the card is ready, work goes on
    if f in ("card_ready", "ready", "funded", "settled", "held"):
        best = max(best, _RANK["processing"] if f != "held" else _RANK["funding"])
    elif f:
        best = max(best, _RANK["funding"])
    # [I] payment_state: anything that says money moved / was submitted
    if any(k in p for k in ("submitted", "paid", "charged", "settled", "success", "complete")):
        best = max(best, _RANK["payment_submitted"])
    return VENDOR_STEPS[min(best, _RANK["payment_submitted"])]


def failure_reason(status: Any) -> str:
    s = _low(status)
    if s in _FAIL_PAYMENT:
        return "declined"
    if s in _FAIL_CANCEL:
        return "cancelled"
    if s == "expired":
        return "expired"
    return "not_completed"


def vendor_problem(code: Any, msg: Any = None) -> str | None:
    """Coarse buyer-safe class of a vendor failure event (owner 2026-11-25): ``account_flagged``
    = OpenAI's risk control refused the account (``checkout_unusual_activity``: use another
    account or a fresh session); ``session`` = the session was refused, expired or answered 401
    (it may be single-use after that: the buyer needs a fresh one). ``None`` otherwise."""
    c = _low(code)
    m = str(msg or "").strip().lower()
    if "unusual" in c or "risk" in c:
        return "account_flagged"
    if any(k in c for k in ("session", "token", "login", "unauthor", "credential")) or (
        m.endswith(":401") or m.endswith(" 401") or m == "401"
    ):
        return "session"
    return None


def _last_failure(doc: Mapping[str, Any]) -> tuple[Any, Any]:
    """(code, msg) of the latest ``failed`` vendor event in a job's progress document."""
    events = doc.get("events")
    if not isinstance(events, list):
        return None, None
    for e in reversed(events):
        if isinstance(e, Mapping) and e.get("cat") == "failed" and (e.get("code") or e.get("msg")):
            return e.get("code"), e.get("msg")
    return None, None


def _attention_reason(error_code: str | None) -> str:
    c = (error_code or "").lower()
    return (
        "account"
        if any(k in c for k in ("session", "credential", "account", "email"))
        else "review"
    )


def map_progress(
    *,
    job_state: str | None,
    error_code: str | None = None,
    waiting_supplier: bool = False,
    half: bool = False,
    status: Any = None,
    stage: Any = None,
    payment_state: Any = None,
    funding_state: Any = None,
    sent: bool = False,
    order_refunded: bool = False,
    vendor_reason: str | None = None,
) -> Progress:
    """(our job state + vendor fields) -> the buyer's step. See the ADR in architecture.md.
    ``vendor_reason`` (:func:`vendor_problem` of the latest failed vendor event) replaces the
    generic reason of a failed / held job so the buyer is told what to do.

    ``job_state`` is the procurement job state (``queued | submitting | accepted_pending |
    needs_manual | fulfilled | failed | voided | void_pending``) or None while no job exists.
    ``sent``: the job has been sent to the vendor (reference recorded or first send time set).
    """
    js = (job_state or "").lower()
    vs = _low(status)
    if js == "fulfilled":
        return Progress("completed", 100, True, "completed")
    if order_refunded:
        return Progress(
            "refunded",
            _percent_before(status, stage, payment_state, funding_state, sent),
            True,
            "failed",
            "refunded",
        )
    base = _percent_before(status, stage, payment_state, funding_state, sent)
    if js in ("failed", "voided", "void_pending") or (js == "needs_manual" and vs in _FAILED):
        return Progress(
            "failed",
            base,
            True,
            "failed",
            vendor_reason or (failure_reason(status) if vs in _FAILED else "not_completed"),
        )
    if js == "needs_manual":
        return Progress(
            "attention", base, False, "attention", vendor_reason or _attention_reason(error_code)
        )
    if not js:  # paid, no job yet
        return Progress("paid", STEP_PERCENT["paid"], False, "active")
    if waiting_supplier or (js == "queued" and (error_code or "") == "waiting_for_card"):
        return Progress("waiting_supplier", STEP_PERCENT["waiting_supplier"], False, "active")
    if not sent:
        step = "half_check" if half else "paid"  # not sent yet
        return Progress(step, STEP_PERCENT[step], False, "active")
    if not (vs or _low(stage) or _low(payment_state) or _low(funding_state)):
        return Progress("sent", STEP_PERCENT["sent"], False, "active")
    step = vendor_rank(status, stage, payment_state, funding_state)
    # a vendor "completed" is only a hint: the buyer sees completion once OUR job is fulfilled
    return Progress(step, STEP_PERCENT[step], False, "active")


def _percent_before(
    status: Any, stage: Any, payment_state: Any, funding_state: Any, sent: bool
) -> int:
    """Percent of the furthest step reached before a terminal / attention outcome."""
    if not (_low(status) or _low(stage) or _low(payment_state) or _low(funding_state)):
        return STEP_PERCENT["sent"] if sent else STEP_PERCENT["paid"]
    vs = _low(status)
    probe = None if vs in _FAILED else status
    return STEP_PERCENT[vendor_rank(probe, stage, payment_state, funding_state)]


# ---- vendor answer -> whitelist ---------------------------------------------------------------


def _event_view(ev: Mapping[str, Any]) -> dict[str, Any] | None:
    tid = None
    for key in ("timeline_event_id", "id", "event_id"):  # [I] the id key of a ``data.events`` item
        v = ev.get(key)
        if isinstance(v, (str, int)) and not isinstance(v, bool):
            tid = clean_code(v, 64)
            if tid:
                break
    out = {
        "tid": tid,
        "cat": clean_code(ev.get("category"), 32),
        "step": clean_code(ev.get("step"), 48),
        "code": clean_code(ev.get("public_code")),
        "msg": clean_message(ev.get("public_message")),
        "to": clean_code(ev.get("to_status"), 32),
        "fact": clean_code(ev.get("payment_fact"), 32),
        "status": clean_code(ev.get("status"), 32),
    }
    if not tid:
        return None
    return {k: v for k, v in out.items() if v is not None}


def _event_time(ev: Any) -> str | None:
    if not isinstance(ev, Mapping):
        return None
    for key in ("occurred_at", "created_at", "at", "time"):
        v = ev.get(key)
        if isinstance(v, (str, int)) and not isinstance(v, bool):
            return str(v)[:40]
    return None


def _newest(events: list[Any], n: int) -> list[Any]:
    """The newest ``n`` vendor events, oldest first. The vendor's list order is not documented:
    sort by the event's own time when every item has one (stable, so ties keep list order),
    otherwise by its numeric id, otherwise assume oldest-first and take the tail."""
    items = events[-500:] if len(events) > 500 else list(events)
    times = [_event_time(e) for e in items]
    if items and all(t is not None for t in times) and len({type(e) for e in items}) == 1:
        order = sorted(range(len(items)), key=lambda i: (times[i] or "", i))
        return [items[i] for i in order][-n:]
    ids: list[int | None] = []
    for e in items:
        raw = None
        if isinstance(e, Mapping):
            for key in ("timeline_event_id", "id", "event_id"):
                if isinstance(e.get(key), (str, int)) and not isinstance(e.get(key), bool):
                    raw = e[key]
                    break
        ids.append(
            int(raw)
            if isinstance(raw, int) or (isinstance(raw, str) and raw.isdigit() and len(raw) < 18)
            else None
        )
    if items and all(i is not None for i in ids):
        order = sorted(range(len(items)), key=lambda i: (ids[i] or 0, i))
        return [items[i] for i in order][-n:]
    return items[-n:]


def vendor_view(order: Mapping[str, Any], events: Any = None) -> dict[str, Any]:
    """The whitelist of a vendor order read (``data.order`` + ``data.events``): four state fields
    and at most 50 sanitised timeline events. Nothing else of the body survives."""
    out: dict[str, Any] = {k: clean_code(order.get(k), 32) for k in _FIELDS}
    evs: list[dict[str, Any]] = []
    if isinstance(events, list):
        for ev in _newest(events, 50):
            if isinstance(ev, Mapping) and (v := _event_view(ev)) is not None:
                evs.append(v)
    out["events"] = evs
    return out


def vendor_from_webhook(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The same whitelist from a (normalised) webhook payload."""
    out: dict[str, Any] = {k: clean_code(payload.get(k), 32) for k in _FIELDS}
    ev = _event_view(payload) if payload.get("timeline_event_id") is not None else None
    out["events"] = [ev] if ev else []
    return out


# ---- timeline recording -----------------------------------------------------------------------


def _now_iso(now: datetime | None) -> str:
    return (
        (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _step_rank(v: Mapping[str, Any]) -> int:
    return _RANK[
        vendor_rank(v.get("status"), v.get("stage"), v.get("payment_state"), v.get("funding_state"))
    ]


def record(
    state: dict[str, Any] | None,
    vendor: Mapping[str, Any],
    source: str,
    now: datetime | None = None,
) -> tuple[dict[str, Any], int]:
    """Append what CHANGED to a job's progress document; returns (document, entries added).

    A state entry is added when any of status / stage / payment_state / funding_state differs from
    the last seen values; a timeline entry for each vendor event id not seen before. Identical
    consecutive entries are dropped; the list keeps the newest :data:`MAX_ENTRIES`."""
    doc: dict[str, Any] = dict(state or {})
    entries: list[dict[str, Any]] = list(doc.get("events") or [])
    seen: list[str] = list(doc.get("seen") or [])
    best = dict(doc.get("v") or {})  # the furthest state: what the BUYER's step is computed from
    last = dict(doc.get("raw") or best)  # the latest state: the staff timeline compares to this
    at = _now_iso(now)
    added = 0

    def push(entry: dict[str, Any]) -> None:
        nonlocal added
        core = {k: v for k, v in entry.items() if k not in ("at", "src")}
        if entries and {k: v for k, v in entries[-1].items() if k not in ("at", "src")} == core:
            return
        entries.append({"at": at, "src": source, **entry})
        added += 1

    cur = {k: vendor.get(k) for k in _FIELDS}
    if any(cur[k] is not None for k in _FIELDS) and any(cur[k] != last.get(k) for k in _FIELDS):
        # a field the vendor did not send keeps its last value (a webhook has no payment_state)
        merged = {k: cur[k] if cur[k] is not None else last.get(k) for k in _FIELDS}
        if merged != {k: last.get(k) for k in _FIELDS}:
            clean = {k: v for k, v in merged.items() if v is not None}
            push(clean)  # the staff timeline keeps every change, even a late lower one
            doc["raw"] = clean
            # a late or out-of-order answer, or a vendor ``failed`` on a still-active job, must
            # not LOWER the step the buyer sees: only an equal or higher rank replaces ``v``
            if not best or (
                _low(merged.get("status")) not in _FAILED
                and _step_rank(merged) >= _step_rank(best)
                and _step_rank({"status": merged.get("status")})
                >= _step_rank({"status": best.get("status")})
            ):
                doc["v"] = clean
    for ev in vendor.get("events") or []:
        tid = ev.get("tid")
        if not tid or tid in seen:
            continue
        seen.append(tid)
        push({**ev, "kind": "event"})
    doc["events"] = entries[-MAX_ENTRIES:]
    doc["seen"] = seen[-MAX_SEEN:]
    return doc, added


# ---- from a job row ---------------------------------------------------------------------------


def from_job(job: Any, *, half: bool = False, order_refunded: bool = False) -> Progress:
    """:func:`map_progress` for a ``ProcurementJob`` row (duck-typed: state, error_code,
    upstream_ref, first_sent_at, progress)."""
    doc = getattr(job, "progress", None) or {}
    v = doc.get("v") or {}
    status = v.get("status")
    if not v:  # ``upstream_status`` is ``status`` or ``op:status`` / ``status:fee_status``
        status = str(job.upstream_status or "").split(":", 1)[0] or None
    if job.state in ("failed", "voided", "void_pending", "needs_manual"):
        # the failure reason comes from the LATEST vendor status (``v`` never moves backwards)
        raw_status = (doc.get("raw") or {}).get("status")
        if _low(raw_status) in _FAILED:
            status = raw_status
    return map_progress(
        job_state=job.state,
        error_code=job.error_code,
        waiting_supplier=job.state == "queued" and job.error_code == "waiting_for_card",
        half=half,
        status=status,
        stage=v.get("stage"),
        payment_state=v.get("payment_state"),
        funding_state=v.get("funding_state"),
        sent=bool(job.upstream_ref or job.first_sent_at),
        order_refunded=order_refunded,
        vendor_reason=vendor_problem(*_last_failure(doc)),
    )


def stamp(doc: dict[str, Any] | None, step: str, now: datetime | None = None) -> dict[str, Any]:
    """Remember when ``step`` was first reached (``reached``) and return the new document."""
    out = dict(doc or {})
    reached = dict(out.get("reached") or {})
    if step not in reached:
        reached[step] = _now_iso(now)
    out["reached"] = reached
    return out


def buyer_view(
    p: Progress,
    *,
    half: bool = False,
    waited: bool = False,
    at: Mapping[str, Any] | None = None,
    skip: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The BUYER's progress document: step list with states, percent, outcome, coarse reason key
    and times. Only these keys leave the server; no vendor value, code or message is in it.

    ``at`` maps step -> ISO time (paid / half_check / waiting_supplier / sent from our rows,
    the vendor steps from ``progress.reached``). ``waited``: the order waited for a card once."""
    at = at or {}
    keys = [k for k in STEP_PERCENT if (k != "half_check" or half) and k not in skip]
    keys = [k for k in keys if k != "waiting_supplier" or waited or p.step == "waiting_supplier"]
    steps: list[dict[str, Any]] = []
    failed = p.outcome in ("failed", "attention")
    for k in keys:
        if p.outcome == "completed":
            state = "done"
        elif failed:
            state = "done" if STEP_PERCENT[k] <= p.percent else "todo"
        elif k == p.step:
            state = "current"
        else:
            state = "done" if _order(k, keys) < _order(p.step, keys) else "todo"
        steps.append({"key": k, "state": state, "at": at.get(k) if state != "todo" else None})
    if failed:
        # the steps after the point of failure are not shown: they will not happen
        cut = max((i for i, s in enumerate(steps) if s["state"] == "done"), default=-1)
        steps = steps[: cut + 1]
        steps.append({"key": p.step, "state": p.outcome, "at": at.get(p.step)})
    return {
        "step": p.step,
        "percent": p.percent,
        "outcome": p.outcome,
        "terminal": p.terminal,
        "reason": p.reason,
        "steps": steps,
        "completed_at": at.get("completed") if p.outcome == "completed" else None,
    }


def _order(key: str, keys: list[str]) -> int:
    return keys.index(key) if key in keys else -1


def is_half(params: Mapping[str, Any] | None) -> bool:
    """Whether the offer's plan is a half-price plan (adds the half-price step)."""
    plan = (params or {}).get("plan")
    return isinstance(plan, str) and plan.endswith("_half")  # as zovocard.direct.is_half_plan


def redeem_progress(
    status: Any,
    stage: Any = None,
    payment_state: Any = None,
    funding_state: Any = None,
    completed_at: Any = None,
) -> dict[str, Any]:
    """The same step model for a CDK redemption read (no job of ours: the proxy only sees the
    vendor's order). The redemption starts at ``sent`` (the vendor accepted it); ``completed`` is
    reached through the vendor's own terminal status because there is no delivery of ours to wait
    for. Returns :func:`buyer_view`."""
    vs = _low(status)
    if vs in _DONE or vs in ("succeeded", "success"):  # the sandbox says ``succeeded``
        state = "fulfilled"
    elif vs in _FAILED:
        state = "failed"
    else:
        state = "accepted_pending"
    p = map_progress(
        job_state=state,
        status=status,
        stage=stage,
        payment_state=payment_state,
        funding_state=funding_state,
        sent=True,
    )
    at = {"completed": completed_at} if completed_at else {}
    return buyer_view(p, at=at, skip=("paid",))
