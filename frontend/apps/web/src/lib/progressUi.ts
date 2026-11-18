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

export { createCoalescer, type Coalescer } from '@24goods/shared'
