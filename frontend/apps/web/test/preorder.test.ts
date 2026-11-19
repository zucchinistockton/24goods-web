import { describe, expect, it } from 'vitest'
import { preorderRange } from '../src/lib/preorder'

// the key + values are what matters; the i18n bundles are checked by i18n.test.ts
const t = (key: string, values?: Record<string, unknown>) => `${key}:${JSON.stringify(values ?? {})}`

describe('pre-order window phrase (U10)', () => {
  it('uses a range, a single day count or "within"', () => {
    expect(preorderRange(t, { min_days: 1, max_days: 3 })).toBe('preorder.range:{"min":1,"max":3}')
    expect(preorderRange(t, { min_days: 3, max_days: 3 })).toBe('preorder.days:{"n":3}')
    expect(preorderRange(t, { min_days: 0, max_days: 2 })).toBe('preorder.within:{"n":2}')
  })
})
