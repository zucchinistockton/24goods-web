<script setup lang="ts">
/**
 * issue #27: the half-price eligibility check BEFORE payment. The method is the shop's choice:
 * `vendor_preflight` / `chatgpt_direct` read the buyer's own account with the session pasted
 * here (sealed by the server at once, never stored, used only for this read); `manual` reads
 * nothing and staff confirm after payment. The session lives in this component's memory only
 * while the flow is open and is handed to the parent only when the order itself needs it.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { DirectProduct, HalfCheck, HalfCheckMethod } from '@24goods/shared'
import { api } from '../api'
import { toastError } from '../ui'
import DirectSessionSheet from './DirectSessionSheet.vue'

const props = defineProps<{
  show: boolean
  productId: string
  variantId: string | null
  method: HalfCheckMethod | null
  known?: DirectProduct | null
  /** a session the buyer already pasted for this order (a direct product): not asked twice */
  preset?: string | null
}>()
const emit = defineEmits<{
  'update:show': [v: boolean]
  passed: [id: string, session: string | null, confirmed: boolean]
}>()
const { t, te } = useI18n()

const step = ref<'session' | 'busy' | 'result'>('session')
const result = ref<HalfCheck | null>(null)
/** a `manual` check is decided by staff after payment: the buyer ticks that they understand */
const understood = ref(false)
let kept: string | null = null

const askSession = computed(() => props.show && step.value === 'session')
const open = computed(() => props.show && step.value !== 'session')
const ok = computed(() => result.value?.status === 'passed' || result.value?.status === 'manual_pending')
const reasonText = computed(() => {
  const r = result.value
  if (!r) return ''
  const key = `halfcheck.reason.${r.reason ?? ''}`
  if (r.reason && te(key)) return t(key)
  return t(`halfcheck.status.${r.status}`)
})

watch(
  () => props.show,
  (v) => {
    result.value = null
    understood.value = false
    kept = null
    if (!v) return
    if (props.method === 'manual') {
      void run(null)
    } else if (props.preset) {
      void run(props.preset)
    } else {
      step.value = 'session'
    }
  },
)

async function run(session: string | null) {
  step.value = 'busy'
  kept = session
  try {
    result.value = await api.halfChecks.create(props.productId, props.variantId, session)
    step.value = 'result'
  } catch (e) {
    toastError(e)
    // an unusable session or an unavailable check: back to the paste step (or close for manual)
    if (props.method === 'manual') emit('update:show', false)
    else step.value = 'session'
  }
}
function onSession(s: string) {
  void run(s)
}
function again() {
  if (kept && props.method !== 'manual') void run(kept)
  else step.value = 'session'
}
function repaste() {
  kept = null
  step.value = 'session'
}
function proceed() {
  const r = result.value
  if (!r || !ok.value) return
  if (r.status === 'manual_pending' && !understood.value) return
  const s = kept
  kept = null
  emit('passed', r.id, s, r.status === 'manual_pending')
}
function close() {
  kept = null
  result.value = null
  emit('update:show', false)
}
onBeforeUnmount(() => {
  kept = null // the pasted session never outlives the component
})
</script>

<template>
  <DirectSessionSheet
    :show="askSession"
    purpose="check"
    :product-id="productId"
    :known="known ?? 'gpt'"
    @update:show="(v: boolean) => !v && close()"
    @confirm="onSession"
  />
  <van-popup :show="open" position="bottom" round safe-area-inset-bottom teleport="body" class="buy-sheet" @update:show="(v: boolean) => !v && close()">
    <div class="hc" aria-live="polite">
      <template v-if="step === 'busy'">
        <van-loading size="28" />
        <p class="msg">{{ t('halfcheck.checking') }}</p>
      </template>
      <template v-else-if="result">
        <h2 :class="{ good: ok, bad: !ok }">
          {{ ok ? t('halfcheck.okTitle') : t('halfcheck.noTitle') }}
        </h2>
        <p class="msg">{{ ok ? t(`halfcheck.status.${result.status}`) : reasonText }}</p>
        <p v-if="result.account" class="muted small">{{ t('halfcheck.account', { a: result.account }) }}</p>
        <van-checkbox v-if="result.status === 'manual_pending'" v-model="understood" class="understood">{{ t('halfcheck.confirmManual') }}</van-checkbox>
        <van-button v-if="ok" block round type="primary" size="large" :disabled="result.status === 'manual_pending' && !understood" @click="proceed">{{ t('halfcheck.continue') }}</van-button>
        <template v-else>
          <van-button v-if="result.status === 'session_invalid'" block round type="primary" size="large" @click="repaste">
            {{ t('halfcheck.repaste') }}
          </van-button>
          <van-button v-else-if="result.status === 'unavailable'" block round type="primary" size="large" @click="again">
            {{ t('halfcheck.retry') }}
          </van-button>
          <van-button block round plain size="large" class="close" @click="close">{{ t('halfcheck.close') }}</van-button>
        </template>
      </template>
    </div>
  </van-popup>
</template>

<style scoped>
.hc {
  padding: 24px 16px 16px;
  text-align: center;
}
.hc h2 {
  margin: 0 0 8px;
  font-size: 18px;
}
.good {
  color: var(--c-success, inherit);
}
.bad {
  color: var(--c-danger, inherit);
}
.msg {
  margin: 0 0 16px;
  font-size: 14px;
  line-height: 1.6;
  color: var(--c-text-2);
}
.close {
  margin-top: 8px;
}
.understood {
  margin: 0 0 16px;
  text-align: left;
}
</style>
