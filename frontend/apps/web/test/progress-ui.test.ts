import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createCoalescer, stepGlyph, stepStateKey } from '../src/lib/progressUi'

describe('stepGlyph', () => {
  it('has a distinct shape per state', () => {
    expect(stepGlyph('done')).toBe('✓')
    expect(stepGlyph('current')).toBe('◉')
    expect(stepGlyph('failed')).toBe('!')
    expect(stepGlyph('attention')).toBe('!')
    expect(stepGlyph('todo')).toBe('○')
    expect(stepGlyph('weird')).toBe('○')
    expect(stepStateKey('weird')).toBe('todo')
    expect(stepStateKey('done')).toBe('done')
  })
})

describe('createCoalescer', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('debounces a burst into one load', async () => {
    const load = vi.fn().mockResolvedValue(undefined)
    const c = createCoalescer(load, 500)
    c.trigger()
    c.trigger()
    await vi.advanceTimersByTimeAsync(300)
    c.trigger()
    await vi.advanceTimersByTimeAsync(499)
    expect(load).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(2)
    expect(load).toHaveBeenCalledTimes(1)
  })

  it('never overlaps loads and runs one more after events arrived meanwhile', async () => {
    let release: () => void = () => {}
    let active = 0
    let maxActive = 0
    const load = vi.fn(
      () =>
        new Promise<void>((res) => {
          active++
          maxActive = Math.max(maxActive, active)
          release = () => {
            active--
            res()
          }
        }),
    )
    const c = createCoalescer(load, 500)
    c.trigger()
    await vi.advanceTimersByTimeAsync(500)
    expect(load).toHaveBeenCalledTimes(1)
    c.trigger()
    await vi.advanceTimersByTimeAsync(500)
    c.trigger()
    await vi.advanceTimersByTimeAsync(500)
    expect(load).toHaveBeenCalledTimes(1)
    release()
    await vi.advanceTimersByTimeAsync(0)
    expect(load).toHaveBeenCalledTimes(2)
    release()
    await vi.advanceTimersByTimeAsync(1000)
    expect(load).toHaveBeenCalledTimes(2)
    expect(maxActive).toBe(1)
  })

  it('cancel stops pending work', async () => {
    const load = vi.fn().mockResolvedValue(undefined)
    const c = createCoalescer(load, 500)
    c.trigger()
    c.cancel()
    await vi.advanceTimersByTimeAsync(1000)
    expect(load).not.toHaveBeenCalled()
  })
})
