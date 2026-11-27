/** ADR-057: pure helpers of the SMS-code panel (state, countdowns, the reveal text). */
import type { SmsView } from '@24goods/shared'

export type SmsPhase = 'wait' | 'ready' | 'active' | 'delivered' | 'failed'

/** Which screen the buyer sees: `pending`/`none` (no session yet) is a quiet wait. */
export function smsPhase(v: SmsView | null): SmsPhase {
  switch (v?.state) {
    case 'ready':
    case 'active':
    case 'delivered':
    case 'failed':
      return v.state
    default:
      return 'wait'
  }
}

/** Poll only while something can still change and the tab is visible. */
export function shouldPoll(v: SmsView | null, visible: boolean): boolean {
  if (!visible) return false
  const s = v?.state
  return s === undefined || s === 'pending' || s === 'none' || s === 'ready' || s === 'active'
}

/** The number as shown and copied: `+` and the digits only. */
export function formatPhone(digits: string): string {
  const d = digits.replace(/\D/g, '')
  return d ? `+${d}` : ''
}

/** Seconds left until `at` (ISO), never negative. */
export function secondsUntil(at: string | null | undefined, nowMs: number): number {
  if (!at) return 0
  const t = Date.parse(at)
  return Number.isFinite(t) ? Math.max(0, Math.ceil((t - nowMs) / 1000)) : 0
}

/** `can_swap_in_s` was true at `fetchedAtMs`; what is left at `nowMs`. */
export function swapSecondsLeft(v: SmsView | null, fetchedAtMs: number, nowMs: number): number {
  const base = v?.can_swap_in_s ?? 0
  return Math.max(0, base - Math.floor((nowMs - fetchedAtMs) / 1000))
}

export function attemptsLeft(v: SmsView | null): number {
  return Math.max(0, (v?.max_attempts ?? 0) - (v?.attempts_used ?? 0))
}

/** m:ss */
export function formatClock(s: number): string {
  const m = Math.floor(s / 60)
  return `${m}:${String(s % 60).padStart(2, '0')}`
}

/** The reveal text is `<code>----+<phone>`: the code first, the number after the separator. */
export function splitSmsReveal(text: string | undefined | null): { code: string; phone: string } {
  const [code = '', phone = ''] = String(text ?? '').split('----')
  return { code: code.trim(), phone: phone.trim() }
}
