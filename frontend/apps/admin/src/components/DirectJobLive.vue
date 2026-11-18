<script setup lang="ts">
/**
 * Live supplier progress of a direct order inside the order drawer (owner request 2026-11-18):
 * the buyer's step, our job state and the latest supplier reports, refreshed when the supplier
 * reports a change (`order_progress` on the staff stream) or, while live updates are paused, on
 * every polling tick until the job is finished. Read-only: actions stay on the Procurement page.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { createCoalescer, formatDateTime } from '@24goods/shared'
import { client } from '../api'
import { createProcurementApi, type ProcJobDetail } from '../api-procurement'
import { useRealtimeStore } from '../stores/realtime'

const props = defineProps<{ orderId: string; version?: number }>()
const { t, te, locale } = useI18n()
const api = createProcurementApi(client)
const job = ref<ProcJobDetail | null>(null)
const none = ref(false)
const updatedAt = ref<number | null>(null)
const FINISHED = new Set(['fulfilled', 'failed', 'voided'])

async function load() {
  const list = await api.jobs.list({ order_id: props.orderId, page: 1, page_size: 5 })
  const first = list.items.find((j) => j.kind === 'direct') ?? list.items[0]
  if (!first) {
    job.value = null
    none.value = true
    return
  }
  const d = await api.jobs.get(first.id)
  if (d.order.id !== props.orderId) return // the drawer moved on to another order
  job.value = d
  none.value = false
  updatedAt.value = Date.now()
}
const live = createCoalescer(load, 300)
watch(
  () => props.orderId,
  () => {
    job.value = null
    none.value = false
    live.trigger()
  },
  { immediate: true },
)
watch(
  () => props.version,
  (v, old) => {
    if (v !== undefined && old !== undefined && v !== old) live.trigger()
  },
)
const rt = useRealtimeStore()
const offs = [
  rt.on('order_progress', (e) => {
    if (e.order_id === props.orderId) live.trigger()
  }),
  rt.on('sync', () => {
    if (!job.value || !FINISHED.has(job.value.state)) live.trigger()
  }),
]
onBeforeUnmount(() => {
  offs.forEach((off) => off())
  live.cancel()
})

const progress = computed(() => job.value?.buyer_progress ?? null)
const status = computed(() => {
  const o = progress.value?.outcome
  return o === 'completed' ? 'success' : o === 'failed' ? 'exception' : o === 'attention' ? 'warning' : undefined
})
const recent = computed(() => (job.value?.supplier_timeline ?? []).slice(-6).reverse())
const stateLabel = (s: string) => (te(`procurement.state.${s}`) ? t(`procurement.state.${s}`) : s)
const when = (iso: string | null | undefined) => formatDateTime(iso, locale.value)
</script>

<template>
  <section class="live" data-testid="direct-job-live">
    <h3 class="h3">
      {{ t('orders.live.title') }}
      <span v-if="rt.state === 'open'" class="dot on" :title="t('orders.live.on')" />
      <span v-else class="dot" :title="t('orders.live.polling')" />
    </h3>
    <p v-if="none" class="muted small">{{ t('orders.live.noJob') }}</p>
    <template v-else-if="job">
      <el-progress v-if="progress" :percentage="progress.percent" :status="status" :stroke-width="10" />
      <p class="small">
        <b>{{ progress ? progress.step : '—' }}</b>
        <span v-if="progress?.reason" class="muted"> · {{ progress.reason }}</span>
        <span class="muted"> · {{ t('procurement.job.state') }}: {{ stateLabel(job.state) }}</span>
        <span v-if="job.error_code" class="mono warn"> · {{ job.error_code }}</span>
      </p>
      <p class="small muted">
        <span v-if="job.account_mask">{{ job.account_mask }}</span>
        <span v-if="job.plan" class="mono"> · {{ job.plan }}</span>
        <span v-if="job.upstream_ref" class="mono"> · {{ t('procurement.job.upstream') }} {{ job.upstream_ref }}</span>
      </p>
      <p v-if="!recent.length" class="muted small">{{ t('procurement.job.supplierTimelineEmpty') }}</p>
      <ul v-else class="rows">
        <li v-for="(e, i) in recent" :key="i" class="small">
          <span class="mono muted">{{ when(e.at) }}</span>
          <span class="mono"> {{ [e.status, e.stage, e.payment_state, e.funding_state, e.code].filter(Boolean).join(' · ') || '—' }}</span>
          <span v-if="e.msg" class="muted"> · {{ e.msg }}</span>
        </li>
      </ul>
      <p v-if="updatedAt" class="muted tiny">{{ t('orders.live.updated', { at: when(new Date(updatedAt).toISOString()) }) }}</p>
    </template>
    <el-skeleton v-else :rows="2" animated />
  </section>
</template>

<style scoped>
.live {
  margin: 12px 0;
}
.h3 {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--el-color-warning);
}
.dot.on {
  background: var(--el-color-success);
}
.rows {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
}
.rows li {
  padding: 2px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.warn {
  color: var(--el-color-danger);
}
.tiny {
  font-size: 11px;
}
</style>
