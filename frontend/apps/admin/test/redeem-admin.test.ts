import { describe, expect, it } from 'vitest'
import type { RedeemSettings } from '../src/api-procurement'
import { activeDays, barPercents, cdkActions, codeValid, outcomeTag, searchTerm, settingsErrors, settingsPatch, toForm } from '../src/lib/redeem'

const base: RedeemSettings = {
  maintenance: false,
  maintenance_en: '',
  maintenance_zh: '',
  announcement_en: 'Hello',
  announcement_zh: '',
  ip_limits: { preview: 4 },
  defaults: { preview: 10, preflight: 10, redeem: 6, result: 40 },
  bounds: { preview: { min: 1, max: 30 }, preflight: { min: 1, max: 20 }, redeem: { min: 1, max: 20 }, result: { min: 1, max: 60 } },
  caps: { announcement: 300, maintenance: 200 },
}

describe('redemption admin helpers', () => {
  it('codeValid mirrors the server check', () => {
    expect(codeValid('UUUPLUS-ABCDEF1234-0123456789')).toBe(true)
    expect(codeValid('  UUUPLUS-ABCDEF1234  ')).toBe(true)
    expect(codeValid('short')).toBe(false)
    expect(codeValid('has space inside!')).toBe(false)
    expect(codeValid('x'.repeat(129))).toBe(false)
  })
  it('searchTerm trims, collapses and caps', () => {
    expect(searchTerm('  UUUP…  6789 ')).toBe('UUUP… 6789')
    expect(searchTerm('x'.repeat(80))).toHaveLength(40)
  })
  it('barPercents scales to the tallest and keeps zeros at zero', () => {
    expect(barPercents([0, 5, 10])).toEqual([0, 50, 100])
    expect(barPercents([0, 0])).toEqual([0, 0])
    expect(barPercents([1, 1000])[0]).toBe(4) // a non-zero day stays visible
    expect(barPercents([])).toEqual([])
  })
  it('activeDays drops empty days and puts the newest first', () => {
    const d = (day: string, a: number) => ({ day, attempts: a, completed: 0, failed: 0, success_rate: null })
    expect(activeDays([d('2026-11-15', 1), d('2026-11-16', 0), d('2026-11-17', 2)]).map((x) => x.day)).toEqual(['2026-11-17', '2026-11-15'])
  })
  it('settingsErrors checks bounds, whole numbers and the text caps', () => {
    const f = toForm(base)
    expect(settingsErrors(f, base)).toEqual({})
    expect(settingsErrors({ ...f, ip_limits: { ...f.ip_limits, preview: '31' } }, base).limit_preview).toBe('range')
    expect(settingsErrors({ ...f, ip_limits: { ...f.ip_limits, redeem: '0' } }, base).limit_redeem).toBe('range')
    expect(settingsErrors({ ...f, ip_limits: { ...f.ip_limits, result: '1.5' } }, base).limit_result).toBe('whole')
    expect(settingsErrors({ ...f, announcement_en: 'x'.repeat(301) }, base).announcement_en).toBe('long')
    expect(settingsErrors({ ...f, maintenance_zh: '字'.repeat(201) }, base).maintenance_zh).toBe('long')
    expect(settingsErrors({ ...f, announcement_zh: '字'.repeat(300) }, base)).toEqual({})
  })
  it('settingsPatch sends only changes and null for a cleared limit', () => {
    const f = toForm(base)
    expect(settingsPatch(f, f)).toEqual({})
    expect(settingsPatch(f, { ...f, maintenance: true, announcement_zh: '公告' })).toEqual({ maintenance: true, announcement_zh: '公告' })
    expect(settingsPatch(f, { ...f, ip_limits: { ...f.ip_limits, preview: '' } })).toEqual({ ip_limits: { preview: null } })
    expect(settingsPatch(f, { ...f, ip_limits: { ...f.ip_limits, redeem: ' 7 ' } })).toEqual({ ip_limits: { redeem: 7 } })
  })
  it('outcomeTag and cdkActions', () => {
    expect(outcomeTag('completed')).toBe('success')
    expect(outcomeTag('failed')).toBe('danger')
    expect(outcomeTag('maintenance')).toBe('warning')
    expect(outcomeTag('invalid')).toBe('info')
    expect(cdkActions('unused')).toEqual({ disable: true, enable: false })
    expect(cdkActions('disabled')).toEqual({ disable: false, enable: true })
    expect(cdkActions('consumed')).toEqual({ disable: false, enable: false })
    expect(cdkActions(null)).toEqual({ disable: false, enable: false })
  })
})
