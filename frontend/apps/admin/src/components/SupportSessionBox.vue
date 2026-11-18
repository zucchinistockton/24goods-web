<script setup lang="ts">
/**
 * The buyer-consented session of a failing direct order (owner request 2026-11-18, review-v12
 * §26). Shown only after "Show session" with a written reason; every look is audited on the server.
 * The text lives in this component while the dialog is open and is dropped when it closes.
 */
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { copyText, formatDateTime } from '@24goods/shared'
import { client } from '../api'
import { toastError, toastOk } from '../ui'

interface ShareStatus {
  available: boolean
  consented: boolean
  account: string | null
  reason: string | null
  kept_since: string | null
  revealed_count: number
  last_revealed_at: string | null
}
interface Revealed {
  session: string
  account: string | null
  reason: string | null
  kept_since: string | null
}

const props = defineProps<{ orderId: string; version?: number }>()
const { t, locale } = useI18n()
const st = ref<ShareStatus | null>(null)
const dlg = ref(false)
const why = ref('')
const busy = ref(false)
const shown = ref<Revealed | null>(null)
const seg = (s: string) => encodeURIComponent(s)

async function load() {
  try {
    st.value = await client.get<ShareStatus>(`/admin/orders/${seg(props.orderId)}/support-session`)
  } catch {
    st.value = null
  }
}
watch(() => [props.orderId, props.version], load, { immediate: true })

async function reveal() {
  if (why.value.trim().length < 8 || busy.value) return
  busy.value = true
  try {
    shown.value = await client.post<Revealed>(`/admin/orders/${seg(props.orderId)}/support-session/reveal`, { reason: why.value.trim() })
    void load()
  } catch (e) {
    toastError(e)
  } finally {
    busy.value = false
  }
}
async function copy() {
  if (!shown.value) return
  if (await copyText(shown.value.session)) toastOk(t('common.copied'))
}
function closed() {
  shown.value = null // the session never outlives the dialog
  why.value = ''
}
const when = (iso: string | null) => formatDateTime(iso, locale.value)
</script>

<template>
  <section v-if="st && (st.available || st.consented)" class="box" data-testid="support-session">
    <h3 class="h3">{{ t('orders.shared.title') }}</h3>
    <p v-if="st.available" class="small">
      {{ t('orders.shared.kept', { since: when(st.kept_since) }) }}
      <span v-if="st.account" class="muted"> · {{ st.account }}</span>
      <span v-if="st.reason" class="mono muted"> · {{ st.reason }}</span>
      <span v-if="st.revealed_count" class="muted"> · {{ t('orders.shared.seen', { n: st.revealed_count, at: when(st.last_revealed_at) }) }}</span>
    </p>
    <p v-else class="small muted">{{ t('orders.shared.consentOnly') }}</p>
    <el-button v-if="st.available" size="small" type="warning" plain @click="dlg = true">{{ t('orders.shared.show') }}</el-button>

    <el-dialog v-model="dlg" :title="t('orders.shared.title')" width="min(560px, 96vw)" append-to-body destroy-on-close @closed="closed">
      <template v-if="!shown">
        <p class="small">{{ t('orders.shared.warn') }}</p>
        <el-input v-model="why" :placeholder="t('orders.shared.reason')" maxlength="200" show-word-limit />
        <div class="actions">
          <el-button @click="dlg = false">{{ t('common.cancel') }}</el-button>
          <el-button type="warning" :loading="busy" :disabled="why.trim().length < 8" @click="reveal">{{ t('orders.shared.show') }}</el-button>
        </div>
      </template>
      <template v-else>
        <p class="small muted">{{ shown.account }}</p>
        <el-input :model-value="shown.session" type="textarea" :rows="6" readonly class="mono" autocomplete="off" spellcheck="false" />
        <div class="actions">
          <el-button @click="copy">{{ t('common.copy') }}</el-button>
          <el-button type="primary" @click="dlg = false">{{ t('common.close') }}</el-button>
        </div>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.box {
  margin: 12px 0;
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}
</style>
