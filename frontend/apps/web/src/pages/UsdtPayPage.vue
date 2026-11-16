<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { copyText, formatMoney, fromMinor, goBack, isApiError, type UsdtPayView } from '@24goods/shared'
import { api } from '../api'
import { payCheckout, payOrder } from '../lib/orderActions'
import { showSuccessToast, toastError } from '../ui'

/**
 * Our own USDT pay page (review-v5.2 §7): network, receiving address, exact USDT amount,
 * countdown and a QR of the address. It only DISPLAYS and polls the status; the order is
 * fulfilled solely by the server-side notify pipeline.
 */
const route = useRoute()
const router = useRouter()
const { t, locale } = useI18n()
const no = computed(() => String(route.params.no ?? ''))
const isCheckout = computed(() => no.value.toUpperCase().startsWith('C'))
const view = ref<UsdtPayView | null>(null)
const failed = ref(false)
const now = ref(Date.now())
const fetchedAt = ref(Date.now())
const PAID = new Set(['paid', 'delivered', 'completed'])

async function load(silent = false) {
  try {
    view.value = await api.usdtPay(no.value)
    fetchedAt.value = Date.now()
    failed.value = false
  } catch (e) {
    if (!silent) {
      failed.value = true
      if (!(isApiError(e) && e.status === 404)) toastError(e)
    }
  }
}
void load()

const left = computed(() => {
  const v = view.value
  if (!v) return 0
  return Math.max(0, v.seconds_left - Math.floor((now.value - fetchedAt.value) / 1000))
})
const clock = computed(() => `${String(Math.floor(left.value / 60)).padStart(2, '0')}:${String(left.value % 60).padStart(2, '0')}`)
const paid = computed(() => !!view.value && PAID.has(view.value.status))
const review = computed(() => view.value?.status === 'manual_review')
const expired = computed(() => !!view.value && !paid.value && !review.value && (view.value.expired || left.value === 0))
/** Head and tail emphasised (what a wallet shows as `TXyz12…9aBcDe`), middle in groups of 4. */
const EDGE = 6
const addr = computed(() => {
  const a = view.value?.address ?? ''
  if (a.length <= 16) return { head: a, mid: [] as string[], tail: '', short: a }
  const head = a.slice(0, EDGE)
  const tail = a.slice(-EDGE)
  return { head, mid: a.slice(EDGE, -EDGE).match(/.{1,4}/g) ?? [], tail, short: `${head}…${tail}` }
})
const qr = computed(() => (view.value ? `data:image/svg+xml;base64,${btoa(view.value.qr_svg)}` : ''))

const timer = setInterval(() => {
  now.value = Date.now()
}, 1000)
const poll = setInterval(() => {
  if (!paid.value && !review.value && !document.hidden) void load(true)
}, 4000)
onBeforeUnmount(() => {
  clearInterval(timer)
  clearInterval(poll)
})

async function copy(text: string) {
  if (await copyText(text)) showSuccessToast(t('pay.copied'))
}
function orderLink() {
  void router.push(isCheckout.value ? '/orders' : `/orders/${encodeURIComponent(no.value)}`)
}
async function retry() {
  try {
    if (isCheckout.value) await payCheckout(no.value)
    else await payOrder({ id: no.value, checkout_no: null })
  } catch {
    /* already toasted */
  }
}
</script>

<template>
  <div class="page">
    <van-nav-bar :title="t('pay.usdtTitle')" left-arrow fixed placeholder safe-area-inset-top @click-left="goBack(router, '/orders')" />
    <div v-if="failed" class="pad center">
      <p class="muted">{{ t('pay.loadFail') }}</p>
      <van-button round block @click="orderLink">{{ t('pay.viewOrder') }}</van-button>
    </div>
    <div v-else-if="!view" class="pad center"><van-loading /></div>
    <div v-else class="pad">
      <div class="card box">
        <p class="net">{{ view.label }} · {{ view.network }}</p>
        <template v-if="paid || review">
          <p class="state ok">{{ paid ? t('pay.confirmed') : t('pay.review') }}</p>
          <van-button type="primary" round block @click="orderLink">{{ t('pay.viewOrder') }}</van-button>
        </template>
        <template v-else-if="expired">
          <p class="state warn">{{ t('pay.expired') }}</p>
          <van-button type="primary" round block @click="retry">{{ t('pay.retry') }}</van-button>
        </template>
        <template v-else>
          <button type="button" class="amount" @click="copy(view.usdt_amount)">
            <span class="cap">{{ t('pay.amount') }}</span>
            <span class="num">{{ view.usdt_amount }}<small>USDT</small></span>
            <span class="hint">{{ t('pay.copyAmount') }}</span>
          </button>
          <div class="meta">
            <span class="cap">{{ t('pay.expiresIn') }}</span>
            <span class="clock" :class="{ low: left < 60 }">{{ clock }}</span>
          </div>
          <img class="qr" :src="qr" :alt="t('pay.qrAlt')" width="180" height="180" />
          <div class="addr-block">
            <span class="cap">{{ t('pay.address') }}</span>
            <button type="button" class="addr" :aria-label="`${t('pay.copyAddress')}: ${view.address}`" @click="copy(view.address)">
              <b>{{ addr.head }}</b>
              <span v-for="(g, i) in addr.mid" :key="i" class="g">{{ g }}</span>
              <b v-if="addr.tail" class="g">{{ addr.tail }}</b>
            </button>
            <p v-if="addr.tail" class="verify">{{ t('pay.verify', { short: addr.short, n: EDGE }) }}</p>
          </div>
          <div class="meta">
            <span class="cap">{{ t('order.amount') }}</span>
            <span class="val">{{ formatMoney(fromMinor(BigInt(view.fiat_amount_minor)), view.currency, locale) }}</span>
          </div>
          <p class="state wait"><van-loading size="16" /> {{ t('pay.waiting') }}</p>
          <div class="notes">
            <p class="muted small">{{ t('pay.exact') }}</p>
            <p class="muted small" data-testid="usdt-confirm-tip">{{ t('pay.confirmTip') }}</p>
          </div>
          <van-button type="primary" round block @click="copy(view.address)">{{ t('pay.copyAddress') }}</van-button>
          <van-button round block plain @click="orderLink">{{ t('pay.viewOrder') }}</van-button>
          <a v-if="view.sim_url" class="sim" :href="view.sim_url">{{ t('pay.simulate') }}</a>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page {
  min-height: 100vh;
  padding-bottom: calc(var(--safe-bottom) + 16px);
}
.pad {
  padding: 16px;
}
.center {
  text-align: center;
}
.box {
  display: flex;
  flex-direction: column;
  gap: 20px;
  align-items: stretch;
  padding: 24px 20px;
}
.net {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--c-muted);
}
.cap {
  font-size: 12px;
  letter-spacing: 0.04em;
  color: var(--c-muted);
}
.amount,
.addr {
  background: none;
  border: 0;
  padding: 0;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.amount {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-height: 44px;
}
.num {
  font-size: clamp(2rem, 10vw, 2.75rem);
  font-weight: 700;
  line-height: 1.05;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.num small {
  margin-left: 8px;
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--c-muted);
}
.hint {
  font-size: 12px;
  color: var(--c-muted);
  text-decoration: underline;
  text-underline-offset: 3px;
}
.meta {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  padding-top: 16px;
  border-top: 1px solid var(--c-line);
}
.clock,
.val {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.clock.low {
  color: var(--c-danger);
}
.qr {
  align-self: center;
  background: #fff;
  padding: 8px;
  border-radius: var(--radius-s);
}
.addr-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 16px;
  border-top: 1px solid var(--c-line);
}
.addr {
  min-height: 44px;
  padding: 12px 14px;
  border: 1px solid var(--c-line);
  border-radius: var(--radius-s);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 14px;
  line-height: 1.7;
  color: var(--c-muted);
  word-break: break-all;
  transition: border-color 0.15s;
}
.addr:active {
  border-color: var(--c-text);
}
.addr b {
  font-size: 16px;
  font-weight: 700;
  color: var(--c-text);
}
.addr .g {
  margin-left: 0.5ch;
}
.verify {
  margin: 0;
  font-size: 12px;
  color: var(--c-muted);
}
.notes {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.notes p {
  margin: 0;
  line-height: 1.5;
}
.state {
  margin: 0;
  text-align: center;
}
.sim {
  text-align: center;
  min-height: 44px;
  line-height: 44px;
}
.state.ok {
  color: var(--c-success);
}
.state.warn {
  color: var(--c-danger);
}
@media (prefers-reduced-motion: reduce) {
  .addr {
    transition: none;
  }
}
</style>
