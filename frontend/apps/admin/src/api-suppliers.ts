/** review-v12 M1: supplier configuration and the read-only connection test (admin only). */
import { seg, type ApiClient } from '@24goods/shared'

export type SupplierAdapter = 'zovocard' | 'zzshu' | 'mock' | 'manual'
export type SupplierEnv = 'sandbox' | 'production'
export type SupplierHealth = 'unknown' | 'ok' | 'degraded' | 'down'

export interface AdminSupplier {
  id: string
  code: string
  adapter: SupplierAdapter
  label: string
  enabled: boolean
  env: SupplierEnv
  priority: number
  /** decimal string, supplier currency */
  min_spendable: string
  currency: string
  health: SupplierHealth
  health_detail: string | null
  last_probe_at: string | null
  last_balance: string | null
  last_balance_at: string | null
  /** zzshu only: remaining points and when procurement last read them (UTC ISO) */
  points?: number | null
  points_at?: string | null
  breaker_open?: boolean
  /** `hard` = the supplier refused us (manual reset); `balance` = low balance (closes by itself) */
  breaker_kind?: 'hard' | 'balance' | null
  breaker_reason?: string | null
  breaker_at?: string | null
  created_at: string
  updated_at: string
}

export interface SupplierList {
  items: AdminSupplier[]
  adapters: SupplierAdapter[]
  production_locked: boolean
}

export interface SupplierInput {
  code: string
  adapter: SupplierAdapter
  label: string
  env: SupplierEnv
  priority: number
  min_spendable: string
  enabled: boolean
}
/** `current_password`: required by the server for a production supplier (create, or switch to production). */
export type SupplierPatch = Partial<Pick<SupplierInput, 'label' | 'env' | 'priority' | 'min_spendable' | 'enabled'>> & { current_password?: string }

/** Redacted probe report from the procurement process: no secrets, no response bodies. */
export interface ProbeReport {
  supplier: string
  env: SupplierEnv
  ok: boolean
  auth_ok: boolean | null
  ip_allowed: boolean | null
  permissions: Record<string, boolean>
  spendable_balance: string | null
  currency: string | null
  purchasable_plans: number | null
  card_products: number | null
  errors: string[]
  checked_at: string | null
}

export const SUPPLIER_CODE_RE = /^[a-z][a-z0-9_]{1,31}$/
/** Mirrors the server rule: never `_sandbox`; a credential adapter's code is `<adapter>` or `<adapter>_<suffix>`. */
export function supplierCodeOk(code: string, adapter: SupplierAdapter): boolean {
  if (!SUPPLIER_CODE_RE.test(code) || code.endsWith('_sandbox')) return false
  if (adapter === 'zovocard') return code === adapter || code.startsWith(`${adapter}_`)
  return true
}
export const SUPPLIER_AMOUNT_RE = /^[0-9]{1,9}(\.[0-9]{1,2})?$/

export function createSuppliersApi(c: ApiClient) {
  return {
    list: () => c.get<SupplierList>('/admin/suppliers'),
    resetBreaker: (id: string) => c.post<AdminSupplier>(`/admin/suppliers/${seg(id)}/reset-breaker`),
    create: (b: SupplierInput & { current_password?: string }) => c.post<AdminSupplier>('/admin/suppliers', b),
    update: (id: string, b: SupplierPatch) => c.patch<AdminSupplier>(`/admin/suppliers/${seg(id)}`, b),
    probe: (id: string) => c.post<{ report: ProbeReport; supplier: AdminSupplier | null }>(`/admin/suppliers/${seg(id)}/probe`),
  }
}
