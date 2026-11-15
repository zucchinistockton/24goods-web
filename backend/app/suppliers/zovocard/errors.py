"""ZovoCard status + ``error_code`` -> :class:`Outcome` (review-v12 §1.4, §1.5, §5; appendix A).

Routing rule: the ``error_code`` when present, else the HTTP status. ``msg`` is human text that
the vendor changes at will and is NEVER matched. Many errors carry no code at all (401/403
allowlist, 429, every card-operation 400). Anything not in the table:

* on a write (``POST``/``PUT``/``DELETE``) an unknown 4xx or an unknown 5xx is AMBIGUOUS: the
  engine reconciles by ``query()`` and may only resubmit with the same idempotency key;
* on a read it is FATAL (4xx) or RETRYABLE (5xx): a read changes nothing upstream.

Server errors (HTTP 500, ``internal_error``) and the catch-all ``request_failed`` are AMBIGUOUS on
a write for the same reason (``Rule.write_outcome``).

``supplier_stop`` marks errors that concern the whole account (credentials, allowlist, funds,
permissions): the engine opens the supplier breaker and alerts staff. Such calls were refused
before anything was executed, so their outcome is UNAVAILABLE (definite "no charge"); the one
exception is ``request_failed`` on a write, which stays AMBIGUOUS because we cannot tell its
variants apart without reading ``msg``, and which does NOT stop the supplier (it may be one paused
plan; the engine checks the offer's availability before the first send).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.suppliers.base import Outcome


@dataclass(frozen=True)
class Rule:
    outcome: Outcome
    supplier_stop: bool = False
    # outcome on a write when it differs from a read: the vendor may have executed the request
    write_outcome: Outcome | None = None
    # supplier_stop on a write when it differs from a read (None = same as ``supplier_stop``)
    write_stop: bool | None = None

    def resolve(self, write: bool) -> Rule:
        if write and self.write_outcome is not None:
            stop = self.supplier_stop if self.write_stop is None else self.write_stop
            return Rule(self.write_outcome, stop)
        return self


_OK = Rule(Outcome.OK)
_RETRY = Rule(Outcome.RETRYABLE)
_UNAV = Rule(Outcome.UNAVAILABLE)
_STOP = Rule(Outcome.UNAVAILABLE, supplier_stop=True)
_FATAL = Rule(Outcome.FATAL)
_AMBIG = Rule(Outcome.AMBIGUOUS)
# server error: a read is simply repeated; a write may have run before the error, so it is
# reconciled by query() first and resubmitted only under the SAME idempotency key
_SERVER = Rule(Outcome.RETRYABLE, write_outcome=Outcome.AMBIGUOUS)

# The one table. Keys: (status, error_code). ``None`` as status = any status for that code;
# ``None`` as code = the response carried no error_code. Codes are case-sensitive on purpose:
# ``forbidden`` (400, risk control on one request) and ``FORBIDDEN`` (403, sub-account frozen or
# missing permission) mean different things.
TABLE: dict[tuple[int | None, str | None], Rule] = {
    # ---- A.1 generic, without error_code
    (401, None): _STOP,  # missing / invalid / disabled API key
    (403, None): _STOP,  # IP allowlist missing or IP not in it; support accounts
    (429, None): _RETRY,  # 100/min/key (we stay at 60)
    (500, None): _SERVER,  # read: retry; write: AMBIGUOUS (query first, same key)
    # ---- A.1 generic, with error_code
    (None, "invalid_argument"): _FATAL,
    (None, "not_found"): _FATAL,
    (None, "insufficient_balance"): _STOP,
    # The vendor's catch-all ("具体看 msg": API-key spend cap, delisted product, maintenance...).
    # We never read msg, so on a write we cannot know whether anything ran: AMBIGUOUS. The spend
    # cap variant is account-wide, hence the supplier stop on a READ [I] (review-v12 §5); on a
    # write the answer is as likely a paused plan (one offer), so it never stops the whole
    # supplier (payment audit H2): the engine marks the offer unchecked and keeps reconciling.
    (None, "request_failed"): Rule(
        Outcome.UNAVAILABLE, supplier_stop=True, write_outcome=Outcome.AMBIGUOUS, write_stop=False
    ),
    (None, "forbidden"): _FATAL,
    (None, "internal_error"): _SERVER,
    # vendor §3 "渠道暂时不可用": the channel breaker fires BEFORE any charge (no money moved)
    (None, "channel_unavailable"): _UNAV,
    (None, "RECHARGE_REQUIRED"): _STOP,
    (None, "FORBIDDEN"): _STOP,
    # ---- A.2 card products / opening
    (None, "product_exclusive_access_required"): _FATAL,
    (None, "product_approval_required"): _FATAL,
    (None, "product_policy_outdated"): _FATAL,  # 409 with a code: staff must re-confirm
    # vendor A.2: 503 "该卡产品暂停开通" (product paused for opening, refused before execution)
    (None, "product_unavailable"): _UNAV,
    # ---- A.3 card operations
    (None, "amount_below_min"): _FATAL,
    (None, "DELETE_DISABLED"): _FATAL,
    # ---- A.4 direct top-up (GPT / Claude / Grok)
    (None, "ACCOUNT_UNAVAILABLE"): _STOP,
    (None, "GPT_DIRECT_ACCESS_DENIED"): _STOP,
    (None, "GPT_DIRECT_UNAVAILABLE"): _UNAV,
    (None, "GPT_DIRECT_PAUSED"): _UNAV,
    (None, "DIRECT_PRODUCT_DISABLED"): _UNAV,
    (None, "INVALID_REQUEST"): _FATAL,
    (None, "EXTERNAL_CARD_DISABLED"): _FATAL,
    (None, "INSUFFICIENT_BALANCE"): _STOP,
    (None, "PRECHECK_REJECTED"): _FATAL,
    (None, "GPT_PLAN_ALREADY_ACTIVE"): _FATAL,
    # needs a fresh preflight: the M2/M3 engine re-preflights and resubmits under the SAME
    # idempotency key for that step (review-v12 §5) [I]
    (None, "GPT_PRICE_UNCONFIRMED"): _RETRY,
    (None, "GPT_SESSION_INVALID"): _FATAL,
    (None, "CLAUDE_SESSION_INVALID"): _FATAL,
    (None, "GPT_CREDIT_REQUIRES_SUBSCRIPTION"): _FATAL,
    (None, "GPT_CREDIT_DISABLED"): _UNAV,
    (None, "GPT_RENEW_TARGET_NOT_PRO"): _FATAL,
    (None, "GPT_RENEW_SUBSCRIPTION_EXPIRED"): _FATAL,
    # preflight throttled (60/min/user): back off, re-preflight, resubmit under the SAME key
    (None, "GPT_PREFLIGHT_RATE_LIMITED"): _RETRY,
    # ---- A.5 CDK
    (None, "CDK_DISABLE_REJECTED"): _FATAL,  # already used or unknown: cannot void
    (None, "API_SCOPE_UNAVAILABLE"): _STOP,
    (None, "ORDER_NOT_FOUND"): _FATAL,
    (None, "cdk_brand_prefix_invalid"): _FATAL,
}

# Card recharge (vendor §3): "result pending" is HTTP 400 WITHOUT a code. Every 400 on
# /cards/recharge that is not one of these terminal codes is ACCEPTED_PENDING (resolved through
# GET /cards/{id}/recharges), never a failure and never a resubmit.
RECHARGE_TERMINAL_CODES = frozenset(
    {"amount_below_min", "insufficient_balance", "not_found", "invalid_argument"}
)


def classify(
    status: int, error_code: str | None, *, write: bool, endpoint: str | None = None
) -> Rule:
    """Outcome for a non-success response. ``status`` is the effective status (for an HTTP 200
    whose body says ``code != 0`` the caller passes the body code)."""
    code = error_code or None
    if endpoint == "cards.recharge" and status == 400 and code not in RECHARGE_TERMINAL_CODES:
        return Rule(Outcome.ACCEPTED_PENDING)
    if code is not None:
        rule = TABLE.get((status, code)) or TABLE.get((None, code))
        if rule is not None:
            return rule.resolve(write)
    else:
        rule = TABLE.get((status, None))
        if rule is not None:
            return rule.resolve(write)
    # not in the table (including an unknown error_code)
    if status == 409:
        # the first request with this Idempotency-Key is still running (server waited 30 s)
        return _AMBIG if write else _RETRY
    if 400 <= status < 500:
        return _AMBIG if write else _FATAL
    return _AMBIG if write else _RETRY


__all__ = ["RECHARGE_TERMINAL_CODES", "TABLE", "Rule", "classify"]
