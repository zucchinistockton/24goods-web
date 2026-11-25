# snpay.cn (德云付) payment channel: EPay (易支付) protocol

Owner added snpay on 2026-11-25 as another payment channel. Merchant credentials are kept outside git:
`/root/.secrets/snpay.env` on the dev box. Text copies of the public docs (https://www.snpay.cn/doc/index.html and
https://www.snpay.cn/doc/v1_legacy_api.html, fetched 2026-11-25) are in [`vendor/`](vendor/).

Legend: **[V]** verified by a call on 2026-11-25; **[D]** from the vendor docs; **[I]** inferred.

## It is the EPay (易支付) protocol

- **V1** = classic EPay **[D]** (`vendor/v1_legacy_api.txt`): `https://api.snpay.cn/submit.php` (browser redirect),
  `/mapi.php` (server create, returns `payurl` / `qrcode` / `urlscheme`), `/api.php?act=query|settle|order|orders|refund`,
  form-encoded requests, JSON answers, **MD5** signature `md5(sorted non-empty params a=b&c=d + KEY)` lower case,
  notify by GET with `trade_status=TRADE_SUCCESS`, reply `success`. Payment types: `alipay`, `wxpay`, `qqpay`,
  `bank`, `jdpay`, `paypal`, `douyinpay`.
- This is the same protocol as our ZPay / Xiaodou channel (`backend/app/payments/epay.py`, `zpay.py`; gateways are
  per-tenant records in `app/modules/payments/gateways.py`), so **V1 needs no new code**: it is one more EPay gateway
  in the admin console. [I]
- **V2** **[D]** (`vendor/v2_*.txt`): new paths `/api/pay/create`, `/api/pay/submit`, `/api/pay/query`,
  `/api/pay/refund`, `/api/pay/refundquery`, `/api/pay/close`, merchant and transfer (payout) APIs, **SHA256WithRSA**:
  we sign with a **merchant private key** (generated in the snpay console: Profile -> API info -> "生成商户RSA密钥对")
  and verify answers / notifies with the **platform public key**, plus a `timestamp`. Not supported by our code yet;
  only worth it for payouts or if V1 is retired.

## Checks on 2026-11-25

- `api.php?act=query` (read-only merchant info) from the dev box 107.173.10.79 and from production c4 141.98.198.150
  (IIJ Japan): both `code 1`, merchant active, settlement type 1 (Alipay), no IP restriction seen **[V]**.
- Latency: 1.1-2.9 s from the dev box, 0.9-2.4 s from c4 (TLS + server time); `api.snpay.cn` resolves to non-mainland
  addresses (different per region) **[V]**, so the Xiaodou-style blocked route does not apply and c4 can call it
  directly (no relay entry needed).
- The platform RSA public key the owner copied decodes to a valid 2048-bit key (only used by V2).

## Console items (owner)

- **Authorized payment domains**: add `shop.kesiv.com` (where buyers come from; our `return_url` / `notify_url` host).
- **Order refund API** switch: on, if refunds should go through the API (`api.php?act=refund`).
- **Event subscription URL** (complaints, refunds): optional; we have no endpoint for these events yet.
- Which payment types are open on the merchant (Alipay, WeChat, ...) and their fees: read them in the console.
