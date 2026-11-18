import type { ProductVariant } from '@24goods/shared'

/** issue #27: helpers for the plan x region selectors. A variant "has options" when it names both. */
export function hasOptions(v: ProductVariant): boolean {
  return !!v.options?.plan && !!v.options?.region
}

/** The two selectors apply only when EVERY variant has options (else the plain variant chips). */
export function usesOptions(variants: readonly ProductVariant[] | undefined): boolean {
  return !!variants?.length && variants.every(hasOptions)
}

export function soldOutVariant(v: ProductVariant): boolean {
  return v.stock !== null && v.stock !== undefined && v.stock <= 0
}

/** Distinct plans in the order they first appear; a plan is sold out when every region is. */
export function plansOf(variants: readonly ProductVariant[]): { plan: string; soldOut: boolean }[] {
  const out = new Map<string, boolean>()
  for (const v of variants) {
    const plan = v.options?.plan
    if (!plan) continue
    out.set(plan, (out.get(plan) ?? true) && soldOutVariant(v))
  }
  return [...out].map(([plan, soldOut]) => ({ plan, soldOut }))
}

/** The regions a plan is sold in, cheapest first (ties keep the staff's order). Owner rule: the
 * low-price regions lead, the full-price ones (usually the US) come last. */
export function regionsOf(variants: readonly ProductVariant[], plan: string): ProductVariant[] {
  return variants
    .map((v, i) => ({ v, i, price: Number(v.price) }))
    .filter((r) => r.v.options?.plan === plan)
    .sort((a, b) => (Number.isFinite(a.price) && Number.isFinite(b.price) && a.price !== b.price ? a.price - b.price : a.i - b.i))
    .map((r) => r.v)
}

/** Plan x region products preselect the first plan's cheapest region that is in stock. */
export function defaultOptionVariant(variants: readonly ProductVariant[]): ProductVariant | null {
  const plans = plansOf(variants)
  const plan = plans.find((p) => !p.soldOut) ?? plans[0]
  if (!plan) return null
  const rows = regionsOf(variants, plan.plan)
  return rows.find((v) => !soldOutVariant(v)) ?? rows[0] ?? null
}

/** `plus_half` -> "Plus Half" unless the locale has `plan.<key>` (looked up by the caller). */
export function planLabel(plan: string, lookup: (key: string) => string | null): string {
  return lookup(`plan.${plan}`) ?? plan.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

/** "PH" -> "Philippines" / "菲律宾" through the browser's own display names (no network). */
export function regionLabel(code: string, locale: string): string {
  try {
    return new Intl.DisplayNames([locale], { type: 'region' }).of(code) ?? code
  } catch {
    return code
  }
}
