/**
 * Crypto payment channels (owner 2026-11-21: every stablecoin network BEpusdt supports). Mirrors
 * `backend/app/modules/payments/channels.py` (`NETWORKS`, `_TRADE_TYPES`): channel id =
 * `<coin>_<network>`, e.g. `usdt_polygon`. The label helper lets any screen name a channel it has no
 * translation key for.
 */
export const CRYPTO_NETWORKS: Record<string, { en: string; zh: string }> = {
  xlayer: { en: 'X Layer', zh: 'X Layer' },
  polygon: { en: 'Polygon PoS', zh: 'Polygon PoS' },
  arbitrum: { en: 'Arbitrum One', zh: 'Arbitrum One' },
  base: { en: 'Base', zh: 'Base' },
  bep20: { en: 'BNB Chain BEP20', zh: '币安链 BEP20' },
  solana: { en: 'Solana', zh: 'Solana' },
  ton: { en: 'TON', zh: 'TON' },
  aptos: { en: 'Aptos', zh: 'Aptos' },
  plasma: { en: 'Plasma', zh: 'Plasma' },
  erc20: { en: 'Ethereum ERC20', zh: '以太坊 ERC20' },
  trc20: { en: 'TRON TRC20', zh: '波场 TRC20' },
}
const USDT_NETS = ['xlayer', 'polygon', 'arbitrum', 'bep20', 'solana', 'ton', 'aptos', 'plasma', 'erc20', 'trc20']
const USDC_NETS = ['xlayer', 'polygon', 'arbitrum', 'base', 'bep20', 'solana', 'aptos', 'erc20', 'trc20']
export const CRYPTO_CHANNEL_IDS: readonly string[] = [
  ...USDT_NETS.map((n) => `usdt_${n}`),
  ...USDC_NETS.map((n) => `usdc_${n}`),
]

export function isCryptoChannel(id: string | null | undefined): boolean {
  return !!id && /^(usdt|usdc)_[a-z0-9]+$/.test(id)
}

/** `usdt_polygon` -> "USDT (Polygon PoS)" / "USDT（Polygon PoS）"; `null` for a non-crypto id. */
export function cryptoChannelLabel(id: string | null | undefined, locale = 'en'): string | null {
  if (!isCryptoChannel(id)) return null
  const [coin, net] = (id as string).split('_')
  const n = CRYPTO_NETWORKS[net]
  const name = n ? (locale.startsWith('zh') ? n.zh : n.en) : net
  return locale.startsWith('zh') ? `${coin.toUpperCase()}（${name}）` : `${coin.toUpperCase()} (${name})`
}
