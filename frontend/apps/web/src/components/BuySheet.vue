<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ProductThumb } from '@24goods/shared/ui'
import {
  feeBreakdown,
  priceParts,
  multiplyAmount,
  type BuyerInputConfig,
  type HalfCheckMethod,
  type InstantCredentials,
  type ProductSummary,
} from '@24goods/shared'
import { api } from '../api'
import { useAuthStore } from '../stores/auth'
import { useSiteStore } from '../stores/site'
import BuyerInputSheet from './BuyerInputSheet.vue'
import DirectSessionSheet from './DirectSessionSheet.vue'
import HalfCheckSheet from './HalfCheckSheet.vue'
import VariantPicker from './VariantPicker.vue'
import FeeLines from './FeeLines.vue'
import ManualPromise from './ManualPromise.vue'
import PreorderBadge from './PreorderBadge.vue'
import { preorderRange } from '../lib/preorder'
import PromoField, { type AppliedPromo } from './PromoField.vue'
import TurnstileBox from './TurnstileBox.vue'
import VariantChips from './VariantChips.vue'
import { defaultVariantId, findVariant, hasVariants, withVariant } from '../lib/variants'
import { usesOptions } from '../lib/variantOptions'
import { recalledSrc, stashAccessCode } from '../lib/attribution'
import { usePow, isPowAbort } from '../composables/usePow'
import { useTurnstile } from '../composables/useTurnstile'
import CredentialsSheet from './CredentialsSheet.vue'
import PowStatus from './PowStatus.vue'
import { ask, errorText, showToast, toastError } from '../ui'
import { isApiError, type ApiError } from '@24goods/shared'
import { isSoldOut, maxOrderQty } from '../lib/stock'
import { siteAccessCleared } from '../lib/securityChallenge'

const props = defineProps<{ show: boolean; product: (ProductSummary & { buyer_input?: BuyerInputConfig | null }) | null; qty?: number; src?: string | null; variantId?: string | null }>()
const emit = defineEmits<{ 'update:show': [v: boolean] }>()
const { t, locale } = useI18n()
const router = useRouter()
const auth = useAuthStore()
const site = useSiteStore()
const pow = usePow()
const ts = useTurnstile('instant_account')
const promo = ref<AppliedPromo | null>(null)

const qty = ref(1)
const submitting = ref(false)
const creds = ref<InstantCredentials | null>(null)
const showCreds = ref(false)
const showInput = ref(false)
/** The order-information value collected by the §4 popup for this purchase. */
const inputValue = ref<string | null>(null)
/** upstream_direct: the pasted account session, in memory only until the order request is sent */
const sessionValue = ref<string | null>(null)
/** the buyer lets support see the session if the top-up fails (review-v12 §26) */
const shareValue = ref(false)
const showSession = ref(false)
/** issue #27: the passed half-price eligibility check an order of a half-price plan is created with */
const halfId = ref<string | null>(null)
const halfConfirmed = ref(false)
/** U10: the buyer ticked "I understand this is a pre-order" (required before ordering). */
const preorderOk = ref(false)
const showHalf = ref(false)
const halfMethod = ref<HalfCheckMethod | null>(null)

/**
 * The order-information form config. A summary without the field (an older cached list, a chat
 * card) is "unknown", not "off": fetch the product before any order can be placed (issue 49 B).
 */
const resolved = ref<{ id: string; config: BuyerInputConfig | null } | null>(null)
const resolving = ref(false)
const resolveFailed = ref(false)
/** v11 W1: the variant picked in this sheet (products with variants are bought through one). */
const variantSel = ref<string | null>(null)
const base = computed(() => {
  const x = props.product
  if (!x || x.buyer_input !== undefined) return x
  return resolved.value?.id === x.id ? { ...x, buyer_input: resolved.value.config } : x
})
const variant = computed(() => findVariant(base.value, variantSel.value))
const p = computed(() => (base.value ? withVariant(base.value, variant.value) : base.value))
const configKnown = computed(() => p.value?.buyer_input !== undefined)
async function resolveConfig() {
  const x = props.product
  if (!x || x.buyer_input !== undefined || resolving.value) return
  resolving.value = true
  resolveFailed.value = false
  try {
    const full = await api.product(x.id)
    if (props.product?.id === x.id) resolved.value = { id: x.id, config: full.buyer_input ?? null }
  } catch {
    resolveFailed.value = true
  } finally {
    resolving.value = false
  }
}
watch(siteAccessCleared, () => {
  if (props.show && resolveFailed.value) void resolveConfig()
})
const soldOut = computed(() => (p.value ? isSoldOut(p.value) : false))
const canSubmit = computed(
  () => configKnown.value && !soldOut.value && (!hasVariants(base.value) || variant.value !== null) && (!p.value?.preorder || preorderOk.value),
)
const price = computed(() => (p.value ? priceParts(p.value.price, p.value.currency, locale.value) : null))
/** A direct product is applied to ONE account: one unit, and the account session comes with the order. */
const isDirect = computed(() => p.value?.delivery_mode === 'upstream_direct')
// a direct top-up is one account, a half-price plan one unit per check (issue #54 F5)
const maxQty = computed(() => (p.value && !isDirect.value && !needsHalf.value ? maxOrderQty(p.value) : 1))
const subtotal = computed(() => (p.value ? (multiplyAmount(p.value.price, qty.value) ?? p.value.price) : '0.00'))
const discountMinor = computed(() => (promo.value && !promo.value.stale ? promo.value.discount_minor : 0))
const bd = computed(() => feeBreakdown(subtotal.value, discountMinor.value, site.permille))
const promoItems = computed(() =>
  p.value ? [{ product_id: p.value.id, qty: qty.value, ...(variant.value ? { variant_id: variant.value.id } : {}) }] : [],
)
const needsInput = computed(() => p.value?.buyer_input?.enabled === true)
/** a half-price plan (the selected variant's, or the product's own) needs the check first */
const needsHalf = computed(() => (variant.value ? variant.value.half_price === true : !hasVariants(base.value) && p.value?.half_price === true))

watch(
  () => props.show,
  (v) => {
    if (v) {
      variantSel.value = findVariant(props.product, props.variantId ?? null)?.id ?? defaultVariantId(props.product?.variants)
      qty.value = Math.min(Math.max(1, Math.floor(props.qty ?? 1)), maxQty.value)
      inputValue.value = null
      sessionValue.value = null
      halfId.value = null
      halfConfirmed.value = false
      preorderOk.value = false
      promo.value = null
      void site.loadChannels()
      void resolveConfig()
    } else {
      // the sheet closed (ordered or dismissed): the account session and the check are not kept
      sessionValue.value = null
      halfId.value = null
      halfConfirmed.value = false
    }
  },
)

watch(maxQty, (m) => {
  if (qty.value > m) qty.value = m
})
watch(variantSel, (now, before) => {
  if (before !== null && now !== before) {
    promo.value = null // previewed on the previous variant's price
    halfId.value = null // a check belongs to one plan and region
    halfConfirmed.value = false
  }
})

function buyPath(): string {
  const id = encodeURIComponent(p.value?.id ?? '')
  const q = new URLSearchParams({ qty: String(qty.value) })
  if (variant.value) q.set('variant', variant.value.id)
  const src = props.src ?? recalledSrc()
  if (src) q.set('src', src)
  return `/buy/${id}?${q.toString()}`
}
function signIn() {
  emit('update:show', false)
  void router.push({ name: 'login', query: { redirect: buyPath() } })
}

async function placeOrder() {
  const x = p.value
  if (!x || submitting.value || !canSubmit.value) return
  submitting.value = true
  try {
    // Price is decided by the server; only product id + qty (+ attribution tag) are sent.
    const session = sessionValue.value
    sessionValue.value = null // one use: a failed order asks for it again
    const half = halfId.value
    const order = await api.orders.create(x.id, qty.value, props.src ?? recalledSrc(), inputValue.value, promo.value?.code, variant.value?.id, session, half, halfConfirmed.value, shareValue.value, preorderOk.value)
    halfId.value = null // consumed: it can never be used twice
    halfConfirmed.value = false
    stashAccessCode(order.id, order.access_code)
    emit('update:show', false)
    await router.push(`/orders/${encodeURIComponent(order.id)}`)
  } catch (e) {
    if (isApiError(e) && e.code.startsWith('preflight_')) void preflightRefused(e)
    else toastError(e)
  } finally {
    submitting.value = false
  }
}
/** The account pre-check refused (review-v12 §24): no order exists and nothing was paid. Explain
 *  it in a dialog (not a toast that vanishes) and offer a fresh paste when the session is the problem. */
async function preflightRefused(e: ApiError) {
  const raw = (e.details as { current_plan?: unknown } | null)?.current_plan
  const plan = typeof raw === 'string' && /^[a-z0-9_ -]{1,40}$/i.test(raw) ? raw : null
  const msg = e.code === 'preflight_plan_active' && plan ? t('direct.preflight.planActive', { plan }) : errorText(e)
  const repaste = e.code === 'preflight_session_invalid'
  const again = await ask(t('direct.preflight.title'), msg, repaste ? t('direct.pasteAgain') : t('common.ok'), repaste ? t('common.cancel') : undefined)
  if (again && repaste && isDirect.value) {
    pendingGuest = false
    showSession.value = true
  }
}

/** Buy now: the order-information popup (if enabled) comes first, then the order is created. */
function begin(guest: boolean) {
  if (!canSubmit.value) return
  if (needsInput.value && inputValue.value === null) {
    pendingGuest = guest
    showInput.value = true
    return
  }
  if (needsHalf.value && (guest || auth.user?.is_instant)) {
    // the check needs a registered account (a guest account would make it an account oracle)
    showToast(t('halfcheck.registerFirst'))
    void (guest ? signIn() : router.push({ name: 'settings' }))
    return
  }
  if (needsHalf.value && halfId.value === null) {
    void startHalf()
    return
  }
  if (isDirect.value && sessionValue.value === null) {
    pendingGuest = guest
    showSession.value = true
    return
  }
  void (guest ? continueAsGuest() : placeOrder())
}
/** Half-price plans: ask how the shop checks (it decides whether a session is needed), then check. */
async function startHalf() {
  if (submitting.value) return
  submitting.value = true
  try {
    const r = await api.halfChecks.required(p.value?.id ?? '', variant.value?.id)
    if (!r.required) {
      halfId.value = ''
      begin(false) // nothing to check any more (the shop changed): carry on without it
      return
    }
    halfMethod.value = r.method
    showHalf.value = true
  } catch (e) {
    toastError(e)
  } finally {
    submitting.value = false
  }
}
function halfPassed(id: string, session: string | null, confirmed: boolean, share = false) {
  shareValue.value = share
  halfId.value = id
  halfConfirmed.value = confirmed
  showHalf.value = false
  // a direct order is applied with the very session that was just checked: not asked twice
  if (isDirect.value && session) sessionValue.value = session
  begin(false)
}
function sessionConfirmed(v: string, share = false) {
  sessionValue.value = v
  shareValue.value = share
  showSession.value = false
  void (pendingGuest ? continueAsGuest() : placeOrder())
}
let pendingGuest = false
function inputConfirmed(v: string) {
  inputValue.value = v
  showInput.value = false
  begin(pendingGuest) // a direct product asks for the account session next
}

async function continueAsGuest() {
  if (submitting.value || pow.solving.value) return
  submitting.value = true
  try {
    const token = ts.require()
    const solution = await pow.run('instant')
    const r = await api.auth.instant(token ? { pow: solution, turnstile_token: token } : { pow: solution })
    auth.setUser(r.user)
    creds.value = r.credentials
    showCreds.value = true
  } catch (e) {
    if (!isPowAbort(e)) toastError(e)
  } finally {
    ts.reset()
    submitting.value = false
  }
}
async function credsDone() {
  showCreds.value = false
  creds.value = null // plaintext password leaves memory as soon as it was acknowledged
  if (needsHalf.value && halfId.value === null) begin(false) // the check comes next
  else await placeOrder()
}
</script>

<template>
  <van-popup
    :show="show && !showCreds && !showInput && !showSession && !showHalf"
    position="bottom"
    round
    closeable
    safe-area-inset-bottom
    class="buy-sheet"
    teleport="body"
    @update:show="emit('update:show', $event)"
  >
    <div v-if="p" class="sheet">
      <div class="sheet-head">
        <ProductThumb :src="p.cover" :title="p.title" :size="72" :radius="8" />
        <div>
          <div v-if="price" class="price big"><small>{{ price.symbol }}</small>{{ price.value }}</div>
          <div class="muted sheet-title">{{ p.title }}</div>
        </div>
      </div>
      <div v-if="base?.variants?.length" class="sheet-variants">
        <VariantPicker v-if="usesOptions(base.variants)" v-model="variantSel" :variants="base.variants" :currency="base.currency" />
        <template v-else>
          <div class="sheet-label">{{ t('product.variantLabel') }}</div>
          <VariantChips v-model="variantSel" :variants="base.variants" />
        </template>
      </div>
      <p v-if="needsHalf" class="muted small sheet-note">{{ t('halfcheck.sheetNote') }}</p>
      <div class="sheet-row">
        <div>
          <div>{{ t('product.quantity') }}</div>
          <div class="muted small">{{ t('product.maxPerOrder', { n: maxQty }) }}</div>
        </div>
        <van-stepper v-model="qty" :min="1" :max="maxQty" integer :input-width="48" :button-size="36" />
      </div>
      <p v-if="isDirect" class="muted small sheet-note">{{ t('direct.sheetNote') }}</p>
      <template v-if="p.preorder">
        <PreorderBadge class="sheet-manual" :window="p.preorder" />
        <van-checkbox v-model="preorderOk" shape="square" icon-size="18px" class="sheet-note preorder-ack">{{ t('preorder.ack', { range: preorderRange(t, p.preorder) }) }}</van-checkbox>
      </template>
      <ManualPromise v-else-if="p.delivery_mode === 'manual'" class="sheet-manual" :eta="p.delivery_eta_hours" compact />
      <p v-if="soldOut" class="muted small sheet-note" role="alert">{{ t('product.outOfStock') }}</p>
      <div v-else-if="resolveFailed" class="sheet-note sheet-retry" role="alert">
        <span>{{ t('buy.configFailed') }}</span>
        <van-button size="small" plain round class="retry" @click="resolveConfig">{{ t('buy.retry') }}</van-button>
      </div>
      <PromoField v-model="promo" :items="promoItems" />
      <p v-if="promo?.stale" class="muted small">{{ t('promo.stale') }}</p>
      <div class="sheet-fees">
        <FeeLines
          :subtotal="subtotal"
          :discount-minor="discountMinor"
          :fee="bd.fee"
          :currency="p.currency"
          :label="site.channel?.label"
          :rate="site.channel?.fee_rate"
        />
      </div>

      <template v-if="auth.loggedIn">
        <van-button block round type="primary" size="large" :loading="submitting || resolving" :loading-text="isDirect && submitting ? t('direct.preflight.checking') : undefined" :disabled="!canSubmit" @click="begin(false)">
          {{ t('product.placeOrder') }}
        </van-button>
      </template>
      <template v-else>
        <p v-if="site.features.guest_checkout" class="muted small guest-note">{{ t('buy.guestNote') }}</p>
        <PowStatus :solving="pow.solving.value" :progress="pow.progress.value" />
        <TurnstileBox v-if="site.features.guest_checkout" :ts="ts" />
        <van-button
          v-if="site.features.guest_checkout"
          block
          round
          type="primary"
          size="large"
          :loading="submitting || resolving"
          :disabled="!canSubmit"
          @click="begin(true)"
        >
          {{ t('buy.continueGuest') }}
        </van-button>
        <van-button block round :type="site.features.guest_checkout ? undefined : 'primary'" :plain="site.features.guest_checkout" size="large" class="signin" @click="signIn">{{ t('me.login') }}</van-button>
      </template>
    </div>
  </van-popup>
  <HalfCheckSheet
    v-if="p"
    v-model:show="showHalf"
    :product-id="p.id"
    :variant-id="variant?.id ?? null"
    :method="halfMethod"
    :known="p.direct_product"
    :preset="sessionValue"
    @passed="halfPassed"
  />
  <DirectSessionSheet v-model:show="showSession" :product-id="p?.id ?? null" :known="p?.direct_product" @confirm="sessionConfirmed" />
  <BuyerInputSheet v-model:show="showInput" :config="p?.buyer_input ?? null" :initial="inputValue" @confirm="inputConfirmed" />
  <CredentialsSheet :show="showCreds" :credentials="creds" @done="credsDone" />
</template>

<style scoped>
.sheet {
  padding: 20px 16px 16px;
}
.sheet-head {
  display: flex;
  gap: 12px;
  align-items: center;
  padding-right: 32px;
}
.price.big {
  font-size: 26px;
}
.price small {
  font-size: 15px;
  margin-right: 2px;
}
.sheet-title {
  font-size: 13px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.sheet-variants {
  padding: 12px 0;
  border-bottom: 1px solid var(--c-line);
}
.sheet-label {
  margin-bottom: 8px;
}
.sheet-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 56px;
  border-bottom: 1px solid var(--c-line);
  margin-bottom: 4px;
}
.sheet-row:last-of-type {
  border-bottom: 0;
  margin-bottom: 12px;
}
.sheet-manual {
  padding: 8px 0;
}
.sheet-fees {
  padding: 12px 0;
}
.small {
  font-size: 12px;
}
.sheet-note {
  margin: 0 0 8px;
  font-size: 13px;
}
.sheet-retry {
  display: flex;
  align-items: center;
  gap: 8px;
}
.retry {
  flex-shrink: 0;
  min-height: 44px;
}
.preorder-ack {
  min-height: 44px;
  font-size: 13px;
  line-height: 1.4;
}
.guest-note {
  margin: 0 0 4px;
}
.signin {
  margin-top: 8px;
}
</style>
