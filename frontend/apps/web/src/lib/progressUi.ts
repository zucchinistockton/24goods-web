/** Issue #30 UI helpers: a glyph per step state (state is never colour alone) and a coalescing
 * debounced loader (one load in flight at most; events that arrive meanwhile cost one more). */

export type StepState = 'done' | 'current' | 'todo' | 'failed' | 'attention'

const GLYPHS: Record<StepState, string> = { done: '✓', current: '◉', failed: '!', attention: '!', todo: '○' }

/** Text glyph for a step state; unknown states read as todo. */
export function stepGlyph(state: string): string {
  return GLYPHS[state as StepState] ?? GLYPHS.todo
}

/** Known state word key suffix; unknown states read as todo. */
export function stepStateKey(state: string): StepState {
  return state in GLYPHS ? (state as StepState) : 'todo'
}

export interface Coalescer {
  trigger(): void
  cancel(): void
}

/** `trigger()` waits `delayMs` (restarting on each call), then runs `load`. While a load is in
 * flight no second one starts; if triggers arrived meanwhile, exactly one more runs after it. */
export function createCoalescer(load: () => Promise<unknown>, delayMs = 500): Coalescer {
  let timer: ReturnType<typeof setTimeout> | null = null
  let running = false
  let pending = false
  let dead = false
  async function run() {
    if (dead) return
    if (running) {
      pending = true
      return
    }
    running = true
    try {
      await load()
    } catch {
      /* the loader handles its own errors */
    } finally {
      running = false
    }
    if (pending && !dead) {
      pending = false
      void run()
    }
  }
  return {
    trigger() {
      if (dead) return
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => {
        timer = null
        void run()
      }, delayMs)
    },
    cancel() {
      dead = true
      if (timer) clearTimeout(timer)
      timer = null
    },
  }
}
