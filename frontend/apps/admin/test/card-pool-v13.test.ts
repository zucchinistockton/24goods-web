import { describe, expect, it } from 'vitest'
import { cardLabel, codeKey, codeText, planValid, levelTag, ratePct, ruleDiff, ruleErrors, switchMismatch, usageText, type RuleForm } from '../src/lib/cardPool'

const t = (k: string, p?: Record<string, unknown>) => `${k}${p ? JSON.stringify(p) : ''}`
const base: RuleForm = { count_failures: true, light_max_uses: 5, pro20_max_uses: 3, select_mode: 'default', auto_switch_on_fail: true, max_auto_switches: 2, strict_select: false }

describe('card pool helpers', () => {
  it('ratePct moves the decimal point, never floats', () => {
    expect(ratePct('0.1667')).toBe('16.67 %')
    expect(ratePct('0.15')).toBe('15.00 %')
    expect(ratePct('0.3')).toBe('30.00 %')
    expect(ratePct('0.0000')).toBe('0.00 %')
    expect(ratePct('1')).toBe('100.00 %')
    expect(ratePct('0.0909')).toBe('9.09 %')
    expect(ratePct(null)).toBe('—')
    expect(ratePct('abc')).toBe('—')
  })
  it('levelTag', () => {
    expect(levelTag('critical')).toBe('danger')
    expect(levelTag('warning')).toBe('warning')
    expect(levelTag('ok')).toBe('success')
  })
  it('usageText', () => {
    expect(usageText({ used: 3, remaining: 2, cap: 5, failed: 0, unlimited: false }, t)).toContain('usesLeftOf')
    expect(usageText({ used: 3, remaining: 2, cap: null, failed: 0, unlimited: false }, t)).toContain('usesLeft{')
    expect(usageText({ used: 0, remaining: null, cap: 0, failed: 0, unlimited: true }, t)).toBe('cardPool.unlimited')
    expect(usageText({ used: null, remaining: null, cap: null, failed: null, unlimited: false }, t)).toBe('—')
    expect(usageText(undefined, t)).toBe('—')
  })
  it('ruleErrors mirrors the server ranges', () => {
    expect(ruleErrors(base)).toEqual({})
    expect(ruleErrors({ ...base, light_max_uses: -1 }).light_max_uses).toBe('range')
    expect(ruleErrors({ ...base, pro20_max_uses: 1001 }).pro20_max_uses).toBe('range')
    expect(ruleErrors({ ...base, max_auto_switches: 11 }).max_auto_switches).toBe('range')
    expect(ruleErrors({ ...base, max_auto_switches: 1.5 }).max_auto_switches).toBe('range')
    expect(ruleErrors({ ...base, light_max_uses: 0, max_auto_switches: 0 })).toEqual({})
  })
  it('ruleDiff sends only what changed', () => {
    expect(ruleDiff(base, base)).toEqual({})
    expect(ruleDiff(base, { ...base, light_max_uses: 8, select_mode: 'newest' })).toEqual({ light_max_uses: 8, select_mode: 'newest' })
  })
  it('switchMismatch compares the effective switch count with the cost model', () => {
    expect(switchMismatch({ auto_switch_on_fail: true, max_auto_switches: 2 }, 2)).toBe(false)
    expect(switchMismatch({ auto_switch_on_fail: true, max_auto_switches: 4 }, 2)).toBe(true)
    expect(switchMismatch({ auto_switch_on_fail: false, max_auto_switches: 2 }, 2)).toBe(true) // none vs 2
    expect(switchMismatch({ auto_switch_on_fail: true, max_auto_switches: null }, 2)).toBe(false)
  })
  it('cardLabel shows at most the last four', () => {
    expect(cardLabel(4242, '4242')).toBe('#4242 ····4242')
    expect(cardLabel(7, null)).toBe('#7')
    expect(cardLabel(7, '5378000000004242')).toBe('#7') // anything longer is dropped
  })
})

describe('card pool code labels and plan check', () => {
  const msgs: Record<string, string> = { 'cardPool.skip.cooling_down': 'Cooling down', 'cardPool.cardStatus.active': 'Active' }
  const tt = (k: string, p?: Record<string, unknown>) => (k === 'cardPool.other' ? `Other (${p?.code})` : (msgs[k] ?? k))
  const te = (k: string) => k in msgs
  it('codeKey makes a safe key segment', () => {
    expect(codeKey('Cooling-Down')).toBe('cooling_down')
    expect(codeKey('a.b:c')).toBe('a_b_c')
    expect(codeKey('..')).toBe('')
  })
  it('codeText translates known codes and labels unknown ones', () => {
    expect(codeText('cardPool.skip', 'cooling_down', tt, te)).toBe('Cooling down')
    expect(codeText('cardPool.skip', 'Cooling-Down', tt, te)).toBe('Cooling down')
    expect(codeText('cardPool.cardStatus', 'ACTIVE', tt, te)).toBe('Active')
    expect(codeText('cardPool.skip', 'weird_code', tt, te)).toBe('Other (weird_code)')
    expect(codeText('cardPool.skip', '..', tt, te)).toBe('Other (..)')
  })
  it('planValid mirrors the server pattern', () => {
    expect(planValid(' pro_20x ')).toBe(true)
    expect(planValid('')).toBe(false)
    expect(planValid('a b')).toBe(false)
    expect(planValid('x'.repeat(49))).toBe(false)
  })
})
