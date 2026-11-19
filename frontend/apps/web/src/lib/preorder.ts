/** U10 pre-orders: the window phrase used by the badge, the confirmation tick and the order page. */
type T = (key: string, values?: Record<string, unknown>) => string

export interface WindowLike {
  min_days: number
  max_days: number
}

/** "ships 1–3 days after payment" / "ships 3 days after payment" / "ships within 3 days after payment" */
export function preorderRange(t: T, w: WindowLike): string {
  if (w.min_days === w.max_days) return t('preorder.days', { n: w.max_days })
  if (w.min_days === 0) return t('preorder.within', { n: w.max_days })
  return t('preorder.range', { min: w.min_days, max: w.max_days })
}
