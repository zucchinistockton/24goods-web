import { describe, expect, it } from 'vitest'
import type { SmsView } from '@24goods/shared'
import { attemptsLeft, formatClock, formatPhone, secondsUntil, shouldPoll, smsPhase, splitSmsReveal, swapSecondsLeft } from '../src/lib/smsPanel'

const v = (o: Partial<SmsView>): SmsView => ({ state: 'ready', order_status: 'paid', ...o })

describe('sms panel state', () => {
  it('maps every API state to one screen', () => {
    expect(smsPhase(null)).toBe('wait')
    expect(smsPhase(v({ state: 'pending' }))).toBe('wait')
    expect(smsPhase(v({ state: 'none' }))).toBe('wait')
    expect(smsPhase(v({ state: 'ready' }))).toBe('ready')
    expect(smsPhase(v({ state: 'active' }))).toBe('active')
    expect(smsPhase(v({ state: 'delivered' }))).toBe('delivered')
    expect(smsPhase(v({ state: 'failed' }))).toBe('failed')
  })
  it('polls only while something can change and the tab is visible', () => {
    for (const s of ['pending', 'none', 'ready', 'active'] as const) {
      expect(shouldPoll(v({ state: s }), true)).toBe(true)
      expect(shouldPoll(v({ state: s }), false)).toBe(false)
    }
    expect(shouldPoll(null, true)).toBe(true)
    expect(shouldPoll(v({ state: 'delivered' }), true)).toBe(false)
    expect(shouldPoll(v({ state: 'failed' }), true)).toBe(false)
  })
  it('shows the number as +digits and copies exactly that', () => {
    expect(formatPhone('819012345678')).toBe('+819012345678')
    expect(formatPhone('+81 90')).toBe('+8190')
    expect(formatPhone('')).toBe('')
  })
  it('counts the swap delay down and gates attempts', () => {
    const view = v({ state: 'active', can_swap_in_s: 30, attempts_used: 1, max_attempts: 3 })
    expect(swapSecondsLeft(view, 1000, 1000)).toBe(30)
    expect(swapSecondsLeft(view, 1000, 11_500)).toBe(20)
    expect(swapSecondsLeft(view, 1000, 99_000)).toBe(0)
    expect(attemptsLeft(view)).toBe(2)
    expect(attemptsLeft(v({ state: 'pending' }))).toBe(0)
  })
  it('clock and deadlines', () => {
    expect(formatClock(125)).toBe('2:05')
    expect(secondsUntil('2026-01-01T00:01:00Z', Date.parse('2026-01-01T00:00:00Z'))).toBe(60)
    expect(secondsUntil('2026-01-01T00:00:00Z', Date.parse('2026-01-01T00:05:00Z'))).toBe(0)
    expect(secondsUntil(null, 0)).toBe(0)
  })
  it('splits the reveal text into code and number', () => {
    expect(splitSmsReveal('123456----+819012345678')).toEqual({ code: '123456', phone: '+819012345678' })
    expect(splitSmsReveal(undefined)).toEqual({ code: '', phone: '' })
  })
})
