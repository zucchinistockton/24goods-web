<script setup lang="ts">
/**
 * ADR-057: the buyer's side of an SMS-code order. No choices: Get number, copy it, wait for the
 * code, swap the number if nothing arrives. The code itself comes from the reveal endpoint.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { copyText, type SmsView } from '@24goods/shared'
import { api } from '../api'
import { openSupport as support } from '../lib/support'
import { attemptsLeft, formatClock, formatPhone, secondsUntil, shouldPoll, smsPhase, splitSmsReveal, swapSecondsLeft } from '../lib/smsPanel'
import { showToast, toastError } from '../ui'
import { useRouter } from 'vue-router'

const props = defineProps<{ orderId: string }>()
const emit = defineEmits<{ changed: [] }>()
const { t } = useI18n()
const router = useRouter()

const POLL_MS = 3000
const view = ref<SmsView | null>(null)
const fetchedAt = ref(Date.now())
const now = ref(Date.now())
const busy = ref(false)
const code = ref('')
const revealedPhone = ref('')
const revealFailed = ref(false)
let revealing = false
let pollTimer: ReturnType<typeof setInterval> | undefined
let tickTimer: ReturnType<typeof setInterval> | undefined

const phase = computed(() => smsPhase(view.value))
const phone = computed(() => formatPhone(view.value?.current?.phone ?? ''))
const swapIn = computed(() => swapSecondsLeft(view.value, fetchedAt.value, now.value))
const left = computed(() => attemptsLeft(view.value))
const waitLeft = computed(() => secondsUntil(view.value?.current?.expires_at, now.value))
const canSwap = computed(() => !busy.value && swapIn.value === 0 && left.value > 0)

function apply(v: SmsView) {
  const was = view.value?.state
  view.value = v
  fetchedAt.value = Date.now()
  if (v.state !== was && (v.state === 'delivered' || v.state === 'failed')) emit('changed')
  if (v.state === 'delivered') void showCode()
}

async function load() {
  try {
    apply(await api.orders.sms(props.orderId))
  } catch {
    /* a poll miss is retried on the next tick */
  }
}

async function showCode() {
  if (code.value || revealing) return
  revealing = true
  revealFailed.value = false
  try {
    const r = await api.orders.reveal(props.orderId)
    const p = splitSmsReveal(r.cards[0]?.code)
    code.value = p.code
    revealedPhone.value = formatPhone(p.phone)
    if (!p.code) revealFailed.value = true
  } catch (e) {
    revealFailed.value = true
    toastError(e)
  } finally {
    revealing = false
  }
}

async function getNumber() {
  if (busy.value) return
  busy.value = true
  try {
    apply(await api.orders.smsNumber(props.orderId))
  } catch (e) {
    toastError(e) // sms_too_early, sms_unavailable, ... are translated by code
    void load()
  } finally {
    busy.value = false
  }
}

async function copy(s: string) {
  showToast(await copyText(s) ? t('common.copied') : t('common.copyFailed'))
}

function restart() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = setInterval(() => {
    if (shouldPoll(view.value, document.visibilityState !== 'hidden')) void load()
  }, POLL_MS)
}
function onVisible() {
  if (document.visibilityState !== 'visible') return
  if (shouldPoll(view.value, true)) void load()
  else if (phase.value === 'delivered' && !code.value) void showCode()
}
onMounted(() => {
  void load()
  restart()
  tickTimer = setInterval(() => (now.value = Date.now()), 1000)
  document.addEventListener('visibilitychange', onVisible)
})
onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (tickTimer) clearInterval(tickTimer)
  document.removeEventListener('visibilitychange', onVisible)
})
watch(() => props.orderId, () => {
  view.value = null
  code.value = ''
  revealedPhone.value = ''
  revealFailed.value = false
  void load()
})
</script>

<template>
  <div class="sms" data-test="sms-panel" :data-phase="phase" aria-live="polite" aria-atomic="false">
    <template v-if="phase === 'ready'">
      <p class="sms-line">{{ t('sms.readyLine') }}</p>
      <van-button block round type="primary" size="large" class="tap" :loading="busy" @click="getNumber">{{ t('sms.getNumber') }}</van-button>
    </template>

    <template v-else-if="phase === 'active'">
      <div class="sms-big" data-test="sms-phone">{{ phone }}</div>
      <van-button block round plain type="primary" class="tap" @click="copy(phone)">{{ t('sms.copyNumber') }}</van-button>
      <p class="sms-wait muted">
        <van-loading size="14" /> {{ t('sms.waiting') }}
        <span v-if="waitLeft > 0" class="sms-clock">{{ formatClock(waitLeft) }}</span>
      </p>
      <van-button block round plain class="tap" :disabled="!canSwap" :loading="busy" data-test="sms-swap" @click="getNumber">
        {{ swapIn > 0 ? t('sms.anotherIn', { s: swapIn }) : t('sms.another') }}
      </van-button>
      <p class="muted small sms-left">{{ t('sms.attemptsLeft', { n: left }) }}</p>
    </template>

    <template v-else-if="phase === 'delivered'">
      <div class="sms-big sms-code" data-test="sms-code" :aria-label="code ? t('sms.codeIs', { code: code.split('').join(' ') }) : undefined">{{ code || '…' }}</div>
      <van-button v-if="code" block round type="primary" class="tap" @click="copy(code)">{{ t('sms.copyCode') }}</van-button>
      <van-button v-else-if="revealFailed" block round plain type="primary" class="tap" data-test="sms-reveal-retry" @click="showCode">{{ t('sms.showCodeAgain') }}</van-button>
      <p v-if="revealedPhone" class="muted small sms-left">{{ revealedPhone }}</p>
    </template>

    <template v-else-if="phase === 'failed'">
      <p class="sms-line" data-test="sms-failed">{{ t('sms.failed') }}</p>
      <van-button block round plain icon="chat-o" class="tap" @click="support(router)">{{ t('order.contact') }}</van-button>
    </template>

    <p v-else class="sms-wait muted"><van-loading size="14" /> {{ t('sms.preparing') }}</p>
  </div>
</template>

<style scoped>
.sms {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.sms-line,
.sms-wait,
.sms-left {
  margin: 0;
  text-align: center;
}
.sms-big {
  font-size: 28px;
  font-weight: 700;
  text-align: center;
  letter-spacing: 1px;
  word-break: break-all;
  font-variant-numeric: tabular-nums;
}
.sms-code {
  font-size: 36px;
  letter-spacing: 4px;
}
.sms-clock {
  margin-left: 6px;
  font-variant-numeric: tabular-nums;
}
</style>
