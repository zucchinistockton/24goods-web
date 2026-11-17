<script setup lang="ts">
/**
 * 3DS one-time codes for the virtual card(s) of one order. A code arrives over the realtime
 * stream while the buyer is paying somewhere with the card; this panel also asks the server once
 * when it opens (and on demand). Codes live only in this component: no storage, gone on leave.
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { copyText, type CardOtp } from '@24goods/shared'
import { api } from '../api'
import { useRealtimeStore } from '../stores/realtime'
import { showToast } from '../ui'

const props = defineProps<{ orderId: string }>()
const { t } = useI18n()
const rt = useRealtimeStore()
type Live = CardOtp & { until: number }
const codes = ref<Live[]>([])
const now = ref(Date.now())
const loading = ref(false)

function put(c: CardOtp) {
  const live = { ...c, until: Date.now() + Math.max(0, c.expires_in) * 1000 }
  // one code per card unit: a newer one replaces the older
  codes.value = [live, ...codes.value.filter((x) => x.unit !== c.unit)]
}
async function check() {
  if (loading.value) return
  loading.value = true
  try {
    const r = await api.orders.cardOtp(props.orderId)
    r.items.forEach(put)
  } catch {
    // nothing to show is the normal case; the stream delivers a code when one arrives
  } finally {
    loading.value = false
  }
}
const left = (c: Live) => Math.max(0, Math.ceil((c.until - now.value) / 1000))
const clock = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
async function copy(c: Live) {
  showToast((await copyText(c.otp)) ? t('cardOtp.copied') : t('cardOtp.copyFailed'))
}

const off = rt.on('card_otp', (e) => {
  if (e.order_id === props.orderId) put(e)
})
const timer = window.setInterval(() => {
  now.value = Date.now()
  codes.value = codes.value.filter((c) => c.until > now.value)
}, 1000)
onMounted(check)
onBeforeUnmount(() => {
  off()
  window.clearInterval(timer)
  codes.value = []
})
</script>

<template>
  <div class="otp" aria-live="polite">
    <h3 class="otp-title">{{ t('cardOtp.title') }}</h3>
    <template v-if="codes.length">
      <div v-for="c in codes" :key="c.unit" class="otp-row">
        <button type="button" class="otp-code" :aria-label="t('cardOtp.copy')" @click="copy(c)">{{ c.otp }}</button>
        <div class="otp-meta">
          <span class="otp-card">{{ c.card }}</span>
          <span class="otp-left">{{ t('cardOtp.expiresIn', { time: clock(left(c)) }) }}</span>
        </div>
      </div>
    </template>
    <p v-else class="otp-none">{{ t('cardOtp.none') }}</p>
    <van-button plain round size="small" class="otp-check" :loading="loading" @click="check">{{ t('cardOtp.check') }}</van-button>
  </div>
</template>

<style scoped>
.otp {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--c-fill-strong);
}
.otp-title {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 600;
}
.otp-row {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 8px 0;
}
.otp-code {
  min-height: 48px;
  padding: 4px 12px;
  border: 1px solid var(--c-note-line);
  border-radius: 8px;
  background: var(--c-note-bg);
  color: inherit;
  font: 700 26px/1.2 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.12em;
}
.otp-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 13px;
  color: var(--c-text-2);
}
.otp-card {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.otp-left {
  color: var(--c-muted);
  font-variant-numeric: tabular-nums;
}
.otp-none {
  margin: 0 0 12px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--c-muted);
}
.otp-check {
  min-height: 44px;
  padding: 0 16px;
}
</style>
