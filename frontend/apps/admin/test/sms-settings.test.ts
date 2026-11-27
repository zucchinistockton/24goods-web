import { describe, expect, it } from 'vitest'
import type { SmsParams } from '../src/api-sms'
import { addRoute, countryHint, firstBadField, moveRoute, percent, removeRoute } from '../src/lib/smsForm'

const base = (): SmsParams => ({
  service: 'xx',
  routes: [{ country: 182, max_price: 0.5 }],
  max_attempts: 3,
  tries_per_route: 2,
  window_minutes: 30,
  number_wait_s: 180,
  min_swap_s: 60,
  start_hours: 24,
  daily_cap: 100,
  per_buyer_daily: 3,
  cool_no_numbers_min: 10,
  cool_no_code_streak: 3,
  cool_no_code_min: 15,
})

describe('sms settings form', () => {
  const r = (c: number) => ({ country: c, max_price: 1 })
  it('reorders, removes and adds routes without mutating', () => {
    const a = [r(1), r(2), r(3)]
    expect(moveRoute(a, 0, 1).map((x) => x.country)).toEqual([2, 1, 3])
    expect(moveRoute(a, 0, -1)).toBe(a)
    expect(moveRoute(a, 2, 1)).toBe(a)
    expect(removeRoute(a, 1).map((x) => x.country)).toEqual([1, 3])
    expect(addRoute(a)).toHaveLength(4)
    expect(a).toHaveLength(3)
  })
  it('hints known country ids only', () => {
    expect(countryHint(182)).toBe('Japan')
    expect(countryHint(52)).toBe('Thailand')
    expect(countryHint(999)).toBe('')
  })
  it('flags the first bad field', () => {
    expect(firstBadField(base())).toBeNull()
    expect(firstBadField({ ...base(), routes: [] })).toBe('routes')
    expect(firstBadField({ ...base(), routes: [{ country: 0, max_price: 1 }] })).toBe('routes.country')
    expect(firstBadField({ ...base(), routes: [{ country: 5, max_price: 0 }] })).toBe('routes.max_price')
    expect(firstBadField({ ...base(), daily_cap: -1 })).toBe('daily_cap')
    expect(firstBadField({ ...base(), max_attempts: 1.5 })).toBe('max_attempts')
  })
  it('formats rates', () => {
    expect(percent(0.756)).toBe('76%')
    expect(percent(null)).toBe('–')
  })
})
