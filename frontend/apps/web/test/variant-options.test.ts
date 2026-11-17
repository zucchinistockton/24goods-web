import { describe, expect, it } from 'vitest'
import type { ProductVariant } from '@24goods/shared'
import { hasOptions, planLabel, plansOf, regionsOf, regionLabel, usesOptions } from '../src/lib/variantOptions'

const v = (id: string, plan: string, region: string, stock: number | null = 5): ProductVariant => ({
  id,
  name: `${plan} ${region}`,
  price: '10.00',
  stock,
  options: { plan, region },
})

describe('plan x region variant helpers (issue #27)', () => {
  const rows = [v('1', 'plus', 'PH'), v('2', 'plus', 'JP', 0), v('3', 'plus_half', 'PH', 0), v('4', 'plus_half', 'JP', 0)]

  it('two selectors only when every variant has options', () => {
    expect(usesOptions(rows)).toBe(true)
    expect(usesOptions([...rows, { id: '5', name: 'plain', price: '1.00', stock: 1 }])).toBe(false)
    expect(usesOptions([])).toBe(false)
    expect(usesOptions(undefined)).toBe(false)
    expect(hasOptions({ id: 'x', name: 'x', price: '1', stock: 1, options: { plan: 'plus', region: '' } })).toBe(false)
  })
  it('plans keep their order and are sold out only when every region is', () => {
    expect(plansOf(rows)).toEqual([
      { plan: 'plus', soldOut: false },
      { plan: 'plus_half', soldOut: true },
    ])
  })
  it('regions of a plan', () => {
    expect(regionsOf(rows, 'plus').map((r) => r.id)).toEqual(['1', '2'])
    expect(regionsOf(rows, 'nope')).toEqual([])
  })
  it('labels fall back to a readable plan name and the ISO code', () => {
    expect(planLabel('plus_half', () => null)).toBe('Plus Half')
    expect(planLabel('plus', (k) => (k === 'plan.plus' ? 'Plus' : null))).toBe('Plus')
    expect(regionLabel('PH', 'en')).toBe('Philippines')
    expect(regionLabel('??', 'en')).toBeTruthy()
  })
})
