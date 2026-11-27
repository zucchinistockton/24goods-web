<script setup lang="ts">
/**
 * ADR-057: staff settings of an SMS-code product: supplier, on/off, the ordered routes
 * (country id + max price), the limits, the live status and the 7-day route stats.
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { errorExtra, errorI18nKey, formatDateTime } from '@24goods/shared'
import { client } from '../api'
import { createSmsApi, type SmsConfig, type SmsParams, type SmsStats } from '../api-sms'
import { addRoute, countryHint, firstBadField, moveRoute, percent, removeRoute } from '../lib/smsForm'

const props = defineProps<{ modelValue: boolean; productId: string }>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()
const { t, te, locale } = useI18n()
const api = createSmsApi(client)

const cfg = ref<SmsConfig | null>(null)
const supplierId = ref('')
const enabled = ref(false)
const params = ref<SmsParams | null>(null)
const stats = ref<SmsStats | null>(null)
const loading = ref(false)
const saving = ref(false)
const bad = ref<string | null>(null)

const LIMITS = [
  'max_attempts', 'tries_per_route', 'window_minutes', 'number_wait_s', 'min_swap_s', 'start_hours',
  'daily_cap', 'per_buyer_daily', 'cool_no_numbers_min', 'cool_no_code_streak', 'cool_no_code_min',
] as const

function fail(e: unknown) {
  const field = errorExtra(e).field
  const base = t(errorI18nKey(e))
  ElMessage.error(typeof field === 'string' ? `${base}: ${field}` : base)
}

async function load() {
  loading.value = true
  try {
    const c = await api.config(props.productId)
    cfg.value = c
    supplierId.value = c.supplier_id ?? c.suppliers.find((s) => s.enabled)?.id ?? ''
    enabled.value = c.enabled
    params.value = JSON.parse(JSON.stringify(c.params)) as SmsParams
    stats.value = await api.stats(7).catch(() => null)
  } catch (e) {
    fail(e)
  } finally {
    loading.value = false
  }
}
watch(() => [props.modelValue, props.productId] as const, ([open]) => open && void load(), { immediate: true })

const p = computed(() => params.value)
async function save() {
  if (!params.value || saving.value) return
  bad.value = firstBadField(params.value)
  if (bad.value) return
  if (!supplierId.value) {
    bad.value = 'supplier'
    return
  }
  saving.value = true
  try {
    const c = await api.save(props.productId, { supplier_id: supplierId.value, enabled: enabled.value, params: params.value })
    cfg.value = c
    ElMessage.success(t('common.saved'))
  } catch (e) {
    fail(e)
  } finally {
    saving.value = false
  }
}
const statusText = computed(() => {
  const s = cfg.value?.status
  if (!s) return ''
  if (s.ok) return t('sms.statusOk', { n: s.stock ?? '–' })
  return s.reason && te(`sms.reason.${s.reason}`) ? t(`sms.reason.${s.reason}`) : (s.reason ?? t('sms.statusBad'))
})
const close = () => emit('update:modelValue', false)
</script>

<template>
  <el-dialog :model-value="modelValue" :title="t('sms.title')" width="min(760px, 96vw)" append-to-body @update:model-value="emit('update:modelValue', $event)" @closed="close">
    <div v-loading="loading" data-test="sms-settings">
      <template v-if="cfg && p">
        <el-alert
          v-if="cfg.status"
          :type="cfg.status.ok ? 'success' : 'warning'"
          :closable="false"
          show-icon
          :title="statusText"
          :description="cfg.status.checked_at ? t('sms.checkedAt', { at: formatDateTime(cfg.status.checked_at, locale) }) : undefined"
          data-test="sms-status"
        />
        <el-form label-position="top" class="sms-form" @submit.prevent="save">
          <el-form-item :label="t('sms.supplier')" :error="bad === 'supplier' ? t('sms.pickSupplier') : ''">
            <el-select v-model="supplierId" :placeholder="t('sms.pickSupplier')">
              <el-option v-for="s in cfg.suppliers" :key="s.id" :value="s.id" :label="`${s.code} (${s.env})${s.enabled ? '' : ' - ' + t('sms.disabled')}`" />
            </el-select>
          </el-form-item>
          <el-form-item :label="t('sms.enabled')">
            <el-switch v-model="enabled" data-test="sms-enabled" />
          </el-form-item>

          <el-form-item :label="t('sms.routes')" :error="bad?.startsWith('routes') ? t('sms.badRoutes') : ''">
            <div class="routes">
              <div v-for="(r, i) in p.routes" :key="i" class="route" data-test="sms-route">
                <span class="muted">{{ i + 1 }}</span>
                <el-input-number v-model="r.country" :min="1" :controls="false" :aria-label="t('sms.country')" class="num" />
                <el-input-number v-model="r.max_price" :min="0" :step="0.05" :precision="2" :aria-label="t('sms.maxPrice')" class="num" />
                <span class="muted small hint">{{ countryHint(r.country) }}</span>
                <el-button :disabled="i === 0" size="small" @click="p.routes = moveRoute(p.routes, i, -1)">↑</el-button>
                <el-button :disabled="i === p.routes.length - 1" size="small" @click="p.routes = moveRoute(p.routes, i, 1)">↓</el-button>
                <el-button size="small" type="danger" plain @click="p.routes = removeRoute(p.routes, i)">{{ t('common.delete') }}</el-button>
              </div>
              <el-button size="small" data-test="sms-add-route" @click="p.routes = addRoute(p.routes)">{{ t('sms.addRoute') }}</el-button>
              <div class="muted small">{{ t('sms.routesHint') }}</div>
            </div>
          </el-form-item>

          <div class="limits">
            <el-form-item v-for="k in LIMITS" :key="k" :label="t(`sms.limit.${k}`)" :error="bad === k ? t('sms.badNumber') : ''">
              <el-input-number v-model="p[k]" :min="0" :controls="false" :data-test="`sms-${k}`" />
            </el-form-item>
          </div>
          <el-button type="primary" :loading="saving" native-type="submit" data-test="sms-save">{{ t('common.save') }}</el-button>
        </el-form>

        <h3 class="h3">{{ t('sms.statsTitle', { n: stats?.days ?? 7 }) }}</h3>
        <template v-if="stats">
          <p v-if="stats.sessions" class="muted small" data-test="sms-success">
            {{ t('sms.successRate', { rate: percent(stats.sessions.success_rate), ok: stats.sessions.delivered, bad: stats.sessions.failed }) }}
          </p>
          <el-table :data="stats.routes" size="small" :empty-text="t('common.empty')" data-test="sms-stats">
            <el-table-column prop="country" :label="t('sms.country')" min-width="90" />
            <el-table-column prop="numbers" :label="t('sms.numbers')" width="80" />
            <el-table-column prop="codes" :label="t('sms.codes')" width="70" />
            <el-table-column :label="t('sms.codeRate')" width="90">
              <template #default="{ row }">{{ percent(row.code_rate) }}</template>
            </el-table-column>
            <el-table-column :label="t('sms.median')" width="90">
              <template #default="{ row }">{{ row.median_seconds ?? '–' }}</template>
            </el-table-column>
            <el-table-column :label="t('sms.costPerCode')" width="110">
              <template #default="{ row }">{{ row.cost_per_code_usd ?? '–' }}</template>
            </el-table-column>
          </el-table>
        </template>
      </template>
    </div>
  </el-dialog>
</template>

<style scoped>
.sms-form {
  margin-top: 12px;
}
.routes {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}
.route {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.num {
  width: 110px;
}
.hint {
  min-width: 70px;
}
.limits {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 0 16px;
}
.h3 {
  margin: 20px 0 8px;
}
</style>
