/** review-v12 M2: procurement jobs, sources per variant and the supplier cost report. */
import { seg, type ApiClient, type Paginated } from '@24goods/shared'
import type { SupplierEnv } from './api-suppliers'

export type Routing = 'priority' | 'round_robin'
export type JobAction = 'requery' | 'retry' | 'switch' | 'fulfil' | 'fail' | 'void'

export interface ProcSource {
  id: string
  product_id: string
  variant_id: string | null
  supplier_id: string
  supplier_code: string
  supplier_env: SupplierEnv
  supplier_enabled: boolean
  supplier_breaker_open: boolean
  kind: 'cdk' | 'vcc' | 'direct'
  params: SourceParams
  /** decimal string or null */
  cost_estimate: string | null
  currency: string
  priority: number
  enabled: boolean
  last_check_ok: boolean | null
  last_check_at: string | null
  created_at: string
}
/** Code products use `plan` (+ `payment_country`); card products use `product_code` and the amounts. */
export interface SourceParams {
  plan?: string
  product?: string
  payment_country?: string
  product_code?: string
  /** decimal strings */
  init_amount?: string
  recharge_amount?: string
  first_name?: string
  last_name?: string
  /** direct orders: `product` (gpt | claude | grok), `plan`, our funding card id, optional currency */
  card_id?: number
  /** issue #29: `fixed` (default, needs card_id) | `auto_existing` (the vendor's pick); `auto_issue` is refused */
  card_mode?: 'fixed' | 'auto_existing'
  /** let the vendor switch card after a refused payment (extra failed attempts may cost fees) */
  vendor_switch?: boolean
  payment_currency?: string
}
export interface SourceListing {
  product_id: string
  variant_id: string | null
  routing: Routing
  items: ProcSource[]
}
export interface SourceInput {
  product_id: string
  variant_id?: string | null
  supplier_id: string
  params: SourceParams
  cost_estimate?: string
  currency?: string
  priority?: number
  enabled?: boolean
}
export interface SourcePatch {
  params?: SourceParams
  cost_estimate?: string
  clear_cost?: boolean
  currency?: string
  priority?: number
  enabled?: boolean
}
export interface SourceCheck {
  available: boolean
  reason: string | null
  checked_at: string | null
}

export interface ProcJob {
  id: string
  order_id: string
  order_no: string
  unit_no: number
  kind: string
  state: string
  step: string | null
  supplier_id: string | null
  supplier_code: string | null
  supplier_env: SupplierEnv | null
  offer_id: string | null
  error_code: string | null
  attempts: number
  upstream_ref: string | null
  upstream_status: string | null
  /** card jobs: first 6 + last 4 digits and the supplier's card id; never the full number */
  card_mask?: string | null
  card_id?: string | null
  /** direct jobs: masked account e-mail and `product/plan`; never the session */
  account_mask?: string | null
  plan?: string | null
  cost: string | null
  currency: string | null
  next_poll_at: string | null
  manual_by: string | null
  charged_possible: boolean
  /** a request may have reached the supplier (drives charged_possible) */
  maybe_sent?: boolean
  /** a one-off same-key query is pending to find and disable a possibly issued code */
  recovering?: boolean
  actions: JobAction[]
  created_at: string
  updated_at: string
}
export interface JobTimelineRow {
  at: string
  action: string
  actor_id: string | null
  actor_role: string | null
  severity: string | null
  before: unknown
  after: unknown
  reason: string | null
  error_code: string | null
}
/** Issue #30: one change the supplier reported (poll or webhook); text already sanitised. */
export interface SupplierTimelineRow {
  at: string
  src: 'poll' | 'webhook' | string
  status?: string
  stage?: string
  payment_state?: string
  funding_state?: string
  kind?: 'event' | string
  tid?: string
  cat?: string
  step?: string
  code?: string
  msg?: string
  to?: string
  fact?: string
}
export interface ProcJobDetail extends ProcJob {
  /** direct jobs: what the supplier reported, one row per change (newest last) */
  supplier_timeline?: SupplierTimelineRow[]
  vendor_state?: { status?: string; stage?: string; payment_state?: string; funding_state?: string }
  /** the buyer's current step as the buyer sees it */
  buyer_progress?: { step: string; percent: number; outcome: string; reason: string | null }
  tried_offer_ids: string[]
  /** whether a code is stored; the code itself never reaches the console */
  has_code: boolean
  order: {
    id: string
    order_no: string
    status: string
    qty: number
    amount_minor: number
    currency: string
    product_id: string
    variant_id: string | null
    delivery_mode: string
  }
  ledger: { id: string; type: string; amount: string; currency: string; occurred_at: string }[]
  timeline: JobTimelineRow[]
}
export interface JobList extends Paginated<ProcJob> {
  counts: Record<string, number>
}

export type CostGroup = 'day' | 'supplier' | 'type'
export interface CostRange {
  date_from?: string
  date_to?: string
}
export interface CostSummaryRow {
  key: string
  currency: string
  count: number
  debit: string
  credit: string
  net: string
  supplier_code?: string
  supplier_env?: SupplierEnv
}
export interface CostSummary {
  group: CostGroup
  date_from: string
  date_to: string
  items: CostSummaryRow[]
}
export interface MarginRow {
  order_id: string
  order_no: string
  status: string
  paid_at: string | null
  revenue: string
  revenue_currency: string
  jobs: number
  unreconciled_jobs: number
  cost: { currency: string; amount: string; source: 'ledger' | 'job' }[]
  margin: { amount: string; currency: string } | null
}
export interface UnreconciledRow {
  id: string
  supplier_code: string
  external_id: string
  type: string
  amount: string
  currency: string
  balance_after: string | null
  card_mask: string | null
  ref_id: string | null
  occurred_at: string
}

/** Vendor plan keys are short ASCII identifiers; the server validates against the live catalog. */
export const PLAN_RE = /^[a-z][a-z0-9_]{0,47}$/
export const COUNTRY_RE = /^[A-Z]{2}$/
export const COST_RE = /^[0-9]{1,9}(\.[0-9]{1,4})?$/

function clean<T extends object>(q: T): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null && v !== '') out[k] = v as string | number
  return out
}

export function createProcurementApi(c: ApiClient) {
  const base = '/admin/procurement'
  const target = (product_id: string, variant_id: string | null) => clean({ product_id, variant_id })
  const job = (id: string, action: JobAction, body?: object) => c.post<ProcJob>(`${base}/jobs/${seg(id)}/${action}`, body)
  return {
    sources: {
      list: (productId: string, variantId: string | null) => c.get<SourceListing>(`${base}/sources`, target(productId, variantId)),
      create: (b: SourceInput) => c.post<ProcSource>(`${base}/sources`, b),
      update: (id: string, b: SourcePatch) => c.patch<ProcSource>(`${base}/sources/${seg(id)}`, b),
      order: (productId: string, variantId: string | null, ids: string[]) =>
        c.post<SourceListing>(`${base}/sources/order`, { ...target(productId, variantId), ids }),
      routing: (productId: string, variantId: string | null, mode: Routing) =>
        c.put<{ routing: Routing }>(`${base}/sources/routing`, { ...target(productId, variantId), mode }),
      check: (id: string) => c.post<SourceCheck>(`${base}/sources/${seg(id)}/check`),
    },
    jobs: {
      list: (q: { state?: string; order_id?: string; page?: number; page_size?: number }) => c.get<JobList>(`${base}/jobs`, clean(q)),
      get: (id: string) => c.get<ProcJobDetail>(`${base}/jobs/${seg(id)}`),
      requery: (id: string) => job(id, 'requery'),
      retry: (id: string) => job(id, 'retry'),
      switchSource: (id: string) => job(id, 'switch'),
      fulfil: (id: string, code: string, reason: string, card_id?: string, charge_checked?: boolean) =>
        job(id, 'fulfil', clean({ code, reason, card_id, charge_checked })),
      /** ADR-048: payment, supplier order, the job's top-ups and the funding card's spends (read only) */
      chargeCheck: (id: string) => c.get<ChargeCheck>(`${base}/jobs/${seg(id)}/charge-check`),
      void: (id: string, reason: string) => job(id, 'void', { reason }),
      fail: (id: string, reason: string) => job(id, 'fail', { reason }),
    },
    cost: {
      summary: (group: CostGroup, r: CostRange) => c.get<CostSummary>(`${base}/cost/summary`, clean({ group, ...r })),
      margin: (r: CostRange & { page?: number; page_size?: number }) => c.get<Paginated<MarginRow>>(`${base}/cost/margin`, clean(r)),
      unreconciled: (q: { page?: number; page_size?: number }) => c.get<Paginated<UnreconciledRow>>(`${base}/cost/unreconciled`, clean(q)),
      csvUrl: (r: CostRange) => {
        const s = new URLSearchParams(clean(r) as Record<string, string>).toString()
        return c.url(`${base}/cost/export.csv`) + (s ? `?${s}` : '')
      },
    },
  }
}

// ------------------------------------------------------------------ pricing (issue #28)
export interface PricingSettings {
  card_open_fee: string
  topup_fee_rate: string
  topup_fee_fixed: string
  failed_fee: string
  failed_free_count: number
  max_auto_switches: number
  auth_reversal_rate: string
  chargeback_tier1_rate: string
  chargeback_tier1_min_tx: number
  chargeback_tier1_surcharge: string
  chargeback_tier2_rate: string
  chargeback_tier2_deposit: string
  expected_failure_rate: string
  target_margin: string
  /** above 0: recommended price = cost x (1 + markup), target_margin unused (owner rule 2026-11-18) */
  target_markup: string
  round_step_minor: number
  /** 1: the markup goes on the official (supplier) price only, never below the full cost; 0: on the full cost */
  markup_on_price: number
  /** 0 = plain rounding; 1-9 = suggested prices round UP to a whole price ending in this digit */
  price_ending: number
  service_fee_direct: Record<'gpt' | 'grok' | 'claude', string>
  uses_per_card: Record<'standard' | 'pro20x', number>
  fx: Record<string, { per_usd: string; updated_at: string; updated_by: string }>
  fx_bands: Record<string, { min: string; max: string }>
  fx_max_age_days: number
  fx_max_change: string
}
export interface PricingSettingsView {
  settings: PricingSettings
  informational: string[]
  targets: Record<string, string>
  markups?: Record<string, string>
  channel: { id: string; fee_rate: string }
  currency: 'USD'
}
export type PricingSettingsPatch = Partial<Omit<PricingSettings, 'fx' | 'service_fee_direct' | 'uses_per_card' | 'fx_bands'>> & {
  service_fee_direct?: Partial<PricingSettings['service_fee_direct']>
  uses_per_card?: Partial<PricingSettings['uses_per_card']>
  fx?: { currency: string; per_usd: string; confirm?: boolean }
}
export interface UnitRow {
  key: string
  product_id: string
  variant_id: string | null
  offer_id: string | null
  region: string | null
  title: string
  variant_name: string | null
  status: string
  source: { kind: string; supplier: string | null; enabled: boolean | null; priority: number | null }
  price: { minor: number; amount: string; currency: string; usd: string | null }
  cost_usd: Record<'supplier_price' | 'service_fee' | 'topup_fee' | 'card_share' | 'failure_cost' | 'channel_fee' | 'total', string | null>
  gross_usd: string | null
  net_usd: string | null
  gross_margin_pct: string | null
  net_margin_pct: string | null
  target_margin_pct: string
  /** the markup on the official price this unit uses (own / product override, else the default) */
  target_markup_pct?: string | null
  recommended: { amount: string; minor: number; currency: string; usd: string; diff_minor: number; diff: string } | null
  incomplete: string[]
  cost_incomplete: boolean
  fingerprint: string
  fx_stale: boolean
  large_drop: boolean
}
export interface UnitList extends Paginated<UnitRow> {
  fx: PricingSettings['fx']
  truncated: boolean
}
export interface ActualRow extends Omit<MarginRow, 'margin'> {
  paid: string
  reasons: string[]
  fee_estimated: boolean
  revenue_usd: string | null
  cost_usd: string | null
  channel_fee_usd: string | null
  gross_usd: string | null
  net_usd: string | null
  gross_margin_pct: string | null
  net_margin_pct: string | null
  cost_incomplete: boolean
}

export function createPricingApi(c: ApiClient) {
  const base = '/admin/procurement/pricing'
  return {
    settings: () => c.get<PricingSettingsView>(`${base}/settings`),
    saveSettings: (b: PricingSettingsPatch) => c.put<PricingSettingsView>(`${base}/settings`, b),
    setTarget: (product_id: string, variant_id: string | null, margin: string | null) =>
      c.put<{ key: string; margin: string | null }>(`${base}/target`, { product_id, variant_id, margin }),
    setMarkup: (product_id: string, variant_id: string | null, markup: string | null) =>
      c.put<{ key: string; markup: string | null }>(`${base}/target`, { product_id, variant_id, markup }),
    units: (q: { page?: number; page_size?: number }) => c.get<UnitList>(`${base}/units`, clean(q)),
    apply: (r: UnitRow, confirmLargeDrop = false) =>
      c.post<{ applied: boolean; noop?: boolean }>(`${base}/apply`, {
        product_id: r.product_id,
        variant_id: r.variant_id,
        offer_id: r.offer_id,
        current_minor: r.price.minor,
        recommended_minor: r.recommended?.minor,
        fingerprint: r.fingerprint,
        confirm_large_drop: confirmLargeDrop,
      }),
    actual: (r: CostRange & { page?: number; page_size?: number }) => c.get<Paginated<ActualRow>>(`${base}/actual`, clean(r)),
    actualCsvUrl: (r: CostRange) => {
      const s = new URLSearchParams(clean(r) as Record<string, string>).toString()
      return c.url(`${base}/actual.csv`) + (s ? `?${s}` : '')
    },
  }
}

/** issue #27: regions, the half-price check method and the plan x region generator. */
export type HalfMethod = 'vendor_preflight' | 'chatgpt_direct' | 'manual'
export interface ProcOptions {
  region_allowlist: string[]
  half_price_check: HalfMethod
  half_methods: HalfMethod[]
}
export interface VendorRegion {
  country: string
  currency: string
  allowed: boolean
}
export interface ManualCheck {
  id: string
  status: 'manual_pending' | 'manual_approved' | 'manual_refused'
  plan: string
  product_id: string
  variant_id: string | null
  order_id: string | null
  order_no: string | null
  order_status: string | null
  created_at: string
  decided_at: string | null
  /** minutes a `manual_pending` check has waited; `overdue` = a PAID order is held past the SLA */
  waiting_minutes: number | null
  overdue: boolean
}
export interface GenerateInput {
  product_id: string
  supplier_id: string
  vendor_product: 'gpt' | 'claude' | 'grok'
  plans: string[]
  regions: string[]
  card_id?: number
  cost_estimate?: string
  /** per-region cost (region -> decimal string); falls back to `cost_estimate` */
  cost_estimates?: Record<string, string>
}
export interface GenerateResult {
  created: { plan: string; region: string; variant_id: string }[]
  kept: { plan: string; region: string }[]
  skipped: { region: string; reason: 'not_allowed' | 'not_offered' }[]
}

export function createRegionsApi(c: ApiClient) {
  const base = '/admin/procurement'
  return {
    options: () => c.get<ProcOptions>(`${base}/options`),
    saveOptions: (b: { region_allowlist?: string[]; half_price_check?: HalfMethod }) => c.put<ProcOptions>(`${base}/options`, b),
    regions: (product: 'gpt' | 'claude' | 'grok') => c.get<{ available: boolean; regions: VendorRegion[] }>(`${base}/regions`, { product }),
    generate: (b: GenerateInput) => c.post<GenerateResult>(`${base}/generate-variants`, b),
    checks: (status: ManualCheck['status'] | 'all' = 'manual_pending') => c.get<{ items: ManualCheck[] }>(`${base}/half-checks`, { status }),
    decide: (id: string, approve: boolean) => c.post<{ id: string; status: string }>(`${base}/half-checks/${seg(id)}/decision`, { approve }),
  }
}

// ------------------------------------------------------------------ card pool (issue #29 part 2)
// Response shapes marked [I] are inferred from the vendor docs (review-v12 §21.8) and parsed
// defensively by the server; a field the vendor does not send is null here.
export type CardProduct = 'gpt' | 'claude' | 'grok'
export interface CardSupplier {
  id: string
  code: string
  label: string
  env: SupplierEnv
}
export interface UsageBucket {
  used: number | null
  remaining: number | null
  cap: number | null
  failed: number | null
  unlimited: boolean
}
export interface CardUsage {
  readable?: boolean
  light?: UsageBucket
  pro20?: UsageBucket
  cooling?: boolean
  busy?: boolean
  /** [I] not a documented field */
  locked?: boolean
  /** [I] consecutive failures */
  failure_streak?: number | null
}
export type RiskLevelName = 'ok' | 'warning' | 'critical'
export interface RiskView {
  sent: number
  declined: number
  in_flight: number
  /** a decimal fraction ("0.1667") or null for an empty period */
  rate: string | null
  level: RiskLevelName
  tier1: 'ok' | 'near' | 'over'
  tier2: 'ok' | 'near' | 'over'
}
export interface PoolCard {
  id: number
  last4: string | null
  product_code: string
  status: string
  balance: string | null
  usage: CardUsage
  ours_30d: RiskView
}
export interface CardPool {
  supplier: { id: string; code: string; env: SupplierEnv }
  product: CardProduct
  cards: PoolCard[]
  truncated: boolean
  excluded_buyer_cards: number
  ours_30d: RiskView
}
export interface SchedulePreview {
  pick: number | null
  candidates: { card_id: number; skip_reason: string | null }[]
}
export interface CardRule {
  product: CardProduct
  count_failures: boolean | null
  light_max_uses: number | null
  pro20_max_uses: number | null
  select_mode: 'default' | 'lowest_usage' | 'newest' | null
  auto_switch_on_fail: boolean | null
  max_auto_switches: number | null
  strict_select: boolean | null
  is_default: boolean
  matches_cost_model: boolean | null
}
export interface CardRules {
  supplier: { id: string; code: string; env: SupplierEnv } | null
  rules: Partial<Record<CardProduct, CardRule>>
  cost_model_max_auto_switches: number
}
export type CardRulePatch = Partial<{
  count_failures: boolean
  light_max_uses: number
  pro20_max_uses: number
  select_mode: 'default' | 'lowest_usage' | 'newest'
  auto_switch_on_fail: boolean
  max_auto_switches: number
  strict_select: boolean
}>
export interface RiskReport {
  thresholds: { tier1: string; tier2: string; warn_at: string; min_sample: number; tier1_min_tx: number }
  level: RiskLevelName
  windows: Record<'7' | '30', { overall: RiskView; cards: (RiskView & { card_id: string })[] }>
  chargebacks: { available: boolean; reason: string }
}

export function createCardsApi(c: ApiClient) {
  const base = '/admin/procurement/cards'
  const sup = (supplier_id?: string) => (supplier_id ? { supplier_id } : {})
  return {
    suppliers: () => c.get<{ items: CardSupplier[] }>(`${base}/suppliers`),
    pool: (product: CardProduct, supplier_id?: string) => c.get<CardPool>(`${base}/pool`, { product, ...sup(supplier_id) }),
    schedule: (product: CardProduct, plan: string, supplier_id?: string) => c.get<SchedulePreview>(`${base}/schedule`, { product, plan, ...sup(supplier_id) }),
    risk: (supplier_id?: string) => c.get<RiskReport>(`${base}/risk`, sup(supplier_id)),
    rules: (supplier_id?: string) => c.get<CardRules>(`${base}/rules`, sup(supplier_id)),
    saveRule: (product: CardProduct, patch: CardRulePatch, supplier_id?: string) =>
      c.put<{ product: CardProduct; before: CardRulePatch | null; after: CardRulePatch | null }>(`${base}/rules/${seg(product)}`, { ...patch, ...sup(supplier_id) }),
    resetFailed: (card_ids: number[], supplier_id?: string) =>
      c.post<{ scope: 'failed'; card_ids: number[]; reset: number | null }>(`${base}/reset-failed`, { card_ids, confirm: true, ...sup(supplier_id) }),
  }
}

// ------------------------------------------------------------------ redemption admin (issue #31)
export type RedeemStep = 'preview' | 'preflight' | 'redeem' | 'result'
export type RedeemOutcome = 'ok' | 'completed' | 'failed' | 'error' | 'rate_limited' | 'invalid' | 'maintenance'
export interface RedeemAttempt {
  id: string
  at: string
  step: RedeemStep
  outcome: RedeemOutcome
  reason: string | null
  /** masked: head + "…" + tail, never the code */
  code: string | null
  plan: string | null
  order_no: string | null
  /** first characters of a keyed hash of the client's /24 or /48 (never an address) */
  ip_class: string | null
}
export interface RedeemAttemptFilter {
  q?: string
  outcome?: RedeemOutcome
  step?: RedeemStep
  date_from?: string
  date_to?: string
  order_ref?: string
}
export interface RedeemStatsDay {
  day: string
  attempts: number
  completed: number
  failed: number
  /** 4-decimal fraction string or null */
  success_rate: string | null
}
export interface RedeemStats {
  days: RedeemStatsDay[]
  totals: { attempts: number; completed: number; failed: number; success_rate: string | null }
  top_reasons: { reason: string; count: number }[]
}
export interface RedeemLookup {
  code: string | null
  sold_by_us: boolean
  ours: { order_no: string; status: string; paid_at: string | null; delivered_at: string | null; buyer: string | null } | null
  attempts: RedeemAttempt[]
  vendor: { available: true; found: boolean; status: string | null; plan: string | null; env: string | null } | { available: false; reason: string } | null
}
export type RedeemLimitStep = 'preview' | 'preflight' | 'redeem' | 'result'
export interface RedeemSettings {
  maintenance: boolean
  maintenance_en: string
  maintenance_zh: string
  announcement_en: string
  announcement_zh: string
  ip_limits: Partial<Record<RedeemLimitStep, number>>
  defaults: Record<RedeemLimitStep, number>
  bounds: Record<RedeemLimitStep, { min: number; max: number }>
  caps: { announcement: number; maintenance: number }
}
export type RedeemSettingsPatch = Partial<Pick<RedeemSettings, 'maintenance' | 'maintenance_en' | 'maintenance_zh' | 'announcement_en' | 'announcement_zh'>> & {
  ip_limits?: Partial<Record<RedeemLimitStep, number | null>>
}

export function createRedeemApi(c: ApiClient) {
  const base = '/admin/procurement/redeem'
  return {
    attempts: (f: RedeemAttemptFilter, page: number, page_size: number) =>
      c.get<Paginated<RedeemAttempt> & { retention_days: number }>(`${base}/attempts`, { ...f, page, page_size }),
    stats: (days = 30) => c.get<RedeemStats>(`${base}/stats`, { days }),
    lookup: (b: { code?: string; order_ref?: string }) => c.post<RedeemLookup>(`${base}/lookup`, b),
    setCdk: (action: 'disable' | 'enable', code: string, forceUnsold = false) =>
      c.post<{ action: string; before: { status: string | null }; after: { status: string | null } }>(`${base}/cdk/${seg(action)}`, { code, confirm: true, ...(forceUnsold ? { force_unsold: true } : {}) }),
    settings: () => c.get<RedeemSettings>(`${base}/settings`),
    saveSettings: (patch: RedeemSettingsPatch) => c.put<RedeemSettings>(`${base}/settings`, patch),
  }
}

// ------------------------------------------------------------------ card funding (ADR-048)
export interface FundingSettings {
  auto_recharge: boolean
  /** ADR-049: open a new card when no card has a use left (only while auto_recharge is on) */
  auto_open_cards: boolean
  /** ADR-049: move a used-up card's money back to the wallet (only while auto_recharge is on) */
  sweep_leftover: boolean
  /** owner 2026-11-25: off = upgrades sell at the full list price */
  upgrade_pay_difference?: boolean
  /** owner 2026-11-25: cancel auto-renewal (vendor API) before each upgrade order */
  cancel_renewal_before_upgrade?: boolean
  /** ADR-049: Telegram alert when the ZovoCard wallet is below this (USD; 0 = off) */
  wallet_alert_usd: string
  /** ADR-054: a zzshu order's dedicated card is loaded with the plan cost x (1 + this / 100); 0-100 */
  zzshu_card_margin_pct: string
  daily_cap_usd: string
  per_topup_max_usd: string
  bin_priority: string[]
}
export interface FundingTopup {
  id: string
  created_at: string | null
  state: 'planned' | 'sent' | 'done' | 'failed' | 'void'
  kind?: 'recharge' | 'open' | 'sweep'
  card_id: string
  amount: string
  currency: string
  order_id: string | null
  job_id: string | null
  error: string | null
}
export interface FundingView {
  settings: FundingSettings
  today: { spent_usd: string; left_usd: string }
  topups: FundingTopup[]
}
export type FundingPatch = Partial<FundingSettings> & { totp_code: string }
export interface CardCharge {
  time: string | null
  amount: string | null
  currency: string | null
  status: string | null
  type: string | null
  merchant: string | null
  ai: boolean
}
export interface ChargeCheck {
  payment: { order_status: string; paid: boolean; paid_at: string | null; amount_minor: number | null; currency: string }
  supplier: null | {
    state: 'not_sent' | 'completed' | 'pending' | 'failed' | 'not_found' | 'unknown'
    upstream_ref: string | null
    card_id: string | null
    charges: CardCharge[] | null
    topups: { state: string; card_id: string; amount: string; error: string | null }[]
    job_created_at: string | null
    first_sent_at: string | null
  }
}
export const BIN_RE = /^\d{6,9}$/
export const USD_RE = /^\d{1,5}(\.\d{1,2})?$/

export function createFundingApi(c: ApiClient) {
  const base = '/admin/procurement/funding'
  return {
    get: () => c.get<FundingView>(base),
    save: (patch: FundingPatch) => c.put<FundingView>(base, patch),
  }
}
