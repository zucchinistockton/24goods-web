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

export function regionsOf(variants: readonly ProductVariant[], plan: string): ProductVariant[] {
  return variants.filter((v) => v.options?.plan === plan)
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
