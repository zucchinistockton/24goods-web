import { describe, expect, it } from 'vitest'
import type { ProductVariant } from '@24goods/shared'
import { defaultOptionVariant, regionsOf } from '../src/lib/variantOptions'
import { defaultVariantId } from '../src/lib/variants'
import { maskEmail, summariseSession } from '../src/lib/sessionSummary'

const v = (id: string, plan: string, region: string, price: string, stock: number | null = 5): ProductVariant => ({
  id,
  name: `${plan} ${region}`,
  price,
  stock,
  options: { plan, region },
})

describe('regions cheapest first (owner rule)', () => {
  const rows = [
    v('us', 'plus', 'US', '169.00'),
    v('jp', 'plus', 'JP', '159.00'),
    v('ph', 'plus', 'PH', '99.00'),
    v('eg', 'plus', 'EG', '89.00', 0),
    v('h-us', 'plus_half', 'US', '89.00'),
    v('h-ph', 'plus_half', 'PH', '49.00'),
  ]
  it('sorts a plan’s regions by price, ties keep the staff order', () => {
    expect(regionsOf(rows, 'plus').map((r) => r.id)).toEqual(['eg', 'ph', 'jp', 'us'])
    const tie = [v('a', 'go', 'PH', '39.00'), v('b', 'go', 'EG', '39.00')]
    expect(regionsOf(tie, 'go').map((r) => r.id)).toEqual(['a', 'b'])
  })
  it('preselects the first plan’s cheapest region in stock', () => {
    expect(defaultOptionVariant(rows)?.id).toBe('ph') // EG is sold out
    expect(defaultVariantId(rows)).toBe('ph')
  })
  it('plain variants keep the first-in-stock rule', () => {
    const plain: ProductVariant[] = [
      { id: 'x', name: 'x', price: '9.00', stock: 0 },
      { id: 'y', name: 'y', price: '19.00', stock: 3 },
    ]
    expect(defaultVariantId(plain)).toBe('y')
  })
})

const b64url = (o: object) => btoa(JSON.stringify(o)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
const jwt = (exp: number) => `${b64url({ alg: 'none' })}.${b64url({ exp })}.sig`
const NOW = Date.parse('2026-11-18T12:00:00Z')
const session = (over: Record<string, unknown> = {}) =>
  JSON.stringify({
    user: { id: 'user-1', email: 'buyer@example.com' },
    account: { id: 'acc-1', planType: 'free' },
    expires: '2027-02-01T00:00:00.000Z',
    accessToken: jwt(NOW / 1000 + 864000),
    sessionToken: 'st-' + 'x'.repeat(40),
    ...over,
  })

describe('session paste summary', () => {
  it('confirms a complete session with the masked account, plan and validity', () => {
    const s = summariseSession(session(), 'gpt', NOW)
    expect(s.problem).toBeNull()
    expect(s.account).toBe('b***@example.com')
    expect(s.plan).toBe('free')
    expect(s.validUntil).toBe(NOW + 864000 * 1000) // the earlier of token and session expiry
    expect(s.bytes).toBeGreaterThan(100)
  })
  it('never exposes the full e-mail', () => {
    expect(maskEmail('someone@example.com')).toBe('s***@example.com')
    expect(maskEmail('nope')).toBeNull()
  })
  it('flags a paste that was cut off', () => {
    const full = session()
    expect(summariseSession(full.slice(0, full.length - 30), 'gpt', NOW).problem).toBe('incomplete')
    expect(summariseSession('hello', 'gpt', NOW).problem).toBe('incomplete')
  })
  it('flags only the access token, a missing token and an expired session', () => {
    expect(summariseSession(jwt(NOW / 1000 + 60), 'gpt', NOW).problem).toBe('access_token_only')
    expect(summariseSession(session({ sessionToken: undefined }), 'gpt', NOW).problem).toBe('missing_token')
    expect(summariseSession(session({ accessToken: jwt(NOW / 1000 - 60) }), 'gpt', NOW).problem).toBe('expired')
    expect(summariseSession(session({ expires: '2026-11-01T00:00:00Z' }), 'gpt', NOW).problem).toBe('expired')
  })
  it('other products: size only, no format guess', () => {
    const s = summariseSession('sk-ant-' + 'a'.repeat(80), 'claude', NOW)
    expect(s).toMatchObject({ problem: null, account: null, plan: null })
  })
})
