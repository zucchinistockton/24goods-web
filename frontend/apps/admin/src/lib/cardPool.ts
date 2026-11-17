/** issue #29 part 2: pure helpers of the card pool, rules editor and risk monitor. No floats for money. */

export type RiskLevel = 'ok' | 'warning' | 'critical'

/** "0.1667" -> "16.67 %" by moving the decimal point (no floating point); `null` -> "—". */
export function ratePct(rate: string | null | undefined): string {
  if (rate == null || !/^[0-9]{1,3}(\.[0-9]{1,6})?$/.test(rate)) return '—'
  const [int, frac = ''] = rate.split('.')
  const f = (frac + '0000').slice(0, 4) // 4 places of a fraction = 2 places of a percent
  const whole = String(Number(int + f.slice(0, 2)))
  const dec = f.slice(2, 4)
  return `${whole}.${dec} %`
}

/** Element Plus tag type of a risk level. */
export function levelTag(level: RiskLevel | string): 'success' | 'warning' | 'danger' {
  return level === 'critical' ? 'danger' : level === 'warning' ? 'warning' : 'success'
}

export interface Bucket {
  used: number | null
  remaining: number | null
  cap: number | null
  failed: number | null
  unlimited: boolean
}

/** "2 left of 5", "unlimited" or "—" for one tier of a card's usage. */
export function usageText(b: Bucket | undefined, t: (k: string, p?: Record<string, unknown>) => string): string {
  if (!b) return '—'
  if (b.unlimited) return t('cardPool.unlimited')
  if (b.remaining == null) return '—'
  return b.cap ? t('cardPool.usesLeftOf', { n: b.remaining, cap: b.cap }) : t('cardPool.usesLeft', { n: b.remaining })
}

export interface RuleForm {
  count_failures: boolean
  light_max_uses: number
  pro20_max_uses: number
  select_mode: 'default' | 'lowest_usage' | 'newest'
  auto_switch_on_fail: boolean
  max_auto_switches: number
  strict_select: boolean
}
export const RULE_RANGES = { light_max_uses: [0, 1000], pro20_max_uses: [0, 1000], max_auto_switches: [0, 10] } as const

/** Field -> problem key for an out-of-range or non-integer value (mirrors the server). */
export function ruleErrors(f: RuleForm): Record<string, string> {
  const out: Record<string, string> = {}
  for (const k of ['light_max_uses', 'pro20_max_uses', 'max_auto_switches'] as const) {
    const v = f[k]
    const [lo, hi] = RULE_RANGES[k]
    if (!Number.isInteger(v) || v < lo || v > hi) out[k] = 'range'
  }
  return out
}

/** Only the fields that differ from the loaded rule: the PUT is a patch. */
export function ruleDiff(before: RuleForm, after: RuleForm): Partial<RuleForm> {
  const out: Record<string, unknown> = {}
  for (const k of Object.keys(after) as (keyof RuleForm)[]) if (after[k] !== before[k]) out[k] = after[k]
  return out as Partial<RuleForm>
}

/** The vendor's switch count must match the cost model's, or the estimate is wrong. */
export function switchMismatch(rule: { auto_switch_on_fail: boolean | null; max_auto_switches: number | null }, costModel: number): boolean {
  const eff = rule.auto_switch_on_fail === false ? 0 : rule.max_auto_switches
  return eff != null && eff !== costModel
}

/** Show the last four only: never anything longer, whatever the server sent. */
export function cardLabel(id: number, last4: string | null): string {
  return /^[0-9]{4}$/.test(last4 ?? '') ? `#${id} ····${last4}` : `#${id}`
}
