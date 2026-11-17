import type { DirectProgress, DirectProgressStep, ProgressStepState } from './types'

/** Issue #30: helpers for the buyer-facing progress of a direct order / redemption. The server
 * sends only coarse keys; these functions make the page robust against anything unexpected. */

const STATES: ProgressStepState[] = ['done', 'current', 'todo', 'failed', 'attention']
const OUTCOMES = ['active', 'completed', 'failed', 'attention'] as const

export function clampPercent(v: unknown): number {
  const n = typeof v === 'number' && Number.isFinite(v) ? Math.round(v) : 0
  return Math.min(100, Math.max(0, n))
}

/** Defensive copy of a server progress document: bounded percent, known states and outcome only. */
export function normaliseProgress(raw: unknown): DirectProgress | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  if (!Array.isArray(r.steps)) return null
  const steps: DirectProgressStep[] = []
  for (const s of r.steps.slice(0, 16)) {
    if (!s || typeof s !== 'object') continue
    const o = s as Record<string, unknown>
    if (typeof o.key !== 'string') continue
    const state = STATES.includes(o.state as ProgressStepState) ? (o.state as ProgressStepState) : 'todo'
    const at = typeof o.at === 'string' || typeof o.at === 'number' ? o.at : null
    steps.push({ key: o.key, state, at })
  }
  const outcome = (OUTCOMES as readonly string[]).includes(r.outcome as string) ? (r.outcome as DirectProgress['outcome']) : 'active'
  return {
    step: typeof r.step === 'string' ? r.step : '',
    percent: clampPercent(r.percent),
    outcome,
    terminal: r.terminal === true,
    reason: typeof r.reason === 'string' ? r.reason : null,
    steps,
    completed_at: typeof r.completed_at === 'string' || typeof r.completed_at === 'number' ? r.completed_at : null,
  }
}

/** i18n key for a step label; unknown keys fall back to a generic label. */
export function stepLabelKey(prefix: string, key: string, known: readonly string[]): string {
  return known.includes(key) ? `${prefix}.${key}` : `${prefix}.unknown`
}

/** Tone of the bar: green when completed, red when failed, amber when it needs attention. */
export function progressTone(p: Pick<DirectProgress, 'outcome'>): 'ok' | 'bad' | 'warn' | 'active' {
  switch (p.outcome) {
    case 'completed':
      return 'ok'
    case 'failed':
      return 'bad'
    case 'attention':
      return 'warn'
    default:
      return 'active'
  }
}

/** Step time as epoch milliseconds (ISO text or epoch seconds); null when unusable. */
export function stepTime(at: string | number | null | undefined): number | null {
  if (typeof at === 'number') return Number.isFinite(at) && at > 0 ? (at < 1e12 ? at * 1000 : at) : null
  if (typeof at !== 'string') return null
  const t = Date.parse(at)
  return Number.isNaN(t) ? null : t
}
