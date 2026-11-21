import { seg, type ApiClient } from '@24goods/shared'

/** Crypto networks and BEpusdt receiving wallets (owner 2026-11-21). */
export interface CryptoChannel {
  id: string
  trade_type: string
  coin: string
  network: string
  family: 'evm' | 'tron' | 'solana' | 'ton' | 'aptos'
  label: { en: string; 'zh-CN': string }
  enabled: boolean
  surcharge_permille: number
  has_wallet: boolean
}
export interface CryptoWallet {
  id: number
  name: string
  trade_type: string
  address: string
  enabled: boolean
  remark: string
  updated_at: string
}
export interface CryptoView {
  channels: CryptoChannel[]
  wallets: CryptoWallet[]
  error: string | null
}
export function createCryptoApi(c: ApiClient) {
  const base = '/admin/payments/crypto'
  return {
    get: () => c.get<CryptoView>(base),
    add: (b: { trade_type: string; address: string; name?: string; remark?: string }) => c.post<CryptoView>(`${base}/wallets`, b),
    edit: (id: number, b: { enabled?: boolean; address?: string; name?: string; remark?: string }) => c.patch<CryptoView>(`${base}/wallets/${seg(String(id))}`, b),
    remove: (id: number) => c.delete<CryptoView>(`${base}/wallets/${seg(String(id))}`),
  }
}
/** Address shape per family (mirrors backend `crypto_wallets.ADDRESS_RE`) */
export const ADDRESS_RE: Record<CryptoChannel['family'], RegExp> = {
  evm: /^0x[0-9a-fA-F]{40}$/,
  tron: /^T[1-9A-HJ-NP-Za-km-z]{33}$/,
  solana: /^[1-9A-HJ-NP-Za-km-z]{32,44}$/,
  ton: /^((EQ|UQ|kQ|0Q)[A-Za-z0-9_-]{46}|-?[0-9]:[0-9a-fA-F]{64})$/,
  aptos: /^0x[0-9a-fA-F]{1,64}$/,
}
