import { afterEach, describe, expect, it, vi } from 'vitest'
import { followSystemTheme } from '../src/theme'

/** Minimal DOM stand-ins: the helper only touches classList, one meta tag and matchMedia. */
function fakeDom(dark: boolean) {
  const classes = new Set<string>()
  const meta = { content: '', setAttribute: (_: string, v: string) => void (meta.content = v) }
  let listener: (() => void) | null = null
  const mq = {
    matches: dark,
    addEventListener: (_: string, fn: () => void) => void (listener = fn),
    removeEventListener: () => void (listener = null),
  }
  vi.stubGlobal('window', { matchMedia: () => mq })
  vi.stubGlobal('document', {
    documentElement: { classList: { toggle: (c: string, on: boolean) => (on ? classes.add(c) : classes.delete(c)) } },
    querySelector: () => meta,
  })
  return { classes, meta, mq, fire: () => listener?.(), hasListener: () => listener !== null }
}

afterEach(() => vi.unstubAllGlobals())

describe('followSystemTheme', () => {
  it('applies the system scheme at once and follows changes', () => {
    const dom = fakeDom(true)
    const stop = followSystemTheme(['dark', 'van-theme-dark'], { light: '#ffd60a', dark: '#161614' })
    expect([...dom.classes].sort()).toEqual(['dark', 'van-theme-dark'])
    expect(dom.meta.content).toBe('#161614')
    dom.mq.matches = false
    dom.fire()
    expect(dom.classes.size).toBe(0)
    expect(dom.meta.content).toBe('#ffd60a')
    stop()
    expect(dom.hasListener()).toBe(false)
  })

  it('is a no-op without matchMedia (SSR, old webviews)', () => {
    vi.stubGlobal('window', {})
    expect(() => followSystemTheme(['dark'])()).not.toThrow()
  })
})
