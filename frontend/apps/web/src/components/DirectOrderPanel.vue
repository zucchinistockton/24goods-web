<script setup lang="ts">
/**
 * What the buyer may see of a direct order: the masked account, the plan and where it stands.
 * When the first session expired or was refused, the buyer can supply a fresh one here.
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { DirectOrderInfo } from '@24goods/shared'
import { api } from '../api'
import DirectProgress from './DirectProgress.vue'
import { createCoalescer } from '../lib/progressUi'
import DirectSessionSheet from './DirectSessionSheet.vue'
import { useRealtimeStore } from '../stores/realtime'
import { showSuccessToast, toastError } from '../ui'

const props = defineProps<{ orderId: string; productId: string | null; canResupply: boolean }>()
const { t, te } = useI18n()
const info = ref<DirectOrderInfo | null>(null)
const asking = ref(false)
const sending = ref(false)

async function load() {
  try {
    info.value = await api.orders.direct(props.orderId)
  } catch {
    info.value = null // not a direct order for this buyer, or not available yet: show nothing
  }
}
onMounted(load)
// issue #30: the engine publishes a coarse event when the buyer's step changes; the page then
// refetches the direct info. Debounced (500 ms) and coalesced: never two loads in flight, and
// events that arrive during one cost a single extra load. Nothing polls beyond the page's sync.
const rt = useRealtimeStore()
const refetch = createCoalescer(load, 500)
const off = rt.on('order_progress', (e) => {
  if (e.order_id === props.orderId) refetch.trigger()
})
onBeforeUnmount(() => {
  off()
  refetch.cancel()
})
defineExpose({ load })

async function resupply(session: string) {
  asking.value = false
  if (sending.value) return
  sending.value = true
  try {
    await api.orders.directSession(props.orderId, session)
    showSuccessToast(t('direct.resupplied'))
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    sending.value = false
  }
}
const stateText = (s: string | null) => (s && te(`direct.state.${s}`) ? t(`direct.state.${s}`) : t('direct.state.working'))
</script>

<template>
  <div v-if="info" class="direct">
    <h3 class="direct-title">{{ t('direct.orderTitle') }}</h3>
    <DirectProgress v-if="info.progress" :progress="info.progress" />
    <dl class="facts">
      <div>
        <dt>{{ t('direct.account') }}</dt>
        <dd>{{ info.account ?? t('direct.accountPending') }}</dd>
      </div>
      <div v-if="info.plan">
        <dt>{{ t('direct.plan') }}</dt>
        <dd>{{ info.plan }}</dd>
      </div>
      <div>
        <dt>{{ t('direct.status') }}</dt>
        <dd>{{ stateText(info.waiting_for_supplier ? 'waiting_for_supplier' : info.state) }}</dd>
      </div>
    </dl>
    <template v-if="canResupply && info.state === 'needs_manual'">
      <p class="hint">{{ t('direct.resupplyHint') }}</p>
      <van-button plain round block :loading="sending" @click="asking = true">{{ t('direct.resupply') }}</van-button>
    </template>
    <DirectSessionSheet v-model:show="asking" :product-id="productId" @confirm="resupply" />
  </div>
</template>

<style scoped>
.direct-title {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 600;
}
.facts {
  margin: 0;
  border-top: 1px solid var(--c-fill-strong);
}
.facts > div {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 0;
  border-bottom: 1px solid var(--c-fill-strong);
  font-size: 14px;
}
.facts dt {
  color: var(--c-text-2);
}
.facts dd {
  margin: 0;
  font-weight: 600;
  text-align: end;
  word-break: break-all;
}
.hint {
  margin: 12px 0;
  font-size: 13px;
  line-height: 1.5;
  color: var(--c-muted);
}
</style>
