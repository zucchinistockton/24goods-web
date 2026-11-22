# Xiaodou open platform: EPay interface (in use) vs. the RSA2 open API (future)

Source: Xiaodou's merchant integration skill package `xiaodou-open-platform-skill-v1.0.0` (owner-provided,
2026-11-22; capability facts dated 2026-11-11 by Xiaodou). Read in full; this note keeps what matters to 24goods.
Legend: [D] stated in Xiaodou's package, [I] our inference.

## What 24goods uses today: the EPay-compatible interface

- Endpoints on the gateway host from the seller portal's "易支付接口配置" page: `/submit.php` (redirect),
  `/mapi.php` (programmatic create, JSON `{code:1, trade_no, payurl, qrcode}`), `/api.php` (`act=order` single
  query, `act=query` merchant status) [D]. 24goods: `app/payments/zpay.py` (`mapi.php` create, `api.php` query).
- MD5 signature V1 (sorted `k=v&…` + merchant key) [D]; notify is a GET to `notify_url`, answer plain `success`
  (up to 8 retries); the return URL is not a final state; only the notify or `api.php act=order` are [D].
- No refund endpoint on this interface: refunds are a ticket in the portal [D].
- **No buyer identifier fields** (`buyerId` / `buyerPhone`) exist on this interface [D]. The "old channel"
  (platform collects, 2.80 %) does not need them [D: buyer identifiers are for merchant orders].

## The RSA2 open API (not used yet)

- Seven production endpoints under `/api/open/v1/`: `POST payments` (create; `payUrl`, Alipay `qrCode`),
  `POST payments/{outTradeNo}/attempts` (close old + new attempt), `GET payments/{outTradeNo}` (authoritative
  read), `POST payments/{outTradeNo}/close`, `POST refunds`, `GET refunds/{outRefundNo}`,
  `GET settlement/accounts` (read only) [D]. Sandbox apps add `POST payments/{outTradeNo}/sandbox` [D].
- Auth: six headers `X-XD-App-Id`, `X-XD-Timestamp`, `X-XD-Nonce`, `X-XD-Key-Id`, `X-XD-Request-Id`,
  `X-XD-Sign`; RSA-SHA256 over an 11-line canonical string `XD-Signature-v1` (method, path, query, timestamp,
  nonce, app id, key id, request id, content type, SHA-256 of the raw body) [D]. Responses and webhooks are signed
  too (separate canonical strings) [D].
- Idempotency: request level (`X-XD-Request-Id` + body digest), business level (`outTradeNo` / `outRefundNo`),
  state machine; a retry reuses request id + body but needs a NEW nonce (10-minute replay window) [D].
- States: order `CREATED → PAYING → SUCCEEDED | CLOSED | FAILED`, plus `UNKNOWN` (never treated as failed) [D].
- Create body (minimum): `outTradeNo`, `channel` (e.g. `ALIPAY_H5`), `tradeScope` (dual-declared apps only),
  `amountFen`, `currency`, `subject`, `payerClientIp` (buyer's public IP, "risk hint only") [D].
- Source-IP whitelist in the portal (production + failover egress); a removed IP gets `403` at once [D].

### Buyer identifiers and Xiaodou's risk control

- Merchant orders (the "merchant direct collection" channel, 2.25 %, money goes straight to the merchant's own
  Alipay) should carry `buyerId` and/or `buyerPhone`; Xiaodou stores only an HMAC digest [D].
- Purpose: per-buyer frequency, a "new buyer" tier, and shop-level / platform-level blacklists [D], i.e. stopping
  suspicious buyers before Alipay's own risk control flags the merchant's collection account [I].
- In "forced buyer identifier" mode a merchant Alipay order without one is refused
  (`422 OPEN_API_MERCHANT_BUYER_ID_REQUIRED`); malformed values `422 OPEN_API_MERCHANT_BUYER_ID_INVALID`; platform
  WeChat orders must not carry them [D].
- Pre-payment risk refusal: `409 OPEN_API_MERCHANT_RISK_BLOCKED` (neutral text; do not loop-retry; offer another
  payment method) [D]. App frozen by risk control: `403 OPEN_API_RISK_FROZEN` (new orders refused, existing ones
  unaffected) [D].

## If 24goods moves to the open API (see the GitHub issue)

- `buyerId`: a stable, non-reversible per-buyer value, e.g. HMAC-SHA256(shop secret, user id) [I]. Never the
  email or any raw identifier; 24goods does not collect phone numbers, so `buyerPhone` stays unset [I].
- `payerClientIp`: the buyer's public IP the API already sees (`client_ip`) [I].
- Map `409 OPEN_API_MERCHANT_RISK_BLOCKED` to a buyer-facing "use another payment method" message (USDT) [I].
- Keep the EPay interface as the default provider; the open API would be a second provider type in the existing
  provider table, selectable per channel [I].
