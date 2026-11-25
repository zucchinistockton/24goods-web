# ChatGPT · 开放 API 对接文档

> **本文档只写 ChatGPT。** Grok 订阅请看 [Grok 开放 API 对接文档](./grok-开放API对接文档.md)。不要把 grok.com 的 `_twsid` 当成 Session 提交到 ChatGPT 套餐。
>
> **API Key 获取说明**：请在后台 **API管理** 生成固定 API Key（或卡密管理里的卡密）。把它作为 `X-API-Key` 调用开放接口，也可以在前台开通页填写。默认开通**成功才扣点**，失败不扣；站点开启「失败也扣点」后，开通失败同样扣点。卡密剩余点数可通过 `POST /api/v1/third-party/credits/recharge` 或前台 `/query` 充进 API Key。过往记录：`POST /api/v1/third-party/orders/history`，或打开站点 `/query` 页面查询。
>
> **强烈推荐使用 `direct` 直充方式**：三方系统接入时，建议默认使用 `orderType=direct`，由下游直接提交本次支付所需的银行卡信息。该方式调用链路更直接，不依赖平台银行卡库存，通常更适合稳定、可控的自动化开通。
>
> **`card_key` 兼容性提示**：`orderType=card_key` 当前仍可下单（使用平台卡库选卡），仅作为兼容能力保留。新接入、重构或迁移中的三方系统请使用 `orderType=direct`。

本文档用于下游系统通过 API Key 创建正价开通订单并查询订单最终状态。

生产环境地址：请替换为你的站点根地址，例如 `https://example.com/`

生产 API Base URL：`https://example.com/api/v1`

接口统一前缀：`/api/v1`

当前提供三个接口：

- `GET /api/v1/third-party/user`：确认当前 `X-API-Key`。收费模式下必须是本站卡密/API，`points` 为剩余点数。
- `POST /api/v1/third-party/orders/direct`：创建三方订单，同时支持直充订单和卡密订单。
- `POST /api/v1/third-party/orders/status`：使用创建接口返回的卡密查询最新订单状态。
- `POST /api/v1/third-party/orders/history`：按当前 `X-API-Key` 分页查询过往开通记录。
- `POST /api/v1/third-party/credits/recharge`：用卡密管理里的卡密，把剩余点数充进当前 `X-API-Key`（必须是后台发放的 API Key）。

> 注意：创建接口路径中的 `direct` 是历史保留命名。实际订单类型由请求体 `orderType` 决定，支持 `direct` 和 `card_key`。

## 接入流程

推荐下游按以下流程接入：

1. 向本站购买或领取卡密，将该卡密作为 `X-API-Key` 保存到服务端配置，不要写入前端。
2. 后续查询订单时使用同一把 Key。
3. 准备 ChatGPT Session JSON。**必须原样提交 Session 接口返回的完整 JSON**，不能只拼 `email` 和 `accessToken`。缺少 `sessionToken`、`user.id` 或 `expires` 会直接拒绝下单。
4. **使用 `orderType=direct` 直充方式对接 API**。
5. 调用创建订单接口。
6. 创建成功后保存响应中的 `order_no` 和 `card_key`。
7. 使用 `card_key` 定时调用订单状态查询接口。
8. 若 `verification` 不为 `null`，在对接方自己的页面用 Stripe.js `handleNextAction` 完成安全验证，然后继续轮询原单。不要把 `client_secret` 写入普通日志或转给无关第三方。
9. 查询到 `status=success` 或 `status=failed` 后停止轮询。

创建订单接口受系统维护开关控制。管理员关闭充值后，接口在认证后、创建订单前直接返回 HTTP `403`、业务码 `40305`；用户信息和订单状态查询接口不受影响。

创建成功仅表示订单已经创建并进入异步开通队列，不表示开通已经完成。最终结果以状态查询接口返回的 `status` 为准。

## 本站约定

- `X-API-Key` 默认必须是本站发放的卡密，开通成功按套餐扣点（扣点数量由后台「套餐扣点」配置，默认各 1 点）。
- `points` 返回该卡密剩余点数；未知 Key 且未开启强制卡密时仍返回较大占位值 `99990`。
- 三方订单若使用本站卡密，会消耗卡密点数；失败与取消不扣。
- 套餐取值：`plus` / `go` / `pro5` / `pro20` / `pro25` / `plus_to_5x` / `renew_20x` / `to_20x` / `to_25x` / `codex` / `team`。分别对应 Plus 正价开通、Go 正价开通、5X 正价开通、20X 正价开通、25X 正价开通、5X升级、20X 续费、Go/Plus/5X 升 20X、Go/Plus/5X/20X 升 25X、Codex 点数充值、Team 正价开通。`go` 也可传入别名 `chatgpto` / `chatgptgo` / `chatgptgoplan`；`pro25` 也可传入 `25x` / `chatgptpromax`；`plus_to_5x` 也可传入别名 `plus5x` / `to_5x` / `upgrade_5x`；`renew_20x` 也可传入 `20x_renew` / `pro20_renew`；`to_20x` 也可传入 `upgrade_20x` / `pro20_upgrade`；`to_25x` 也可传入 `upgrade_25x` / `pro25_upgrade`；`codex` 也可传入 `codex_credit` / `codex_credits` / `credits` / `credit`；`team` 也可传入 `chatgptteam` / `chatgptteamplan`。
- 结账地区 `region`：两位国家码，如 `PH`（菲律宾 / PHP）、`US`（美国 / USD）、`EG`（埃及 / EGP）、`CL`（智利 / CLP）。**不传或空字符串固定菲律宾 `PH`。** 显式传入时必须是后台已对该套餐勾选并开启的节点，否则返回 `40020`。仅 Codex 点数可传 `auto`：按该账号现有订阅货币自动选已开放地区（`PHP`→`PH`，`USD`→`US`，`EGP`→`EG`，`CLP`→`CL`）。没有对应节点，或读不到订阅货币（含手机订阅），改走菲律宾 `PH` / `PHP`。订单里保存的是解析后的国家码，不会保存 `auto`。
- `plus` / `go` / `pro5` / `pro20` / `pro25` 是正价开通：`token.account.planType` 有值即可（`free` / `plus` / `pro` / `go` 等都可以）。到期后 Session 未刷新、仍显示旧套餐的账号也可以提交。需要银行卡（`direct` 自填卡，`card_key` 用平台卡库）。`go` 走官方 `chatgptgoplan`，规则与 Plus 相同：必须带卡、成功后取消自动续费。
- `plus_to_5x` 是给**已有 Go 或 Plus** 的账号升到 5X：`token.account.planType` 必须为 `go` 或 `plus`（也接受 `chatgptgoplan`）。用 `upgradeMode` 区分同卡 / 新卡：
  - `same_card`（默认，可省略）：**不传银行卡**，使用账号已绑的默认卡差价升级。
  - `new_card`：必须传银行卡（`direct` 自填卡，`card_key` 从平台卡库选卡），先加卡并设为默认卡，再差价升级。
  - 未传 `upgradeMode` 但直充请求里带了合法卡号，也按新卡升级处理。
  - 状态查询里的 `plan_type` 仍返回 `plus_to_5x`（不会映射成 `pro5`），并额外返回 `upgrade_mode`。
- `to_20x` 是给 **已有 Go、Plus 或 5X** 的账号升到 20X：`token.account.planType` 必须为 `go`、`plus` 或 `prolite`（也接受 `chatgptgoplan` / `chatgptprolite`）。用 `upgradeMode` 区分同卡 / 新卡，规则与 `plus_to_5x` 相同。免费号请走 `pro20` 正价开通；已是 20X 请走 `renew_20x`。成功后会取消自动续费。状态查询里的 `plan_type` 返回 `to_20x`（不会映射成 `pro20`），并额外返回 `upgrade_mode`。
- `to_25x` 是给 **已有 Go、Plus、5X 或 20X** 的账号升到 25X：`token.account.planType` 必须为 `go`、`plus`、`prolite` 或 `pro`（也接受 `chatgptgoplan` / `chatgptprolite` / `chatgptpro`）。同卡 / 新卡规则与 `to_20x` 相同。免费号请走 `pro25` 正价开通。成功后会取消自动续费。状态查询里的 `plan_type` 返回 `to_25x`。
- `renew_20x` 是给 **Pro 20X** 续费（含暂停/欠费）：`token.account.planType` 必须为 `pro`（也接受 `chatgptpro`）。**必须传银行卡**。若账号暂停（`is_delinquent` 或 chatgptpro 发票 `due`），只用这张卡支付 Stripe 托管 due 发票，**不再加卡、不 POST renew**。未暂停则加卡并设默认卡，若未开启自动续费再打开。纯到期且无欠费发票时请走 20X 正价开通。成功后**不会**取消自动续费，`is_subscription_cancelled` 保持 `0`。状态查询里的 `plan_type` 返回 `renew_20x`（不会映射成 `pro20`）。
- `codex` 是给**任意已登录账号**一次性充值 Codex 点数：`token.account.planType` 只要有值即可（free / plus / prolite / pro 均可）。**必须传银行卡**。用 `creditQuantity` 指定点数，预设 `250` / `500` / `2500` / `5000`，也可自定义 `1–25000`；省略时默认 `500`。`region` 可传 `auto`，按账号现有订阅货币选代理和结账货币。走官方 `standalone_credit` 一次性支付（`oaics_` 或 `cs_live_`），成功后**不会**取消自动续费。状态查询里的 `plan_type` 返回 `codex`，并额外返回 `credit_quantity`。
- `team` 是 ChatGPT Team 正价开通：`token.account.planType` 有值即可。**必须传银行卡**。走 `cs_live_` 自定义结账，官方 `plan_name=chatgptteamplan`。用 `seatQuantity` 指定席位，范围 `2–200`，省略时用后台默认（默认 `5`）。建会话后、扣款前会 `checkout/update` 写入同一席位。成功后取消自动续费。状态查询里的 `plan_type` 返回 `team`。
- `plus_half` 是 Plus 三个月五折：别名 `plus50` / `plus_50` / `half_plus`。**必须传银行卡**。只有 `accounts/check` 的 `eligible_promo_campaigns.plus.id` 为 `plus-3-months-50-pct-off` 的资格号可以开通。走 `oaics_`，建会话带该促销；官方没有回显 50% 折扣则中止，不会改走正价 `cs_live_`。成功后账号是 Plus，并取消自动续费。状态查询里的 `plan_type` 返回 `plus_half`。
- 结账货币跟 `region` 走（默认 `PH` / `PHP`）。创建成功时 `payment_amount` / `payment_currency` 可能为 `null`，支付完成后按实际货币返回。
- `token` 必须是完整 Session JSON，强制包含 `user`、`account`、`accessToken`、`sessionToken`、`expires`。
- `card_key` 订单的套餐由请求体 `planType` 决定，省略时默认 `plus`。`plus_to_5x` / `to_20x` 且同卡升级时不从卡库选卡；新卡升级与 `renew_20x` 会从卡库选卡。
- 刷新后的最新 `accessToken` 会写回订单，状态查询的 `token` 为最新值。
- `is_subscription_cancelled`：`1` 已取消自动续费，`0` 未取消或未确认。正价开通、5X升级 与 20X 升级成功后会尝试取消自动续费；**20X 续费与 Codex 点数充值成功后保持自动续费**，该字段为 `0` 即符合预期。

## 通信约定

### 请求格式

- 协议：HTTP 或 HTTPS，生产环境必须使用 HTTPS。
- 数据格式：JSON。
- 字符编码：UTF-8。
- 请求头 `Content-Type`：`application/json`。
- 三方认证请求头：`X-API-Key`。

### 统一响应结构

业务接口正常进入应用处理后，响应使用以下结构：

```json
{
  "code": 0,
  "message": "success",
  "data": {}
}
```

字段说明：

- `code`：业务状态码。`0` 表示成功，非 `0` 表示失败。
- `message`：成功提示或失败原因。
- `data`：业务数据。失败时通常为 `null`。

下游必须同时判断 HTTP 状态码和响应体中的 `code`：

- HTTP `2xx` 且 `code=0`：业务成功。
- HTTP `4xx/5xx` 或 `code!=0`：业务失败。

请求字段类型、必填项或枚举值不符合 Schema 时，服务可能返回 HTTP `422` 的原生参数校验响应。下游应将所有非 `2xx` 响应视为失败，并记录响应内容用于排查。

### 时间格式

状态查询响应中的时间字段使用北京时间字符串：`YYYY-M-D HH:mm:ss`。

示例：`2026-8-16 17:30:00`

## 传输

对接方按本文档的字段原样提交，请求走 HTTPS。服务端收到后自行加密卡号、有效期和登录态再入库，CVV 不入库。调用方不用自己封包。

## API Key 认证

三个接口都必须在请求头中携带 API Key：

```http
X-API-Key: <cdk_code>
```

认证规则：

- 请求头必须带非空的 `X-API-Key`。
- 默认必须是本站卡密管理发放的卡密；未登记返回 `40107`，停用/过期/次数不足/套餐不符返回 `40306`。
- 创建时预占额度。默认开通成功才扣点，失败不扣；开启「失败也扣点」后开通失败同样扣点。取消不扣。
- 不需要同时携带后台登录接口的 Bearer token。

使用建议：

- 不要放在 URL、查询参数或请求体中。
- 不要写入前端页面、浏览器代码、公开仓库或普通业务日志。
- 日志中如需记录，只保留前后少量字符并做脱敏。
- 请固定使用同一把 Key。本站按这把 Key 归属订单，换 Key 后查不到旧订单。

## 查询当前用户信息

`GET /api/v1/third-party/user`

成功 HTTP 状态码：`200 OK`

该接口用于下游确认当前 `X-API-Key` 已被接受，并读取剩余点数。收费模式下未登记的 Key 返回 `40107`。接口无请求体。

### 请求头

```http
X-API-Key: <cdk_code>
```

### cURL 示例

```bash
curl -X GET "https://example.com/api/v1/third-party/user" \
  -H "X-API-Key: <cdk_code>"
```

### 成功响应

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "id": "0",
    "username": "api-user",
    "email": "",
    "points": 99990,
    "role": "normal",
    "status": "active",
    "is_active": true
  }
}
```

### 响应字段

- `id`
  - 类型：字符串。
  - 含义：兼容字段，当前固定为 `"0"`。

- `username`
  - 类型：字符串。
  - 含义：兼容字段，当前固定为 `api-user`。

- `email`
  - 类型：字符串。
  - 含义：固定返回空字符串。不是某笔订单中的 ChatGPT 账号邮箱。

- `points`
  - 类型：整数。
  - 含义：本站卡密的剩余点数。未知 Key 且管理员关闭强制卡密时返回占位值 `99990`。建议以能否下单的错误码为准，不要只看此字段。

- `role`
  - 类型：字符串。
  - 当前固定值：`normal`。

- `status`
  - 类型：字符串。
  - 当前固定值：`active`。

- `is_active`
  - 类型：布尔值。
  - 当前固定值：`true`。

### 安全说明

接口不会返回以下敏感字段：

- CDK / API Key 原文
- 密码或密码哈希
- 登录 token
- ChatGPT accessToken 或 sessionToken

缺少 `X-API-Key` 时返回 `40106`。Key 未在后台登记时（收费模式）返回 `40107`。卡密停用、过期、次数不足或套餐不符返回 `40306`。

## 创建三方订单

`POST /api/v1/third-party/orders/direct`

成功 HTTP 状态码：`201 Created`

### 请求头

```http
X-API-Key: <cdk_code>
Content-Type: application/json
```

### 订单类型

> **接入建议：请使用 `direct`。** 对新接入、重构或迁移中的三方调用方，`direct` 应作为默认 API 方案：下游直接为每笔订单提供银行卡信息，不依赖平台银行卡库存，支付资源及失败排查也更明确。
>
> `card_key` 当前仍可下单，但仅作为存量兼容能力保留。请勿基于 `card_key` 开展新的接口对接。

请求体通过 `orderType` 区分支付方式：

- `direct`（强烈推荐）：`plus` / `go` / `pro5` / `pro20` / `pro25` / `renew_20x` / `codex` 由下游上传银行卡；`plus_to_5x` / `to_20x` 默认同卡升级可不传卡，`upgradeMode=new_card` 时必须传卡。
- `card_key`：下游上传查询用卡密，套餐由 `planType` 决定（可省略，默认 `plus`）。`plus` / `go` / `pro5` / `pro20` / `pro25` / `renew_20x` / `codex` 以及差价升级的新卡模式从平台卡库选卡；`plus_to_5x` / `to_20x` 同卡升级不选库内卡。

创建请求进入服务后，会按以下顺序处理：

1. 校验 `token` 是完整 Session JSON 对象。
2. 校验 `token.user.id`、`token.user.email`。
3. 校验 `token.account.id` 是非空字符串。
4. 校验 `token.accessToken` 的 JWT 结构和有效时间。
5. 校验 `token.sessionToken`、`token.expires` 均存在且格式有效。
6. 按 `planType` 校验账号当前套餐：`plus` / `go` / `pro5` / `pro20` / `pro25` 与 `codex` 只要求 `account.planType` 有值（到期账号 Session 仍是旧套餐也可以）；`plus_to_5x` 要求 `account.planType=go` 或 `plus`；`to_20x` 要求 `account.planType=plus` 或 `prolite`；`renew_20x` 要求 `account.planType=pro`。
7. 确认请求头 `X-API-Key` 非空，并按收费规则校验本站卡密额度。
8. 检查系统是否处于维护关闭。
9. 检查当前开通队列是否已满。
10. 根据 `orderType`、`planType` 与 `upgradeMode` 校验银行卡或查询卡密。`plus_to_5x` / `to_20x` 同卡升级跳过银行卡与卡库；新卡升级、`renew_20x` 与 `codex` 需要卡。
11. 解析 `region`：缺省为 `PH`；`auto` 仅 Codex 可用，按账号订阅货币解析成已开放地区。没有对应节点，或读不到订阅货币（例如手机订阅），只要菲律宾已开放就改走 `PH` / `PHP`。其他值校验两位国家码且该套餐已开放该地区。显式地区非法返回 `40020` 且不创建订单。`auto` 在菲律宾也未开放时返回 `40020`，并记一条失败订单，不扣点、不入队。
12. 创建订单并投递异步开通队列。

无效 token 会在写入订单前被拒绝。

### 计费规则

三方开放接口按本站卡密扣点：

- `X-API-Key` 默认必须是本站发放的卡密。
- 创建订单时按套餐扣点预占额度（与排队中订单合计不能超过剩余点数）。
- 开通成功才真正扣点；失败与取消释放预占。开启「失败也扣点」后，开通失败也转为实扣。
- `GET /api/v1/third-party/user` 的 `points` 为该卡密剩余点数。

### 公共请求字段

- `orderType`
  - 类型：字符串。
  - 必填：是。
  - 可选值：`direct`、`card_key`。
  - 含义：本次订单的支付方式。

- `region`
  - 类型：字符串。
  - 必填：否。
  - 格式：两位国家码，如 `PH`、`US`、`EG`、`CL`（大小写均可）。仅 `planType=codex` 还可传 `auto`。
  - 默认：不传则为 `PH`（菲律宾，货币 `PHP`）。
  - 含义：结账地区 / 货币 / 代理出口。直充与卡密订单均可传入。显式传入国家码时必须已对该套餐开放。`auto` 在创建时读取账号现有订阅货币并换成对应地区，响应里的 `region` 是 `PH` / `US` / `EG` / `CL` 之一。

- `token`
  - 类型：JSON 对象。
  - 必填：是。
  - 含义：ChatGPT Session 接口返回的**完整 JSON**。只拼 `email` + `accessToken` 会被拒绝。该对象会随订单保存，供开通、刷新 Token 和取消续费使用。

- `token.user`
  - 类型：JSON 对象。
  - 必填：是。
  - 含义：ChatGPT 用户信息对象。

- `token.user.id`
  - 类型：字符串。
  - 必填：是。
  - 含义：ChatGPT 用户 ID。

- `token.user.email`
  - 类型：字符串。
  - 必填：是。
  - 含义：开通目标 ChatGPT 账号邮箱，也会写入订单 `email`。

- `token.account`
  - 类型：JSON 对象。
  - 必填：是。
  - 含义：开通目标 ChatGPT 账户信息。

- `token.account.id`
  - 类型：字符串。
  - 必填：是。
  - 校验：去除首尾空格后不能为空，数字、`null`、对象或数组均不允许。
  - 含义：开通目标 ChatGPT Account ID。

- `token.accessToken`
  - 类型：字符串。
  - 必填：是。
  - 含义：ChatGPT accessToken。`plus` / `go` / `pro5` / `pro20` / `pro25` / `codex` 用于创建 checkout 并支付；`plus_to_5x` / `to_20x` 用于预览差价并提交订阅升级；`renew_20x` 用于加卡并开启自动续费（暂停账号会先支付欠费发票）。
  - 校验：必须是三段式 JWT，payload 必须能解析，必须包含数值型 `iat`、`exp`，并且当前时间不能超过 `exp`。

- `token.expires`
  - 类型：字符串。
  - 必填：是。
  - 含义：Session 过期时间，例如 `2026-11-01T08:34:59.567Z`。

- `token.sessionToken`
  - 类型：字符串。
  - 必填：是。
  - 含义：ChatGPT `__Secure-next-auth.session-token`（五段 JWE）。开通成功后旧 accessToken 会失效，必须用它刷新 Token、查询套餐并取消自动续费。
  - 注意：空字符串、`null`、数字、对象或数组都会被拒绝。

`token` 中除上述字段以外的其他 Session 字段必须尽量原样传入，服务会按原 JSON 保存。下游不应修改 accessToken / sessionToken 内容，也不要在日志中输出完整 token。只手工拼接部分字段会返回 `40028`。

### 必须提交的 token 结构

下游必须直接传递 ChatGPT Session 接口返回的完整 JSON，禁止只拼 `email` 和 `accessToken`。

推荐结构示例：

```json
{
  "WARNING_BANNER": "<sensitive-information-warning>",
  "user": {
    "id": "user-example",
    "name": "Example User",
    "email": "user@example.com",
    "idp": "auth0",
    "iat": 1785662977,
    "amr": [
      "otp",
      "urn:openai:amr:otp_email"
    ],
    "mfa": false
  },
  "expires": "2026-11-01T08:34:59.567Z",
  "account": {
    "id": "account-example",
    "planType": "free",
    "structure": "personal",
    "isUsageBasedSeatEnabled": false,
    "isConversationClassifierEnabledForWorkspace": true,
    "hasFloraFeature": false,
    "isFedrampCompliantWorkspace": false,
    "isDelinquent": false,
    "residencyRegion": "no_constraint",
    "computeResidency": "no_constraint"
  },
  "accessToken": "<chatgpt_access_token>",
  "authProvider": "openai",
  "sessionToken": "<chatgpt_session_token>",
  "rumViewTags": {
    "light_account": {
      "fetched": false
    }
  }
}
```

字段要求分级：

- 接口强制要求（缺一即拒绝）：
  - `user.id`
  - `user.email`
  - `account.id`
  - `accessToken`
  - `sessionToken`
  - `expires`

- 推荐原样传递：
  - `WARNING_BANNER`
  - `user.name`
  - `user.idp`
  - `user.iat`
  - `user.amr`
  - `user.mfa`
  - `account` 中除 `id` 外的其他字段
  - `authProvider`
  - `rumViewTags`

- 允许的扩展字段：
  - ChatGPT Session JSON 中存在的其他字段也可以传入。
  - 服务会将完整 `token` JSON 保存到订单，不会主动删除未知字段。

注意事项：

- 示例中的 ID、邮箱和 token 均为占位值，下游必须替换为实际 Session 数据。
- 不要将真实 `accessToken` 或 `sessionToken` 写入接口文档、工单、聊天记录、截图或普通日志。
- `expires` 是 Session 层的过期时间参考；当前创建订单前的强制有效性判断以 `accessToken` JWT payload 中的 `iat` 和 `exp` 为准。
- `account.planType` 表示账号**当前**套餐。正价开通（`plus` / `go` / `pro5` / `pro20` / `pro25`）和 Codex 点数只要该字段有值即可，不要求必须是 `free`；5X升级（`plus_to_5x`）只能提交 `go` 或 `plus`；20X 升级（`to_20x`）只能提交 `go`、`plus` 或 `prolite`；25X 升级（`to_25x`）只能提交 `go`、`plus`、`prolite` 或 `pro`；20X 续费（`renew_20x`）只能提交 `pro`。本次要开的套餐仍由请求体 `planType` 决定。不匹配会返回业务码 `40030`。

## 创建直充订单

`plus` / `go` / `pro5` / `pro20` / `pro25` / `renew_20x` / `codex` 的直充订单使用下游请求中提供的银行卡和套餐。`plus_to_5x` / `to_20x` 仍用 `orderType=direct`：同卡升级不传卡，新卡升级必须传卡，见下方对应升级章节。

> **下单容量提示**：如果创建订单时返回 `当前并发过高，请稍后重试`（HTTP `429`，业务码 `42902`），表示服务器当前可处理的订单量已满，并非请求参数或银行卡信息错误。请勿高频连续重试，建议等待一段时间后重新下单。

### 请求示例

```json
{
  "orderType": "direct",
  "cardNumber": "4242424242424242",
  "expMonth": 12,
  "expYear": 2032,
  "cvv": "123",
  "token": {
    "user": {
      "id": "user-example",
      "email": "user@example.com"
    },
    "expires": "2026-11-01T08:34:59.567Z",
    "account": {
      "id": "account-example"
    },
    "accessToken": "<chatgpt_access_token>",
    "sessionToken": "<chatgpt_session_token>"
  },
  "planType": "pro5"
}
```

### 直充专用字段

- `cardNumber`
  - 类型：字符串。
  - 必填：`orderType=direct` 且 `planType` 为 `plus` / `go` / `pro5` / `pro20` / `pro25` / `renew_20x` / `codex` 时必填；`plus_to_5x` / `to_20x` 仅在 `upgradeMode=new_card`（或未传 `upgradeMode` 但带了卡号）时必填，同卡升级不传。
  - 格式：`12-19` 位数字，不允许空格或连字符。
  - 含义：本次支付或加卡使用的银行卡号。

- `expMonth`
  - 类型：整数。
  - 必填：与 `cardNumber` 相同。
  - 范围：`1-12`。
  - 含义：银行卡有效期月份。

- `expYear`
  - 类型：整数。
  - 必填：与 `cardNumber` 相同。
  - 范围：`2000-9999`。
  - 含义：银行卡有效期年份。
  - 校验：`expYear/expMonth` 不能早于服务器当前月份。

- `cvv`
  - 类型：字符串。
  - 必填：与 `cardNumber` 相同。
  - 格式：`3-4` 位数字。
  - 含义：银行卡安全码。

- `planType`
  - 类型：字符串。
  - 必填：`orderType=direct` 时必填。
  - 可选值：`plus`、`go`（别名 `chatgpto` / `chatgptgo` / `chatgptgoplan`）、`pro5`、`pro20`、`pro25`（别名 `25x` / `chatgptpromax`）、`plus_to_5x`（别名 `plus5x` / `to_5x` / `upgrade_5x`）、`renew_20x`（别名 `20x_renew` / `pro20_renew`）、`to_20x`（别名 `upgrade_20x` / `pro20_upgrade`）、`codex`（别名 `codex_credit` / `codex_credits` / `credits` / `credit`）、`team`（别名 `chatgptteam` / `chatgptteamplan`）。
  - 含义：需要开通、升级、续费或充值点数的 ChatGPT 套餐。
  - 对应关系：
    - `plus` → ChatGPT Plus（正价开通，不限制当前 `account.planType`）
    - `go` → ChatGPT Go（正价开通，官方 `chatgptgoplan`，不限制当前 `account.planType`）
    - `pro5` → ChatGPT Pro 5X（正价开通，不限制当前 `account.planType`）
    - `pro20` → ChatGPT Pro 20X（正价开通，不限制当前 `account.planType`）
    - `pro25` → ChatGPT Pro 25X（正价开通，官方 `plan_name=chatgptpromax`，直接生成结账链接，不限制当前 `account.planType`）
    - `plus_to_5x` → 5X升级到 Pro 5X（已有 Go 或 Plus；用 `upgradeMode` 选同卡或新卡）
    - `renew_20x` → Pro 20X 续费（含暂停/欠费恢复；加新卡并开启自动续费）
    - `to_20x` → Go / Plus / 5X 升级到 Pro 20X（用 `upgradeMode` 选同卡或新卡）
    - `to_25x` → Go / Plus / 5X / 20X 升级到 Pro 25X（用 `upgradeMode` 选同卡或新卡）
    - `codex` → Codex 点数一次性充值（任意已登录账号；用 `creditQuantity` 指定点数）
    - `team` → ChatGPT Team 正价开通（`cs_live_`，官方 `chatgptteamplan`；用 `seatQuantity` 指定席位）
    - `plus_half` → Plus 三个月五折（仅资格号；`oaics_`，促销 `plus-3-months-50-pct-off`）

- `creditQuantity`
  - 类型：整数。
  - 必填：否。仅 `planType=codex` 时生效，其他套餐忽略。
  - 范围：`1-25000`。省略时默认 `500`。
  - 常用值：`250`、`500`、`2500`、`5000`。
  - 含义：本次购买的 Codex 点数。非法值返回 `40020`。

- `seatQuantity`
  - 类型：整数。
  - 必填：否。仅 `planType=team` 时生效，其他套餐忽略。
  - 范围：`2-200`。省略时用后台「Team 席位」，默认 `5`。小于 2 按 2，大于 200 按 200。
  - 含义：本次开通的 Team 席位数。建会话和扣款前的 `checkout/update` 使用同一个数字。

- `upgradeMode`
  - 类型：字符串。
  - 必填：否。仅 `planType=plus_to_5x` 或 `to_20x` 时生效，其他套餐忽略。
  - 可选值：`same_card`、`new_card`（也接受 `same` / `new`）。
  - 默认：未传且未带卡号时为 `same_card`。
  - 含义：差价升级使用账号已绑默认卡，还是先加一张新卡再升级。非法值返回 `40020`。

- `region`
  - 类型：字符串。
  - 必填：否。
  - 格式：两位大写或小写国家码，例如 `PH`、`US`、`EG`、`CL`。`planType=codex` 还可传 `auto`（大小写均可）。
  - 默认：不传、空字符串或只有空格时为 `PH`（菲律宾，结账货币 `PHP`）。
  - 含义：结账地区，同时决定代理出口、oaics / Stripe 账单国和货币、协议语言与时区。
  - 对应关系（常见）：
    - `PH` → 菲律宾 / `PHP`
    - `US` → 美国 / `USD`
    - `EG` → 埃及 / `EGP`
    - `CL` → 智利 / `CLP`
  - `auto`（仅 Codex）：创建订单时读取该账号现有订阅的结账货币，在已开放地区里匹配。`PHP` 用菲律宾，`USD` 用美国，`EGP` 用埃及，`CLP` 用智利。没有对应节点，或读不到订阅货币（含手机订阅），改走菲律宾 `PH` / `PHP`。响应和后续查询返回解析后的国家码，不返回 `auto`。
  - 校验：显式传入国家码时必须对该套餐已开放；非法格式或未开放返回 `40020`，不创建订单。非 Codex 传 `auto` 返回 `40020`，不创建订单。Codex 传 `auto` 时，没有对应地区，或读不到订阅货币（含手机订阅），只要菲律宾已开放就按 `PH` / `PHP` 继续开通。菲律宾也未开放才返回 `40020`，并写入一条失败订单（状态失败、扣点 0）。存量调用方不传该字段时行为为菲律宾 PHP，无需改代码。

### 直充处理说明

- 系统会生成 `DIRECT-` 前缀的随机卡密，例如 `DIRECT-kcJ_wcE5lyTQqjdZYfymdINXFoLk47WI`。
- 随机卡密写入订单 `card_key` 并通过创建响应返回。
- 下游必须保存该 `card_key`，后续通过它查询订单状态。
- `plus` / `pro5` / `pro20` / `pro25`：银行卡号、有效期和 CVV 会随任务保存，worker 走正价开通协议（Plus / 5X 走 gptplus，20X 走 gpt20x）。`pro25` 建会话时直接提交官方 `plan_name=chatgptpromax` 并生成付款链接。结账国家/货币固定为 `PH` / `PHP`。
- `plus_to_5x` 同卡：不保存新卡，worker 预览差价后对 ChatGPT `subscriptions/update` 提交 `updated_plan=chatgptprolite`，用账号已绑默认卡扣差价。
- `plus_to_5x` 新卡：先走账单页加卡并设为默认卡，再执行与同卡相同的 `subscriptions/update`。
- `to_20x` 同卡：不保存新卡。worker 先尝试 ChatGPT `subscriptions/update` 的 `updated_plan=chatgptpro`；若官方返回 `plan_not_available`（20X 通常不能 in-place update），改走与 20X 正价相同的 Checkout：先建 `chatgptprolite` 再 `checkout/update` 切到 `chatgptpro`，用账号已绑默认卡扣差价。Plus 和 5X 都可以升。
- `to_20x` 新卡：先走账单页加卡并设为默认卡，再执行与同卡相同的差价升级（官方不提供 in-place update 时同样改 Checkout）。
- `renew_20x`：若账号暂停/欠费，只用提交的卡支付 Stripe 托管 due 发票，不加卡、不 POST renew。未暂停则加卡并设为默认卡，查询 `will_renew`；未开启则 `POST /subscriptions/renew` 打开自动续费。不走 checkout。纯到期且没有欠费发票时不能续费。
- `codex`：银行卡号、有效期和 CVV 随任务保存，worker 走官方 `standalone_credit` 一次性支付（Stripe `mode=payment`）。不改订阅、成功后不取消自动续费。
- 正价开通、5X升级 与 20X 升级成功后会刷新 Token，查到对应套餐后再取消自动续费。**20X 续费与 Codex 点数充值成功后保持自动续费，不会取消。**

## 创建 5X 升级订单

已有 Go 或 Plus 账号用 `planType=plus_to_5x` 升到 5X。Session 必须是 Go 或 Plus 账号。用 `upgradeMode` 选择同卡或新卡。

### 同卡升级请求示例

账号上已有默认支付卡时，不要再传卡号。`upgradeMode` 可省略，默认 `same_card`。

```json
{
  "orderType": "direct",
  "planType": "plus_to_5x",
  "upgradeMode": "same_card",
  "token": {
    "user": {
      "id": "user-example",
      "email": "user@example.com"
    },
    "expires": "2026-11-01T08:34:59.567Z",
    "account": {
      "id": "account-example",
      "planType": "plus"
    },
    "accessToken": "<chatgpt_access_token>",
    "sessionToken": "<chatgpt_session_token>"
  }
}
```

### 新卡升级请求示例

```json
{
  "orderType": "direct",
  "planType": "plus_to_5x",
  "upgradeMode": "new_card",
  "cardNumber": "4242424242424242",
  "expMonth": 12,
  "expYear": 2032,
  "cvv": "123",
  "token": {
    "user": {
      "id": "user-example",
      "email": "user@example.com"
    },
    "expires": "2026-11-01T08:34:59.567Z",
    "account": {
      "id": "account-example",
      "planType": "plus"
    },
    "accessToken": "<chatgpt_access_token>",
    "sessionToken": "<chatgpt_session_token>"
  }
}
```

### 处理说明

- `token.account.planType` 必须为 `go` 或 `plus`（也接受 `chatgptgoplan`）。免费号、5X、20X 都会被拒绝（`40030`）。
- 同卡升级不要传 `cardNumber` / `expMonth` / `expYear` / `cvv`。传入格式非法的卡字段仍可能先被 Schema 拦成 HTTP `422`，建议完全省略。
- 新卡升级必须传完整卡信息；缺少卡号时返回 `40020`，消息为 `新卡升级请填写 cardNumber / expMonth / expYear / cvv`。
- 同卡升级时，账号没有已绑默认卡，任务会失败，不会去绑新卡。
- 创建响应里的 `plan_type` 为 `plus_to_5x`，`upgrade_mode` 为 `same_card` 或 `new_card`。升级成功后 ChatGPT 侧套餐为 `prolite`（5X）。
- `orderType=card_key` 也可以下 `plus_to_5x`：仍要传 `cardKey` 作为查询标识。同卡升级不从平台卡库选卡；`upgradeMode=new_card` 会从卡库选卡，卡库为空时返回 `40012`。

## 创建 20X 升级订单

已有 Go、Plus 或 5X 账号用 `planType=to_20x` 升到 20X。免费号请走 `pro20`，已是 20X 请走 `renew_20x`。用 `upgradeMode` 选择同卡或新卡。成功后会取消自动续费。

### 同卡升级请求示例

账号上已有默认支付卡时，不要再传卡号。`upgradeMode` 可省略，默认 `same_card`。5X 账号把 `account.planType` 改成 `prolite` 即可。

```json
{
  "orderType": "direct",
  "planType": "to_20x",
  "upgradeMode": "same_card",
  "token": {
    "user": {
      "id": "user-example",
      "email": "user@example.com"
    },
    "expires": "2026-11-01T08:34:59.567Z",
    "account": {
      "id": "account-example",
      "planType": "plus"
    },
    "accessToken": "<chatgpt_access_token>",
    "sessionToken": "<chatgpt_session_token>"
  }
}
```

### 新卡升级请求示例

```json
{
  "orderType": "direct",
  "planType": "to_20x",
  "upgradeMode": "new_card",
  "cardNumber": "4242424242424242",
  "expMonth": 12,
  "expYear": 2032,
  "cvv": "123",
  "token": {
    "user": {
      "id": "user-example",
      "email": "user@example.com"
    },
    "expires": "2026-11-01T08:34:59.567Z",
    "account": {
      "id": "account-example",
      "planType": "prolite"
    },
    "accessToken": "<chatgpt_access_token>",
    "sessionToken": "<chatgpt_session_token>"
  }
}
```

### 处理说明

- `token.account.planType` 必须为 `go`、`plus` 或 `prolite`（也接受 `chatgptgoplan` / `chatgptprolite`）。免费号、已是 20X、企业/团队都会被拒绝（`40030`）。
- 同卡升级不要传 `cardNumber` / `expMonth` / `expYear` / `cvv`。
- 新卡升级必须传完整卡信息；缺少卡号时返回 `40020`，消息为 `新卡升级请填写 cardNumber / expMonth / expYear / cvv`。
- 创建响应里的 `plan_type` 为 `to_20x`，`upgrade_mode` 为 `same_card` 或 `new_card`。升级成功后 ChatGPT 侧套餐为 `chatgptpro`（20X），并会取消自动续费。
- 别名：`upgrade_20x`、`pro20_upgrade`，内部规范成 `to_20x`。
- `orderType=card_key` 也可以下 `to_20x`：同卡升级不从平台卡库选卡；`upgradeMode=new_card` 会从卡库选卡，卡库为空时返回 `40012`。

## 创建 25X 升级订单

已有 Go、Plus、5X 或 20X 账号用 `planType=to_25x` 升到 25X。免费号请走 `pro25`。用 `upgradeMode` 选择同卡或新卡。成功后会取消自动续费。

### 同卡升级请求示例

账号上已有默认支付卡时，不要再传卡号。`upgradeMode` 可省略，默认 `same_card`。Go 账号把 `account.planType` 写成 `go`，5X 写成 `prolite`，20X 写成 `pro`。

```json
{
  "orderType": "direct",
  "planType": "to_25x",
  "upgradeMode": "same_card",
  "token": {
    "user": {
      "id": "user-example",
      "email": "user@example.com"
    },
    "expires": "2026-11-01T08:34:59.567Z",
    "account": {
      "id": "account-example",
      "planType": "plus"
    },
    "accessToken": "<chatgpt_access_token>",
    "sessionToken": "<chatgpt_session_token>"
  }
}
```

### 新卡升级请求示例

```json
{
  "orderType": "direct",
  "planType": "to_25x",
  "upgradeMode": "new_card",
  "cardNumber": "4242424242424242",
  "expMonth": 12,
  "expYear": 2032,
  "cvv": "123",
  "token": {
    "user": {
      "id": "user-example",
      "email": "user@example.com"
    },
    "expires": "2026-11-01T08:34:59.567Z",
    "account": {
      "id": "account-example",
      "planType": "pro"
    },
    "accessToken": "<chatgpt_access_token>",
    "sessionToken": "<chatgpt_session_token>"
  }
}
```

### 处理说明

- `token.account.planType` 必须为 `go`、`plus`、`prolite` 或 `pro`（也接受 `chatgptgoplan` / `chatgptprolite` / `chatgptpro`）。免费号、已是 25X、企业/团队都会被拒绝（`40030`）。
- 同卡升级不要传 `cardNumber` / `expMonth` / `expYear` / `cvv`。
- 新卡升级必须传完整卡信息；缺少卡号时返回 `40020`，消息为 `新卡升级请填写 cardNumber / expMonth / expYear / cvv`。
- 创建响应里的 `plan_type` 为 `to_25x`，`upgrade_mode` 为 `same_card` 或 `new_card`。升级成功后 ChatGPT 侧套餐为 `chatgptpromax`（25X），并会取消自动续费。
- 别名：`upgrade_25x`、`pro25_upgrade`，内部规范成 `to_25x`。`pro25` 仍是 25X 正价开通，不是这条升级。
- `orderType=card_key` 也可以下 `to_25x`：同卡升级不从平台卡库选卡；`upgradeMode=new_card` 会从卡库选卡，卡库为空时返回 `40012`。

## 创建 20X 续费订单

Pro 20X 账号用 `planType=renew_20x` 绑定新卡并开启自动续费。暂停/欠费账号会先支付到期发票。不是 20X 正价开通，不会新建 checkout。纯到期且无欠费发票时请走 `pro20`。

### 请求示例

```json
{
  "orderType": "direct",
  "planType": "renew_20x",
  "cardNumber": "4242424242424242",
  "expMonth": 12,
  "expYear": 2032,
  "cvv": "123",
  "token": {
    "user": {
      "id": "user-example",
      "email": "user@example.com"
    },
    "expires": "2026-11-01T08:34:59.567Z",
    "account": {
      "id": "account-example",
      "planType": "pro"
    },
    "accessToken": "<chatgpt_access_token>",
    "sessionToken": "<chatgpt_session_token>"
  }
}
```

### 处理说明

- `token.account.planType` 必须为 `pro`（也接受 `chatgptpro`）。免费号、Plus、5X 都会被拒绝（`40030`）。
- 直充必须传完整银行卡。`orderType=card_key` 从平台卡库选卡。
- worker 会先确认订阅仍是 20X。暂停/欠费时只支付托管 due 发票（不加卡、不 renew）。未暂停则加卡并设为默认卡；若 `will_renew` 已为 `true` 则不再调用 renew，否则 `POST /subscriptions/renew`。纯到期且无欠费发票会失败。
- 已到期的 20X 不能走本接口，请改用 `planType=pro20` 正价开通。
- 创建响应里的 `plan_type` 为 `renew_20x`。成功后 ChatGPT 侧仍为 Pro 20X，自动续费保持开启，`is_subscription_cancelled` 为 `0`。
- 别名：`20x_renew`、`pro20_renew`，内部规范成 `renew_20x`。

## 创建 Codex 点数订单

任意已登录账号用 `planType=codex` 一次性购买 Codex 点数。必须传银行卡。成功后不会取消自动续费。

### 请求字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `planType` | 是 | 取值 `codex`。也接受 `codex_credit` / `codex_credits` / `credits` / `credit`，内部规范成 `codex`。 |
| `creditQuantity` | 否 | 购买点数。整数 `1–25000`，省略默认 `500`。常用 `250` / `500` / `2500` / `5000`。 |
| `cardNumber` / `expMonth` / `expYear` / `cvv` | 直充必填 | 与其他带卡套餐相同。`orderType=card_key` 从平台卡库选卡。 |
| `token.account.planType` | 是 | 任意已登录账号：`free` / `plus` / `prolite` / `pro` 均可。缺少该字段返回 `40030`。 |
| `region` | 否 | 决定结账货币和代理。不传为 `PH` / `PHP`。传 `auto` 时按账号现有订阅货币自动选择：`PHP`→菲律宾，`USD`→美国，`EGP`→埃及，`CLP`→智利。没有对应节点，或读不到订阅货币（含手机订阅），改走菲律宾 `PH` / `PHP`。返回的 `region` 是解析后的国家码。 |

### 请求示例

```json
{
  "orderType": "direct",
  "planType": "codex",
  "creditQuantity": 500,
  "cardNumber": "4242424242424242",
  "expMonth": 12,
  "expYear": 2032,
  "cvv": "123",
  "token": {
    "user": {
      "id": "user-example",
      "email": "user@example.com"
    },
    "expires": "2026-11-01T08:34:59.567Z",
    "account": {
      "id": "account-example",
      "planType": "plus"
    },
    "accessToken": "<chatgpt_access_token>",
    "sessionToken": "<chatgpt_session_token>"
  }
}
```

### 创建成功返回

HTTP `201`，`code=0`。创建成功只表示已入队，最终以状态查询 `status=success` 为准。

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "order_no": "12",
    "status": "processing",
    "email": "user@example.com",
    "pay_link": null,
    "payment_amount": null,
    "payment_currency": null,
    "message": "下单成功，订单已进入队列处理，支付链接正在生成",
    "card_key": "DIRECT-example",
    "order_type": "direct",
    "plan_type": "codex",
    "credit_quantity": 500,
    "region": "PH"
  }
}
```

上例未传 `region`，所以返回 `PH`。若要按账号订阅货币自动选择，在同一请求里增加 `"region": "auto"`，其余字段不变。订阅货币是 `PHP` 时，创建响应的 `region` 为 `PH`；是 `USD` 时为 `US`。不会返回字面量 `auto`。

### 状态查询成功返回

轮询到 `status=success` 后停止。`is_subscription_cancelled` 为 `0` 即符合预期（本单不会取消自动续费）。`credit_quantity` 为下单时购买的点数。

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "order_no": "12",
    "order_type": "direct",
    "plan_type": "codex",
    "credit_quantity": 500,
    "region": "PH",
    "card_key": "DIRECT-example",
    "email": "user@example.com",
    "pay_link": null,
    "payment_amount": null,
    "payment_currency": null,
    "is_subscription_cancelled": 0,
    "status": "success",
    "failure_reason": null,
    "payment_result": {"success": true, "status": "paid"},
    "finished_at": "2026-8-16 17:32:00",
    "created_at": "2026-8-16 17:30:00",
    "updated_at": "2026-8-16 17:32:00"
  }
}
```

### 处理说明

- `token.account.planType` 只要有值即可（free / plus / prolite / pro 均可）。缺少 `planType` 会返回 `40030`。
- 直充必须传完整银行卡。`orderType=card_key` 从平台卡库选卡，卡库为空时返回 `40012`。
- `creditQuantity` 省略时默认 `500`。合法范围 `1–25000`。常用预设 `250` / `500` / `2500` / `5000`。非法值返回 `40020`。
- 别名：`codex_credit`、`codex_credits`、`credits`、`credit`，内部规范成 `codex`。
- 不改 ChatGPT 订阅套餐，成功后账号仍是原来的 Plus / Go / 5X / 20X / 免费号。

## 创建卡密订单

> **维护状态提示**：卡密类型订单当前可用，但仅作为存量系统兼容能力保留。新系统请使用 `orderType=direct`。

卡密订单使用平台卡库选卡。套餐由请求体 `planType` 决定；未传时默认 `plus`。

### 请求示例

```json
{
  "orderType": "card_key",
  "cardKey": "ABCDEF-123456",
  "planType": "plus",
  "token": {
    "user": {
      "id": "user-example",
      "email": "user@example.com"
    },
    "expires": "2026-11-01T08:34:59.567Z",
    "account": {
      "id": "account-example"
    },
    "accessToken": "<chatgpt_access_token>",
    "sessionToken": "<chatgpt_session_token>"
  }
}
```

### 卡密专用字段

- `cardKey`
  - 类型：字符串。
  - 必填：`orderType=card_key` 时必填。
  - 长度：`1-128` 个字符。
  - 含义：本笔订单的查询标识，后续状态查询仍用这个值。
  - 注意：本站不校验该卡密是否真实存在，任意 `1-128` 字符均可。套餐看 `planType`。

- `planType`
  - 类型：字符串。
  - 必填：否。
  - 可选值：`plus`、`go`（别名 `chatgpto` / `chatgptgo` / `chatgptgoplan`）、`pro5`、`pro20`、`pro25`（别名 `25x` / `chatgptpromax`）、`plus_to_5x`（别名 `plus5x` / `to_5x` / `upgrade_5x`）、`renew_20x`（别名 `20x_renew` / `pro20_renew`）、`to_20x`（别名 `upgrade_20x` / `pro20_upgrade`）、`codex`（别名 `codex_credit` / `codex_credits` / `credits` / `credit`）、`team`（别名 `chatgptteam` / `chatgptteamplan`）。
  - 默认：`plus`。
  - 含义：需要开通、升级、续费或充值点数的套餐。卡密订单的套餐由本字段决定，不由 `cardKey` 记录决定。`plus_to_5x` / `to_20x` 同卡升级不从卡库选卡；新卡升级、`renew_20x` 与 `codex` 从卡库选卡。

- `upgradeMode`
  - 类型：字符串。
  - 必填：否。仅 `planType=plus_to_5x` 或 `to_20x` 时生效。
  - 可选值：`same_card`、`new_card`。
  - 含义：同卡升级不选库内卡；新卡升级从平台卡库选一张卡再加卡升级。

- `creditQuantity`
  - 类型：整数。
  - 必填：否。仅 `planType=codex` 时生效，范围 `1-25000`，省略默认 `500`。

- `region`
  - 类型：字符串。
  - 必填：否。
  - 默认：不传则为 `PH`（菲律宾 / PHP）。
  - 含义：与直充订单相同，决定结账地区、货币和代理出口。显式传入国家码必须对该套餐已开放。`planType=codex` 可传 `auto`，规则与直充相同。

### 卡密订单不需要的字段

`orderType=card_key` 时不需要传以下字段：

- `cardNumber`
- `expMonth`
- `expYear`
- `cvv`

即使传入这些字段，卡密订单也不会使用它们。建议下游在卡密订单中完全省略这些字段；如果仍然传入，它们会先经过 Schema 格式校验，例如错误格式的 `cardNumber` 仍可能导致 HTTP `422`。

### 卡密订单处理说明

- 订单归属固定为当前请求头中的 `X-API-Key` 字符串。
- `plus` / `pro5` / `pro20` / `pro25` / `renew_20x` / `codex`：worker 从平台卡库中选择可用卡。没有可用银行卡时返回 `40012`，消息为 `暂无可用卡信息`。
- `plus_to_5x` 同卡：不选库内卡，走同卡升级；没有可用卡库也不会返回 `40012`。
- `plus_to_5x` 新卡：从卡库选卡后加卡并设默认，再差价升级；卡库为空时返回 `40012`。
- `to_20x` 同卡：不选库内卡，走差价升级到 chatgptpro。
- `to_20x` 新卡：从卡库选卡后加卡并设默认，再差价升级到 20X；卡库为空时返回 `40012`。

## 创建订单成功响应

`direct` 和 `card_key` 使用相同的成功响应结构。

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "order_no": "12",
    "status": "processing",
    "email": "user@example.com",
    "pay_link": null,
    "payment_amount": null,
    "payment_currency": null,
    "message": "下单成功，订单已进入队列处理，支付链接正在生成",
    "card_key": "DIRECT-example",
    "order_type": "direct",
    "plan_type": "pro5",
    "region": "PH"
  }
}
```

### 创建响应字段

- `order_no`
  - 类型：字符串。
  - 含义：本次创建的订单号（任务 ID）。
  - 注意：查询业务状态应优先使用 `card_key`。

- `status`
  - 类型：字符串。
  - 创建成功固定值：`processing`。
  - 含义：订单已进入异步处理阶段，不代表开通成功。

- `email`
  - 类型：字符串。
  - 含义：从 `token.user.email` 提取的开通账号邮箱。

- `pay_link`
  - 类型：`null`。
  - 含义：为避免暴露内部 checkout 链接，所有三方接口响应固定返回 `null`。

- `payment_amount`
  - 类型：十进制金额或 `null`。
  - 含义：本次套餐实际支付金额。
  - 注意：当前创建成功时固定为 `null`。

- `payment_currency`
  - 类型：字符串或 `null`。
  - 示例：`PHP`、`USD`、`EGP`、`CLP`。
  - 含义：支付金额对应的货币单位，跟订单 `region` 走。
  - 注意：当前创建成功时固定为 `null`。实际结账：未传 `region` 为 `PHP`；`US` 为 `USD`；`EG` 为 `EGP`；`CL` 为 `CLP`。Codex 传 `auto` 时，货币跟解析后的地区走。

- `region`
  - 类型：字符串。
  - 示例：`PH`、`US`、`EG`、`CL`。
  - 含义：本单实际使用的结账地区。未传时返回 `PH`。请求传 `auto` 时，这里返回解析后的国家码，不返回 `auto`。

- `message`
  - 类型：字符串。
  - 含义：订单创建结果说明。
  - 注意：返回 `支付链接正在生成` 时表示订单已成功入队，后续由 worker 异步执行开通。

- `card_key`
  - 类型：字符串。
  - 含义：后续查询状态的业务标识。
  - `direct`：系统生成的 `DIRECT-...` 随机卡密。
  - `card_key`：请求中传入的业务卡密。
  - 注意：下游必须持久化保存此字段。

- `order_type`
  - 类型：字符串。
  - 可选值：`direct`、`card_key`。
  - 含义：实际创建的订单类型。

- `plan_type`
  - 类型：字符串。
  - 可选值：`plus`、`go`、`pro5`、`pro20`、`pro25`、`plus_to_5x`、`renew_20x`、`to_20x`、`codex`、`team`。
  - `direct`：来自请求体 `planType`（`chatgpto` / `chatgptgo` / `chatgptgoplan` 规范成 `go`；`25x` / `chatgptpromax` 规范成 `pro25`；`plus5x` / `to_5x` / `upgrade_5x` 规范成 `plus_to_5x`；`20x_renew` / `pro20_renew` 规范成 `renew_20x`；`upgrade_20x` / `pro20_upgrade` 规范成 `to_20x`；`codex_credit` / `credits` 规范成 `codex`）。
  - `card_key`：来自请求体 `planType`，省略时为 `plus`。

- `upgrade_mode`
  - 类型：字符串。
  - 仅 `plan_type=plus_to_5x` 或 `to_20x` 时返回：`same_card` 或 `new_card`。
  - 其他套餐不返回该字段。

- `credit_quantity`
  - 类型：整数。
  - 仅 `plan_type=codex` 时返回：本次购买的点数。
  - 其他套餐不返回该字段。

- `region`
  - 类型：字符串。
  - 示例：`PH`、`US`、`EG`、`CL`。
  - 含义：本单结账地区。请求未传 `region` 时为 `PH`。Codex 请求传 `auto` 时，这里是按订阅货币解析后的国家码。

## 创建订单 cURL 示例

### 直充订单

下面示例显式传入埃及。若省略 `region` 或传空字符串，则结账地区为菲律宾 `PH` / `PHP`。

```bash
curl -X POST "https://example.com/api/v1/third-party/orders/direct" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <cdk_code>" \
  -d '{
    "orderType": "direct",
    "cardNumber": "4242424242424242",
    "expMonth": 12,
    "expYear": 2032,
    "cvv": "123",
    "token": {
      "user": {"id": "user-example", "email": "user@example.com"},
      "expires": "2026-11-01T08:34:59.567Z",
      "account": {"id": "account-example"},
      "accessToken": "<chatgpt_access_token>",
      "sessionToken": "<chatgpt_session_token>"
    },
    "planType": "pro5",
    "region": "EG"
  }'
```

### 5X升级（同卡）

```bash
curl -X POST "https://example.com/api/v1/third-party/orders/direct" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <cdk_code>" \
  -d '{
    "orderType": "direct",
    "planType": "plus_to_5x",
    "upgradeMode": "same_card",
    "token": {
      "user": {"id": "user-example", "email": "user@example.com"},
      "expires": "2026-11-01T08:34:59.567Z",
      "account": {"id": "account-example", "planType": "plus"},
      "accessToken": "<chatgpt_access_token>",
      "sessionToken": "<chatgpt_session_token>"
    }
  }'
```

### 5X升级（新卡）

```bash
curl -X POST "https://example.com/api/v1/third-party/orders/direct" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <cdk_code>" \
  -d '{
    "orderType": "direct",
    "planType": "plus_to_5x",
    "upgradeMode": "new_card",
    "cardNumber": "4242424242424242",
    "expMonth": 12,
    "expYear": 2032,
    "cvv": "123",
    "token": {
      "user": {"id": "user-example", "email": "user@example.com"},
      "expires": "2026-11-01T08:34:59.567Z",
      "account": {"id": "account-example", "planType": "plus"},
      "accessToken": "<chatgpt_access_token>",
      "sessionToken": "<chatgpt_session_token>"
    }
  }'
```

### 20X 续费

```bash
curl -X POST "https://example.com/api/v1/third-party/orders/direct" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <cdk_code>" \
  -d '{
    "orderType": "direct",
    "planType": "renew_20x",
    "cardNumber": "4242424242424242",
    "expMonth": 12,
    "expYear": 2032,
    "cvv": "123",
    "token": {
      "user": {"id": "user-example", "email": "user@example.com"},
      "expires": "2026-11-01T08:34:59.567Z",
      "account": {"id": "account-example", "planType": "pro"},
      "accessToken": "<chatgpt_access_token>",
      "sessionToken": "<chatgpt_session_token>"
    }
  }'
```

### Codex 点数充值

```bash
curl -X POST "https://example.com/api/v1/third-party/orders/direct" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <cdk_code>" \
  -d '{
    "orderType": "direct",
    "planType": "codex",
    "creditQuantity": 500,
    "cardNumber": "4242424242424242",
    "expMonth": 12,
    "expYear": 2032,
    "cvv": "123",
    "token": {
      "user": {"id": "user-example", "email": "user@example.com"},
      "expires": "2026-11-01T08:34:59.567Z",
      "account": {"id": "account-example", "planType": "plus"},
      "accessToken": "<chatgpt_access_token>",
      "sessionToken": "<chatgpt_session_token>"
    }
  }'
```

### 卡密订单

```bash
curl -X POST "https://example.com/api/v1/third-party/orders/direct" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <cdk_code>" \
  -d '{
    "orderType": "card_key",
    "cardKey": "ABCDEF-123456",
    "planType": "plus",
    "token": {
      "user": {"id": "user-example", "email": "user@example.com"},
      "expires": "2026-11-01T08:34:59.567Z",
      "account": {"id": "account-example"},
      "accessToken": "<chatgpt_access_token>",
      "sessionToken": "<chatgpt_session_token>"
    }
  }'
```

## 查询三方订单状态

`POST /api/v1/third-party/orders/status`

成功 HTTP 状态码：`200 OK`

该接口使用 `card_key` 查询同一业务订单中的最新记录。`cardKey` 可传单个字符串或字符串数组；传字符串时 `data` 返回单个订单对象，传数组时 `data` 按请求顺序返回多个订单对象。

### 请求头

```http
X-API-Key: <cdk_code>
Content-Type: application/json
```

### 请求体

单个卡密查询：

```json
{
  "cardKey": "DIRECT-example"
}
```

批量卡密查询：

```json
{
  "cardKey": ["DIRECT-example-1", "DIRECT-example-2"]
}
```

### 状态查询请求字段

- `cardKey`
  - 类型：字符串或字符串数组。
  - 必填：是。
  - 长度：每个卡密 `1-128` 个字符。
  - 直充订单：传创建接口返回的 `data.card_key`。
  - 卡密订单：传创建订单时使用的业务卡密。
  - 批量查询：数组顺序会保留到响应 `data` 数组；重复卡密会返回重复位置对应的同一订单状态。

- `includeVerification`
  - 类型：布尔。
  - 必填：否。
  - 默认：`true`。
  - 含义：是否返回 `verification`。`cs_live_` 需要人工安全验证时，该对象含 Stripe.js `handleNextAction` 所需的 `client_secret` 与公开 `publishable_key`，供对接方自己的页面完成打码。不需要验证入口时可传 `false`，响应里不会出现该字段。

### 查询归属规则

- 当前 `X-API-Key` 用于识别下单方；状态查询仍按 Key 归属，不要求卡密仍有余额。
- 订单必须满足 `card_key` 匹配，并且下单时使用的 `X-API-Key` 与当前 Key 相同。
- 不属于当前 Key 的卡密统一返回“订单不存在”，避免泄露其他调用方订单。
- 换 Key 后查不到旧 Key 下的订单，请固定使用同一把。

### 最新记录规则

本站一笔业务对应一条开通任务。状态接口按创建时间和订单 ID 倒序返回该卡密最新一条记录。

- `retry_batch_no`：任务编号 `task_no`。
- `retry_attempt`：内部协议重试次数，从 `1` 开始。

下游建议在首次看到 `failed` 时延迟 `2-3` 秒再确认一次。

### 状态查询成功响应

单个卡密查询时，`data` 为单个订单对象：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "order_no": "12",
    "order_type": "direct",
    "plan_type": "pro5",
    "region": "PH",
    "card_key": "DIRECT-example",
    "billing_address_name": null,
    "billing_address_line1": "1072 Washington Street",
    "billing_address_city": "Juneau",
    "billing_address_state": "AK",
    "billing_address_postal": "99801",
    "billing_address_country": "US",
    "bank_card_no": "4242424242424242",
    "email": "user@example.com",
    "pay_link": null,
    "payment_amount": null,
    "payment_currency": null,
    "is_subscription_cancelled": 0,
    "token": {
      "accessToken": "<latest_chatgpt_access_token>",
      "user": {"email": "user@example.com"},
      "account": {"id": "account-1"},
      "sessionToken": "<chatgpt_session_token>"
    },
    "retry_batch_no": "A1B2C3D4E5F60789",
    "retry_attempt": 1,
    "status": "processing",
    "failure_reason": null,
    "payment_result": null,
    "verification": null,
    "finished_at": null,
    "created_at": "2026-8-16 17:30:00",
    "updated_at": "2026-8-16 17:30:00"
  }
}
```

批量卡密查询时，`data` 为订单对象数组，顺序与请求 `cardKey` 数组一致：

```json
{
  "code": 0,
  "message": "success",
  "data": [
    {
      "order_no": "12",
      "order_type": "direct",
      "plan_type": "pro5",
      "region": "PH",
      "card_key": "DIRECT-example-1",
      "token": {
        "accessToken": "<chatgpt_access_token>",
        "user": {"email": "user@example.com"},
        "account": {"id": "account-1"}
      },
      "retry_batch_no": "A1B2C3D4E5F60789",
      "retry_attempt": 1,
      "status": "processing",
      "failure_reason": null,
      "payment_result": null,
      "verification": null,
      "finished_at": null,
      "created_at": "2026-8-16 17:30:00",
      "updated_at": "2026-8-16 17:30:00"
    },
    {
      "order_no": "13",
      "order_type": "direct",
      "plan_type": "plus",
      "card_key": "DIRECT-example-2",
      "token": {
        "accessToken": "<latest_chatgpt_access_token>",
        "user": {"email": "user@example.com"},
        "account": {"id": "account-1"}
      },
      "retry_batch_no": "B2C3D4E5F607891A",
      "retry_attempt": 1,
      "status": "success",
      "failure_reason": null,
      "payment_result": {"success": true, "status": "paid"},
      "is_subscription_cancelled": 1,
      "finished_at": "2026-8-16 17:32:00",
      "created_at": "2026-8-16 17:30:00",
      "updated_at": "2026-8-16 17:32:00"
    }
  ]
}
```

### 状态响应字段

- `order_no`
  - 类型：字符串。
  - 含义：当前订单号。

- `order_type`
  - 类型：字符串。
  - 可选值：`direct`、`card_key`。

- `plan_type`
  - 类型：字符串。
  - 可选值：`plus`、`go`、`pro5`、`pro20`、`pro25`、`plus_to_5x`、`renew_20x`、`to_20x`、`codex`、`team`。

- `region`
  - 类型：字符串。
  - 示例：`PH`、`US`、`EG`、`CL`。
  - 含义：下单时使用的结账地区。未传时为 `PH`。Codex 下单传 `auto` 时，这里是解析后的国家码。

- `upgrade_mode`
  - 类型：字符串。
  - 仅 `plan_type=plus_to_5x` 或 `to_20x` 时返回：`same_card` 或 `new_card`（有新卡则为 `new_card`）。

- `credit_quantity`
  - 类型：整数。
  - 仅 `plan_type=codex` 时返回：本次购买的点数。

- `card_key`
  - 类型：字符串或 `null`。
  - 含义：当前业务订单的查询卡密。

- `billing_address_name`
  - 类型：字符串或 `null`。
  - 含义：账单姓名快照。当前可能为 `null`。

- `billing_address_line1`
  - 类型：字符串或 `null`。
  - 含义：账单地址第一行。开通时使用美国免税州随机地址。

- `billing_address_city`
  - 类型：字符串或 `null`。
  - 含义：账单城市。

- `billing_address_state`
  - 类型：字符串或 `null`。
  - 含义：账单州。

- `billing_address_postal`
  - 类型：字符串或 `null`。
  - 含义：账单邮编。

- `billing_address_country`
  - 类型：字符串或 `null`。
  - 含义：账单国家代码，通常为 `US`。

- `bank_card_no`
  - 类型：字符串或 `null`。
  - 含义：当前支付使用的银行卡号。
  - 安全：属于敏感数据，下游不得在普通日志或前端页面完整展示。

- `email`
  - 类型：字符串或 `null`。
  - 含义：开通目标账号邮箱。

- `pay_link`
  - 类型：`null`。
  - 含义：固定返回 `null`。

- `payment_amount`
  - 类型：十进制金额或 `null`。
  - 含义：当前支付金额。当前可能为 `null`。

- `payment_currency`
  - 类型：字符串或 `null`。
  - 含义：支付货币单位，跟 `region` 走（`PH`→`PHP`，`US`→`USD`，`EG`→`EGP`，`CL`→`CLP`）。Codex 传 `auto` 时按解析后的地区返回。支付完成前可能为 `null`。

- `is_subscription_cancelled`
  - 类型：整数。
  - `0`：未确认取消续费，或本单本来就不会取消（例如 `renew_20x`、`codex`）。
  - `1`：已取消自动续费，本期权益保留至到期日。
  - 更新时效：正价开通、5X升级、20X 升级成功后，系统会再查一次套餐并取消续费。刚完成时该字段可能仍为 `0`。Grok 取消续费见 Grok 文档。
  - `renew_20x` / `codex`：成功后保持自动续费，该字段为 `0` 即符合预期，无需再等取消续费。
  - 结果判定：建议在订单 `success` 后约 `1` 分钟再查一次；若仍为 `0` 且不是 20X 续费或 Codex 点数，表示取消续费未完成，开通本身仍然成功。

- `token`
  - 类型：JSON 对象或 `null`。
  - 含义：订单保存的完整 ChatGPT Session。开通过程中一旦刷新出新 accessToken，会立刻写回该对象。
  - 安全：可能包含 `accessToken`、`sessionToken` 等敏感凭证，只能在受信任的服务端使用；禁止写入普通日志、返回前端或转发给无关第三方。

- `retry_batch_no`
  - 类型：字符串。
  - 含义：本笔任务编号。

- `retry_attempt`
  - 类型：整数。
  - 含义：内部协议尝试次数，从 `1` 开始。

- `status`
  - 类型：字符串。
  - 可选值：`pending`、`processing`、`success`、`failed`。

- `failure_reason`
  - 类型：字符串或 `null`。
  - 含义：订单失败原因。非失败状态通常为 `null`。
  - 脱敏规则：验证码或挑战失败统一返回 `验证策略失败，请稍后重试`。

- `payment_result`
  - 类型：JSON 对象或 `null`。
  - 成功示例：`{"success": true, "status": "paid"}`。
  - 失败示例：`{"success": false, "status": "failed", "failure_detail": "..."}`。
  - 代理脱敏：对象任意层级中名为 `proxy` 的字段固定返回 `null`。

- `verification`
  - 类型：JSON 对象或 `null`。默认返回；请求 `includeVerification=false` 时整字段省略。
  - 无待完成的 `cs_live_` 安全验证时为 `null`（含 oaics_ 订单、已结束订单）。
  - 等待打码时示例：

```json
{
  "mode": "stripe",
  "client_secret": "pi_3Sxxxxxxxx_secret_xxxxxxxx",
  "publishable_key": "pk_live_...",
  "expires_at": "2026-09-21T10:15:00Z"
}
```

  - `mode`：固定 `stripe`。对接方在**自己的 HTTPS 页面**加载 `https://js.stripe.com/v3/`，然后：

```javascript
const stripe = Stripe(verification.publishable_key)
const { error } = await stripe.handleNextAction({
  clientSecret: verification.client_secret
})
```

  - 挑战 token 由 Stripe.js 提交给 Stripe，本站不接收、不转发 hCaptcha token。
  - `handleNextAction` 完成或关闭弹层**不等于**付款成功。继续用同一个 `card_key` 轮询，直到 `status=success` 或 `status=failed`。
  - 请遵守 `expires_at`（120 秒）。超时后 `verification` 变为 `null`，订单记为失败，并提示核对原单。
  - `client_secret` 只能给本次订单的持有方前端；禁止写入普通日志、禁止转给无关第三方。

- `finished_at`
  - 类型：时间字符串或 `null`。
  - 含义：订单进入最终状态的时间。

- `created_at`
  - 类型：时间字符串。
  - 含义：当前订单记录创建时间。

- `updated_at`
  - 类型：时间字符串。
  - 含义：当前订单记录最后更新时间。

### 状态枚举与轮询建议

- `pending`
  - 含义：订单已创建，尚未开始处理（内部 `queued`）。
  - 建议：继续轮询。

- `processing`
  - 含义：订单正在排队或执行开通。若同时 `verification` 不为 `null`，表示正在等待对接方完成 Stripe 安全验证（120 秒内），此时仍是 `processing`，不是成功。
  - 建议：`verification` 为空则继续轮询；不为空则先完成打码，再继续轮询，直到 `success` / `failed`。不要把完成弹层当成付款成功。

- `success`
  - 含义：正价开通、升级、续费或 Codex 点数充值成功。
  - 建议：停止轮询，按成功处理。可再读 `is_subscription_cancelled`、`credit_quantity`（仅 Codex）和最新 `token`。

- `failed`
  - 含义：当前订单失败或已取消。
  - 建议：延迟 `2-3` 秒再确认一次后停止轮询，并读取 `failure_reason` 和 `payment_result`。

推荐轮询策略：

- 创建成功后等待 `2-3` 秒再开始首次查询。
- 前 `30` 秒每 `2-3` 秒查询一次。
- 之后可降低为每 `5-10` 秒查询一次。
- 查询到 `success` 后立即停止。
- 查询到 `failed` 后等待 `2-3` 秒再确认一次后停止。
- `verification` 不为 `null` 时先在对接方面完成 `handleNextAction`，不要停止轮询。
- 下游应设置总超时时间，超时后保留 `card_key`，允许后续补查。
- 不要每秒高频轮询。

### 状态查询 cURL 示例

单个卡密查询：

```bash
curl -X POST "https://example.com/api/v1/third-party/orders/status" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <cdk_code>" \
  -d '{
    "cardKey": "DIRECT-example"
  }'
```

批量卡密查询：

```bash
curl -X POST "https://example.com/api/v1/third-party/orders/status" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <cdk_code>" \
  -d '{
    "cardKey": ["DIRECT-example-1", "DIRECT-example-2"]
  }'
```

## 查询过往开通记录

`POST /api/v1/third-party/orders/history`

成功 HTTP 状态码：`200 OK`

按当前 `X-API-Key` 分页返回该 Key 下的全部开通记录（含前台填写同一把 Key 的订单）。不返回 Token、完整卡号。站点前台也可打开 `/query` 用同一把 Key 查询。

请求体：

```json
{
  "page": 1,
  "page_size": 20
}
```

```bash
curl -X POST "https://example.com/api/v1/third-party/orders/history" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <api_key>" \
  -d '{"page":1,"page_size":20}'
```

`data` 含 `items`、`total`、`page`、`page_size`；若 Key 是本站发放的卡密/API，另含 `remaining`、`max_uses`、`used_count`。每条记录含 `task_no`、`plan_type`、`status`、`channel`（`api` / `web`）、`cdk_cost`（实际扣点：开通成功为扣除点数；开启「失败也扣点」时失败单也为扣除点数；取消和未完成为 0）、完整 `email`（不脱敏，供客户核对开通账号）、脱敏 `card`、`created_at`、`result_message`。

## 用卡密给 API 充点

`POST /api/v1/third-party/credits/recharge`

成功 HTTP 状态码：`200 OK`

`X-API-Key` 必须是后台 **API管理** 发放的固定 Key。请求体里的 `cdkCode` 必须是 **卡密管理** 里生成、且仍有剩余点数的卡密。充值会把该卡密剩余点数全部转入 API 额度，卡密立即用尽。卡密若有进行中的开通订单，会拒绝充值。站点前台 `/query` 也可完成同样操作。

请求体：

```json
{
  "cdkCode": "BIND-XXXXXXXX"
}
```

```bash
curl -X POST "https://example.com/api/v1/third-party/credits/recharge" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <api_key>" \
  -d '{"cdkCode":"BIND-XXXXXXXX"}'
```

`data` 含 `points`（本次转入点数）、`api_code`、`cdk_code`、`api_remaining`、`api_max_uses`、`api_used_count`。

## 工具使用

工具接口使用同一把 `X-API-Key`，只读查询，不创建订单，不预占、不扣点。

### 订阅查询

`POST /api/v1/third-party/tools/subscription-lookup`

成功 HTTP 状态码：`200 OK`

查询指定 Session 当前账号的官方订阅。只读，不创建订单，不扣点。账号 ID 取自 `token.account.id`。

#### 请求头

```http
X-API-Key: <cdk_code>
Content-Type: application/json
```

#### 请求字段

- `token`
  - 类型：对象。
  - 必填：是。
  - 含义：`chatgpt.com/api/auth/session` 返回的完整 Session JSON，至少包含 `accessToken` 与 `account.id`。也可以传这段 JSON 的字符串。
  - 缺少 `accessToken` 或不是 JSON 时返回 `40020`。

#### 请求示例

```json
{
  "token": {
    "accessToken": "<access_token>",
    "sessionToken": "<session_token>",
    "user": { "id": "user-xxx", "email": "user@example.com" },
    "account": { "id": "account-uuid", "planType": "plus" },
    "expires": "2026-10-19T12:00:00.000Z"
  }
}
```

#### cURL 示例

```bash
curl -X POST "https://example.com/api/v1/third-party/tools/subscription-lookup" \
  -H "X-API-Key: <cdk_code>" \
  -H "Content-Type: application/json" \
  -d '{"token": { "accessToken": "<access_token>", "account": { "id": "account-uuid" }, "user": { "email": "user@example.com" } }}'
```

#### 成功响应

`data.ok` 为 `true`。主要字段：

- `email`、`user_id`、`session_plan`、`account_count`
- `plus_half`：`有资格` 或 `无资格`。促销 ID 为 `plus-3-months-50-pct-off`，或折扣为 50% 时视为有资格。
- `plus_half_id`：命中的促销 ID，没有则为空。
- `queried_at`：北京时间。
- `accounts`：当前账号一张订阅卡，含 `plan_label`、`plan_type`、`subscription_plan`、`active`、`validity`（`足` / `已过期` / `无`）、`status`、`source`、`currency`、`billing_period`、`subscription_id`、`will_renew`、`pro_20x`、`plus_half`、`promo_id`、`cancel_result`、`started_at`、`active_until`、`cancels_at`。
- `steps`：查询步骤，每项有 `title`、`detail`、`ok`。

#### 失败

- Session 不是完整 JSON，或没有 `accessToken` / `account.id`：HTTP `400`，业务码 `40020`。
- 缺少 `X-API-Key`：HTTP `401`，业务码 `40106`。
- 收费模式下 Key 未登记：HTTP `401`，业务码 `40107`。
- 官方订阅接口失败：HTTP `502`，业务码 `50210`，`message` 为失败原因。不扣点。

### 升级查价

`POST /api/v1/third-party/tools/upgrade-quote`

查询当前套餐升到目标套餐需要补的差价。只读，不创建订单，不扣款，不扣点。

等级从低到高是 Free / Go → Plus → 5X（Pro Lite）→ 20X（Pro）→ 25X（Pro Max）。`target_plan` 传 `next` 时，按当前套餐的上一级查价。省略时按 `5x`。也可以写 `targetPlan`。

`token` 与订阅查询相同。成功时 `data` 含 `current_label`、`target_label`、`next_label`、`is_next`、`currency`、`amount_text`、`amount_display`、`amount_minor`、`active_until`、`source`、`card_brand`、`card_last4`、`lines`、`steps`。

Session 无效或目标套餐无法识别：HTTP `400`，业务码 `40020`。当前套餐不高于目标，或暂时查不到差价：HTTP `502`，业务码 `50210`。不扣点。

## 账单查询

`POST /api/v1/third-party/tools/billing-invoices`

查询指定 Session 的发票记录。只读，不创建订单，不扣点。`token` 与订阅查询相同。

成功时 `data` 含 `email`、`count`、`notice`、`invoices`。每条发票含 `date`、`amount`、`status`、`description`、`card`、`slug`。

获取文件：`POST /api/v1/third-party/tools/billing-invoice-file`，字段 `slug` 和 `file_type`（`invoice` 发票，`receipt` 收据，也可写 `fileType`）。成功时 `data.url` 是文件地址。

Session 无效：HTTP `400`，业务码 `40020`。暂时查不到账单或没有这份文件：HTTP `502`，业务码 `50210`。不扣点。

## 错误码说明

### API Key 与账户错误

- `40106`
  - HTTP 状态：`401`。
  - 消息：`请求头中缺少 X-API-Key`。
  - 处理：检查请求头名称和 API Key 是否为空。

- `40107`
  - HTTP 状态：`401`。
  - 消息：`卡密无效或不存在`。
  - 处理：使用本站卡密管理发放的卡密作为 `X-API-Key`。

- `40306`
  - HTTP 状态：`403`。
  - 消息：`卡密已停用` / `卡密已过期` / `卡密剩余次数不足` / `该卡密不能用于…` / `目标必须是后台发放的 API Key` / `请使用卡密管理里生成的卡密充值` / `卡密有进行中的开通，无法充值`。
  - 处理：更换仍有效且额度足够的卡密，或联系管理员调整套餐限制。充值时 `X-API-Key` 必须是 API管理发放的 Key，`cdkCode` 必须是卡密管理发放且无进行中订单的卡密。

- `40305`
  - HTTP 状态：`403`。
  - 消息：系统维护文案，例如 `系统充值功能已关闭`。
  - 处理：等待管理员重新开放后再下单。用户信息和状态查询不受影响。

### Token 错误

- `40005`
  - HTTP 状态：`400`。
  - 消息：`token 格式错误` 或 `token 中缺少 account 信息` / `token 中缺少 account.id`。
  - 原因：`token` 不是 JSON 对象，或缺少 `account.id`。

- `40006`
  - HTTP 状态：`400`。
  - 消息：`token 中缺少 accessToken`。

- `40007`
  - HTTP 状态：`400`。
  - 消息：`token 中缺少 user 信息`。

- `40008`
  - HTTP 状态：`400`。
  - 消息：`token 中缺少 email`。

- `40024`
  - HTTP 状态：`400`。
  - 消息可能为：`accessToken 格式无效`、`accessToken 无法解析`、`accessToken payload 格式无效`。
  - 原因：不是三段式 JWT、Base64URL payload 无法解码或 payload 不是 JSON 对象。

- `40025`
  - HTTP 状态：`400`。
  - 消息可能为：`accessToken 缺少有效 iat`、`accessToken 缺少有效 exp`、`accessToken 有效时间无效`。

- `40026`
  - HTTP 状态：`400`。
  - 消息：`accessToken 签发时间无效`。
  - 原因：`iat` 比服务器当前时间晚超过 5 分钟。

- `40027`
  - HTTP 状态：`400`。
  - 消息：`accessToken 已过期`。

- `40028`
  - HTTP 状态：`400`。
  - 消息可能为：`token 中缺少 user.id，请提交完整 Session JSON`、`token 中缺少 expires，请提交完整 Session JSON`、`token 中缺少 sessionToken，请提交完整 Session JSON`、`sessionToken 格式无效，请提交完整 Session JSON`。
  - 原因：没有提交完整 Session，或只手工拼接了部分字段。

- `40030`
  - HTTP 状态：`400`。
  - 消息可能为：`5X升级仅支持 Go 或 Plus 账号，当前账号套餐为 free`、`20X续费仅支持 Pro 20X 账号，当前账号套餐为 plus`、`Session JSON 中缺少 account.planType，…`、`仅支持 pro / chatgptpro 账号提交，当前账号套餐为 plus`。
  - 原因：`account.planType` 与本次 `planType` 不匹配。正价开通和 `codex` 只要 `account.planType` 有值；`plus_to_5x` 只要 `go` 或 `plus`；`to_20x` 只要 `plus` 或 `prolite`；`renew_20x` 只要 `pro`。

直充订单参数错误使用错误码 `40020`，卡密订单参数错误使用错误码 `40009`。

### 工具错误

- `50210`
  - HTTP 状态：`502`。
  - 消息：订阅查询、升级查价或账单查询的失败原因，例如 `Access Token 已失效，请重新从 chatgpt.com/api/auth/session 复制`、`所选目标不高于当前套餐`、`暂时查不到账单，请稍后重试`。
  - 处理：更换有效 Session，或改选更高一级套餐后重试。这两个接口都不扣点。

### 直充与卡库错误

- `40020`
  - HTTP 状态：`400`。
  - 消息可能为：`orderType 必须是 direct 或 card_key`、`cardNumber 格式无效`、`expMonth 格式无效`、`expYear 格式无效`、`银行卡已过期`、`cvv 格式无效`、`planType 必须是 plus / go / chatgpto / pro5 / pro20 / pro25 / plus_to_5x / renew_20x / to_20x / codex`、`点数必须是整数`、`点数范围 1–25000`、`upgradeMode 必须是 same_card 或 new_card`、`新卡升级请填写 cardNumber / expMonth / expYear / cvv`、`该套餐未开放`、`region 必须是两位国家码，例如 PH、EG、CL`、`region 未对该套餐开放`、`自动地区仅用于 Codex 点数`、`缺少 account_id，无法读取订阅货币，请手动选择结账地区`、`读取账号订阅货币失败，请手动选择结账地区：…`、`账号没有返回订阅货币，请手动选择结账地区`、`账号订阅货币是 XXX，当前没有对应地区，请手动选择结账地区`。

- `40009`
  - HTTP 状态：`400`。
  - 消息可能为：`cardKey 格式无效`、`该套餐未开放`。

- `40012`
  - HTTP 状态：`400`。
  - 消息：`暂无可用卡信息`。
  - 原因：`orderType=card_key` 时平台卡库没有可用卡（含 `renew_20x` 以及 `plus_to_5x` / `to_20x` 新卡升级）。

### 订单查询错误

- `40403`
  - HTTP 状态：`404`。
  - 消息：`订单不存在`。
  - 原因：卡密不存在，或者订单不属于当前 CDK。
  - 安全说明：这两种情况使用相同消息，避免泄露其他调用方订单。

### 系统与队列错误

- `42902`
  - HTTP 状态：`429`。
  - 消息：`当前并发过高，请稍后重试`。
  - 原因：服务器当前可处理的订单量已满，暂时无法接收新的创建订单请求；这不表示请求参数或银行卡信息有误。
  - 处理：请勿高频连续重试，建议等待一段时间后重新下单。

- `50001`
  - HTTP 状态：`500`。
  - 消息：`充值失败，请稍后重试`。
  - 原因：订单入队或内部处理异常。
  - 处理：不要立即无限重试；先使用已有 `card_key` 查询状态，确认订单是否已经创建。

- `50300`
  - HTTP 状态：`502`。
  - 消息：`服务暂时不可用，请稍后重试`。
  - 原因：网关连不上后端服务或等待超时，请求没有到达业务逻辑。
  - 处理：稍后重试。下单类接口重试前先用已有 `card_key` 查询状态，避免重复创建。

### 当前不会返回的错误码

下列错误码可以继续在下游做分支判断，但本站当前实现不会发出：

- `40107`（API Key 无效或不匹配）
- `40301`（用户被禁用）
- `40303`（账户已锁定）
- `40022`（用户积分不足）
- `40402`（卡密不存在）
- `40902`（卡密已被使用）
- `40903`（卡密已冻结）

## 幂等与重复请求

当前创建订单接口没有单独的下游幂等键参数。

下游必须注意：

- 不要因为客户端读取超时就立即重复提交相同订单。
- 收到创建成功响应后必须保存 `card_key`。
- 如果响应不明确但已获得 `card_key`，优先查询状态，不要重复下单。
- 直充订单每次成功进入创建流程都会生成新的随机卡密和订单，重复请求可能产生多笔订单。

建议下游在自身系统中使用业务单号或请求记录实现幂等控制，并保存请求时间、HTTP 状态、业务 `code`、`order_no` 和 `card_key`。

## 安全与合规要求

请求和响应包含高敏感数据，下游必须遵守以下要求：

- 生产环境必须使用 HTTPS。
- 不要记录完整 API Key / CDK。
- 不要记录完整 accessToken 或 sessionToken。
- 不要记录 CVV。
- 不要把完整银行卡号展示在前端或普通日志中。
- 不要将请求示例中的测试卡号用于真实生产支付。
- 不要在工单、聊天、截图或版本库中粘贴真实 token、API Key、CVV 或完整银行卡号。
- 对保存的 `card_key`、`order_no` 和订单状态建立访问权限控制。
- 下游日志建议只记录脱敏卡号、订单号、卡密前缀、业务码和失败原因。

如果真实 accessToken、sessionToken、API Key 或银行卡安全码发生泄露，应立即轮换或撤销相关凭证。

## 下游实现检查清单

上线前确认以下事项：

- 已通过安全方式配置服务地址，并固定使用同一把本站发放的 `X-API-Key`。
- 已接入用户信息接口（本站不按 `points` 扣次）。
- 已实现 HTTP 状态码和业务 `code` 双重判断。
- 已区分 `direct` 与 `card_key` 请求字段。
- 已在 `token` 中原样传入完整 Session JSON（含 `sessionToken`、`user.id`、`expires`）。
- 已在创建成功后持久化 `order_no` 和 `card_key`。
- 已实现基于 `card_key` 的状态轮询。
- 已在 `success` 或 `failed` 后停止轮询。
- 已处理 HTTP `422` 参数校验错误。
- 已处理请求超时、网络错误和服务端 `5xx`。
- 已实现下游侧防重复提交策略。
- 已按成功订单读取最新 `token.accessToken`（开通后旧 Token 会失效）。
- 已按 `planType=codex` 传 `creditQuantity`（可省略，默认 `500`），并在成功响应中读取 `credit_quantity`；`is_subscription_cancelled=0` 对 Codex 是预期结果。需要按订阅货币选地区时传 `region=auto`，并读取响应里解析后的 `region`，不要把 `auto` 当成订单地区。
