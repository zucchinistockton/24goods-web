<script setup lang="ts">
/** ADR-057: the SMS session and its number attempts of an order (read-only, numbers masked by the API). */
import { onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatDateTime } from '@24goods/shared'
import { client } from '../api'
import { createSmsApi, type SmsOrderInfo } from '../api-sms'

const props = defineProps<{ orderId: string; version?: number }>()
const { t, locale } = useI18n()
const api = createSmsApi(client)
const info = ref<SmsOrderInfo | null>(null)
async function load() {
  try {
    const d = await api.order(props.orderId)
    info.value = d
  } catch {
    info.value = null
  }
}
onMounted(load)
watch(() => [props.orderId, props.version], load)
const at = (s: string | null) => (s ? formatDateTime(s, locale.value) : '–')
</script>

<template>
  <div data-test="sms-order">
    <h3 class="h3">{{ t('sms.orderTitle') }}</h3>
    <template v-if="info">
      <p v-if="info.session" class="muted small">
        {{ t(`sms.sessionState.${info.session.state}`) }} · {{ t('sms.attempts', { n: info.session.attempts_used }) }}
        <template v-if="info.session.fail_reason"> · {{ info.session.fail_reason }}</template>
        · {{ t('sms.windowEnds', { at: at(info.session.window_ends_at) }) }}
      </p>
      <p v-else class="muted small">{{ t('sms.noSession') }}</p>
      <el-table v-if="info.attempts.length" :data="info.attempts" size="small">
        <el-table-column prop="seq" label="#" width="44" />
        <el-table-column prop="country" :label="t('sms.country')" width="80" />
        <el-table-column prop="phone" :label="t('sms.phone')" min-width="110" />
        <el-table-column prop="cost" :label="t('sms.cost')" width="70" />
        <el-table-column prop="state" :label="t('sms.state')" width="90" />
        <el-table-column :label="t('sms.codeAt')" min-width="110">
          <template #default="{ row }">{{ at(row.code_at) }}</template>
        </el-table-column>
      </el-table>
    </template>
  </div>
</template>
