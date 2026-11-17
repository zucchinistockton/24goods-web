import { describe, expect, it } from 'vitest'
import { clampPercent, normaliseProgress, progressTone, stepLabelKey, stepTime } from '../src/directProgress'

describe('directProgress helpers', () => {
  it('clamps percent into 0..100', () => {
    expect(clampPercent(150)).toBe(100)
    expect(clampPercent(-3)).toBe(0)
    expect(clampPercent(42.4)).toBe(42)
    expect(clampPercent('x')).toBe(0)
    expect(clampPercent(Number.NaN)).toBe(0)
  })

  it('normalises a server document and drops anything unexpected', () => {
    const p = normaliseProgress({
      step: 'funding',
      percent: 55,
      outcome: 'active',
      terminal: false,
      reason: null,
      completed_at: null,
      secret: 'must not survive',
      steps: [
        { key: 'paid', state: 'done', at: '2026-11-17T10:00:00Z', vendor_code: 'X' },
        { key: 'funding', state: 'current', at: null },
        { key: 7, state: 'done' },
        { key: 'sent', state: 'weird' },
        null,
      ],
    })
    expect(p).not.toBeNull()
    expect(JSON.stringify(p)).not.toContain('must not survive')
    expect(JSON.stringify(p)).not.toContain('vendor_code')
    expect(p!.steps.map((s) => [s.key, s.state])).toEqual([
      ['paid', 'done'],
      ['funding', 'current'],
      ['sent', 'todo'],
    ])
  })

  it('refuses a malformed document', () => {
    expect(normaliseProgress(null)).toBeNull()
    expect(normaliseProgress('x')).toBeNull()
    expect(normaliseProgress({ steps: 'no' })).toBeNull()
    expect(normaliseProgress({ steps: [], outcome: 'martian', percent: 900 })).toMatchObject({ outcome: 'active', percent: 100 })
  })

  it('maps tone and label keys', () => {
    expect(progressTone({ outcome: 'completed' })).toBe('ok')
    expect(progressTone({ outcome: 'failed' })).toBe('bad')
    expect(progressTone({ outcome: 'attention' })).toBe('warn')
    expect(progressTone({ outcome: 'active' })).toBe('active')
    expect(stepLabelKey('p.step', 'paid', ['paid'])).toBe('p.step.paid')
    expect(stepLabelKey('p.step', 'zzz', ['paid'])).toBe('p.step.unknown')
  })

  it('reads ISO and epoch step times', () => {
    expect(stepTime('2026-11-17T10:00:00Z')).toBe(Date.parse('2026-11-17T10:00:00Z'))
    expect(stepTime(1784397600)).toBe(1784397600000)
    expect(stepTime(1784397600000)).toBe(1784397600000)
    expect(stepTime('nope')).toBeNull()
    expect(stepTime(null)).toBeNull()
  })
})
