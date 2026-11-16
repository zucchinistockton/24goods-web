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
export interface ProcJobDetail extends ProcJob {
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
      fulfil: (id: string, code: string, reason: string, card_id?: string) =>
        job(id, 'fulfil', clean({ code, reason, card_id })),
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
