/** ADR-057: pure helpers of the admin SMS settings form. */
import { SMS_COUNTRY_HINTS, type SmsParams, type SmsRoute } from '../api-sms'

export function moveRoute(routes: SmsRoute[], i: number, dir: -1 | 1): SmsRoute[] {
  const j = i + dir
  if (i < 0 || i >= routes.length || j < 0 || j >= routes.length) return routes
  const out = routes.slice()
  ;[out[i], out[j]] = [out[j]!, out[i]!]
  return out
}

export function removeRoute(routes: SmsRoute[], i: number): SmsRoute[] {
  return routes.filter((_, k) => k !== i)
}

export function addRoute(routes: SmsRoute[]): SmsRoute[] {
  return [...routes, { country: 0, max_price: 0.5 }]
}

export function countryHint(id: number): string {
  return SMS_COUNTRY_HINTS[id] ?? ''
}

/** Client-side check before PUT; the server stays the authority. Returns the first bad field or null. */
export function firstBadField(p: SmsParams): string | null {
  if (!p.routes.length) return 'routes'
  for (const r of p.routes) {
    if (!Number.isInteger(r.country) || r.country <= 0) return 'routes.country'
    if (!(r.max_price > 0)) return 'routes.max_price'
  }
  const ints: (keyof SmsParams)[] = ['max_attempts', 'tries_per_route', 'window_minutes', 'number_wait_s', 'min_swap_s', 'start_hours', 'daily_cap', 'per_buyer_daily', 'cool_no_numbers_min', 'cool_no_code_streak', 'cool_no_code_min']
  for (const k of ints) {
    const v = p[k] as number
    if (!Number.isInteger(v) || v < 0) return k
  }
  return null
}

export function percent(rate: number | null | undefined): string {
  return rate == null ? '–' : `${Math.round(rate * 100)}%`
}
