/** issue #31: pure helpers of the redemption admin (no I/O, no floats for rates). */
import type { RedeemLimitStep, RedeemSettings, RedeemSettingsPatch, RedeemStatsDay } from '../api-procurement'

/** Mirror of the server's code check: the console never sends text that is not a code. */
export const CODE_RE = /^[A-Za-z0-9._:-]{8,128}$/
export const codeValid = (s: string) => CODE_RE.test(s.trim())

/** "head…tail" or a single token, as the log search takes it (≤ 40 characters). */
export function searchTerm(raw: string): string {
  return raw.replace(/\s+/g, ' ').trim().slice(0, 40)
}

/** Share of the tallest bar, 0-100, whole numbers; an empty series is all zeros. */
export function barPercents(values: number[]): number[] {
  const max = Math.max(0, ...values)
  return values.map((v) => (max > 0 && v > 0 ? Math.max(4, Math.round((v / max) * 100)) : 0))
}

/** The days that carry data, newest first, for the table beside the bars. */
export function activeDays(days: RedeemStatsDay[]): RedeemStatsDay[] {
  return days.filter((d) => d.attempts || d.completed || d.failed).slice().reverse()
}

export type TextKey = 'announcement_en' | 'announcement_zh' | 'maintenance_en' | 'maintenance_zh'
export interface SettingsForm {
  maintenance: boolean
  announcement_en: string
  announcement_zh: string
  maintenance_en: string
  maintenance_zh: string
  /** empty string = the deployment default */
  ip_limits: Record<RedeemLimitStep, string>
}
export const LIMIT_STEPS: RedeemLimitStep[] = ['preview', 'preflight', 'redeem', 'result']

export function toForm(s: RedeemSettings): SettingsForm {
  const lim = {} as Record<RedeemLimitStep, string>
  for (const k of LIMIT_STEPS) lim[k] = s.ip_limits[k] != null ? String(s.ip_limits[k]) : ''
  return {
    maintenance: s.maintenance,
    announcement_en: s.announcement_en,
    announcement_zh: s.announcement_zh,
    maintenance_en: s.maintenance_en,
    maintenance_zh: s.maintenance_zh,
    ip_limits: lim,
  }
}

/** Field -> error key; empty when the form can be saved. Mirrors the server's bounds and caps. */
export function settingsErrors(f: SettingsForm, s: RedeemSettings): Record<string, 'range' | 'whole' | 'long'> {
  const out: Record<string, 'range' | 'whole' | 'long'> = {}
  for (const k of LIMIT_STEPS) {
    const raw = f.ip_limits[k].trim()
    if (raw === '') continue
    if (!/^[0-9]{1,3}$/.test(raw)) out[`limit_${k}`] = 'whole'
    else if (Number(raw) < s.bounds[k].min || Number(raw) > s.bounds[k].max) out[`limit_${k}`] = 'range'
  }
  for (const k of ['announcement_en', 'announcement_zh'] as TextKey[]) if ([...f[k]].length > s.caps.announcement) out[k] = 'long'
  for (const k of ['maintenance_en', 'maintenance_zh'] as TextKey[]) if ([...f[k]].length > s.caps.maintenance) out[k] = 'long'
  return out
}

/** Only what changed; a cleared limit becomes ``null`` (back to the default). */
export function settingsPatch(before: SettingsForm, after: SettingsForm): RedeemSettingsPatch {
  const patch: RedeemSettingsPatch = {}
  if (before.maintenance !== after.maintenance) patch.maintenance = after.maintenance
  for (const k of ['announcement_en', 'announcement_zh', 'maintenance_en', 'maintenance_zh'] as TextKey[]) {
    if (before[k] !== after[k]) patch[k] = after[k]
  }
  const lim: Partial<Record<RedeemLimitStep, number | null>> = {}
  for (const k of LIMIT_STEPS) {
    if (before.ip_limits[k].trim() !== after.ip_limits[k].trim()) lim[k] = after.ip_limits[k].trim() === '' ? null : Number(after.ip_limits[k])
  }
  if (Object.keys(lim).length) patch.ip_limits = lim
  return patch
}

/** Outcome -> Element Plus tag type. */
export function outcomeTag(o: string): 'success' | 'warning' | 'danger' | 'info' {
  if (o === 'completed' || o === 'ok') return 'success'
  if (o === 'failed' || o === 'error') return 'danger'
  if (o === 'rate_limited' || o === 'maintenance') return 'warning'
  return 'info'
}

/** Vendor CDK status -> whether the console offers "disable" / "enable". */
export function cdkActions(status: string | null | undefined): { disable: boolean; enable: boolean } {
  return { disable: status === 'unused', enable: status === 'disabled' }
}
