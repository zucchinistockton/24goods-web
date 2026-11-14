import { describe, expect, it } from 'vitest'
import { supplierCodeOk } from '../src/api-suppliers'

describe('supplier code rule (mirrors the server)', () => {
  it('accepts the adapter name or adapter_suffix for credential adapters', () => {
    expect(supplierCodeOk('zovocard', 'zovocard')).toBe(true)
    expect(supplierCodeOk('zovocard_backup', 'zovocard')).toBe(true)
    expect(supplierCodeOk('acme', 'zovocard')).toBe(false)
    expect(supplierCodeOk('zovocardx', 'zovocard')).toBe(false)
  })
  it('never accepts a _sandbox suffix or a malformed code', () => {
    expect(supplierCodeOk('zovocard_sandbox', 'zovocard')).toBe(false)
    expect(supplierCodeOk('desk_sandbox', 'manual')).toBe(false)
    expect(supplierCodeOk('Desk', 'manual')).toBe(false)
    expect(supplierCodeOk('desk', 'manual')).toBe(true)
  })
})
