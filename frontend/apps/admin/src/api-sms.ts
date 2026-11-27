/** ADR-057: staff API of the SMS-code (HeroSMS) delivery mode. */
import { seg, type ApiClient } from '@24goods/shared'

export interface SmsRoute {
  country: number
  /** USD, decimal number */
  max_price: number
}
export interface SmsParams {
  service: string
  routes: SmsRoute[]
  max_attempts: number
  tries_per_route: number
  window_minutes: number
  number_wait_s: number
  min_swap_s: number
  start_hours: number
  daily_cap: number
  per_buyer_daily: number
  cool_no_numbers_min: number
  cool_no_code_streak: number
  cool_no_code_min: number
}
export interface SmsSupplier {
  id: string
  code: string
  env: string
  enabled: boolean
  health: string
  balance: string | number | null
}
export interface SmsStatus {
  ok: boolean
  reason: string | null
  stock: number | null
  checked_at: string | null
}
export interface SmsConfig {
  configured: boolean
  supplier_id: string | null
  enabled: boolean
  params: SmsParams
  status: SmsStatus | null
  delivery_mode: string
  suppliers: SmsSupplier[]
}
export interface SmsConfigInput {
  supplier_id: string
  enabled: boolean
  params: SmsParams
}
export interface SmsAttemptRow {
  seq: number
  country: number | string
  operator: string | null
  phone: string
  cost: string | number | null
  state: string
  bought_at: string | null
  code_at: string | null
  closed_at: string | null
}
export interface SmsOrderInfo {
  session: {
    state: string
    attempts_used: number
    fail_reason: string | null
    started_at: string | null
    window_ends_at: string | null
    start_deadline: string | null
    finished_at: string | null
  } | null
  attempts: SmsAttemptRow[]
}
export interface SmsRouteStat {
  country: number | string
  numbers: number
  codes: number
  no_code: number
  open: number
  code_rate: number | null
  median_seconds: number | null
  spent_usd: number | string
  cost_per_code_usd: number | string | null
  operators?: unknown
}
export interface SmsStats {
  days: number
  routes: SmsRouteStat[]
  sessions: {
    ready: number
    active: number
    delivered: number
    failed: number
    reasons: Record<string, number>
    success_rate: number | null
  }
}

/** Known HeroSMS country ids (hints only; any id is accepted). */
export const SMS_COUNTRY_HINTS: Record<number, string> = { 182: 'Japan', 52: 'Thailand', 187: 'USA', 16: 'UK', 6: 'Indonesia' }

export function createSmsApi(c: ApiClient) {
  return {
    config: (productId: string) => c.get<SmsConfig>(`/admin/sms/products/${seg(productId)}`),
    save: (productId: string, b: SmsConfigInput) => c.put<SmsConfig>(`/admin/sms/products/${seg(productId)}`, b),
    order: (orderId: string) => c.get<SmsOrderInfo>(`/admin/sms/orders/${seg(orderId)}`),
    stats: (days = 7) => c.get<SmsStats>('/admin/sms/stats', { days }),
  }
}
