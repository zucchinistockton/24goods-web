import { describe, expect, it } from 'vitest'
import { DIRECT_VERIFY_URL, DIRECT_VERIFY_WINDOW_S, isExpediteMessage, isUrgentPaid, verifySecondsLeft } from '../src/lib/notifyPolicy'

describe('urgent desk alerts (v9 §6)', () => {
  it('only a paid manual goods order is urgent', () => {
    expect(isUrgentPaid('paid', { delivery_mode: 'manual', kind: 'goods' })).toBe(true)
    expect(isUrgentPaid('paid', { delivery_mode: 'auto_card', kind: 'goods' })).toBe(false)
    expect(isUrgentPaid('created', { delivery_mode: 'manual', kind: 'goods' })).toBe(false)
    expect(isUrgentPaid(null, { delivery_mode: 'manual' })).toBe(false)
  })
  it('recognises the buyer reminder system message', () => {
    expect(isExpediteMessage({ type: 'system', payload: { event: 'delivery_expedite' } })).toBe(true)
    expect(isExpediteMessage({ type: 'system', payload: { event: 'paid' } })).toBe(false)
    expect(isExpediteMessage({ type: 'text', payload: { event: 'delivery_expedite' } })).toBe(false)
  })
})

describe('direct-order verification alert', () => {
  it('counts the seconds left, never negative, with the vendor window as fallback', () => {
    const now = Date.parse('2026-11-26T13:12:00Z')
    expect(verifySecondsLeft('2026-11-26T13:14:30Z', now)).toBe(150)
    expect(verifySecondsLeft('2026-11-26T13:00:00Z', now)).toBe(0)
    expect(verifySecondsLeft(null, now)).toBe(DIRECT_VERIFY_WINDOW_S)
    expect(verifySecondsLeft('garbage', now)).toBe(DIRECT_VERIFY_WINDOW_S)
  })
  it('the click target is the one fixed https URL', () => {
    expect(DIRECT_VERIFY_URL).toBe('https://zovocard.com/direct')
  })
})
