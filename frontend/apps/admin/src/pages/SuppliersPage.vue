<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Plus } from '@element-plus/icons-vue'
import { formatDateTime, isApiError } from '@24goods/shared'
import { client } from '../api'
import {
  createSuppliersApi,
  SUPPLIER_AMOUNT_RE,
  supplierCodeOk,
  type AdminSupplier,
  type ProbeReport,
  type SupplierAdapter,
  type SupplierEnv,
  type SupplierHealth,
  type SupplierInput,
} from '../api-suppliers'
import { confirmBox, toastError, toastOk } from '../ui'

const api = createSuppliersApi(client)
const { t, locale } = useI18n()

const items = ref<AdminSupplier[]>([])
const adapters = ref<SupplierAdapter[]>(['zovocard', 'zzshu', 'manual'])
const productionLocked = ref(true)
const loading = ref(false)

const dialog = ref(false)
const editing = ref<AdminSupplier | null>(null)
const saving = ref(false)
const errors = ref<Record<string, string>>({})
const emptyForm = (): SupplierInput => ({
  code: 'zovocard',
  adapter: 'zovocard',
  label: '',
  env: 'sandbox',
  priority: 100,
  min_spendable: '0',
  enabled: false,
})
const f = ref<SupplierInput>(emptyForm())
const password = ref('')
/** the server asks for the admin password when a supplier becomes a production one */
const needsPassword = computed(
  () => f.value.env === 'production' && (editing.value?.env !== 'production' || (f.value.enabled && !editing.value.enabled)),
)

const probing = ref<string | null>(null)
const probeOpen = ref(false)
const probeOf = ref<AdminSupplier | null>(null)
const report = ref<ProbeReport | null>(null)

async function load() {
  loading.value = true
  try {
    const r = await api.list()
    items.value = r.items
    adapters.value = r.adapters
    productionLocked.value = r.production_locked
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}
onMounted(load)

function openDialog(row: AdminSupplier | null) {
  editing.value = row
  f.value = row
    ? {
        code: row.code,
        adapter: row.adapter,
        label: row.label,
        env: row.env,
        priority: row.priority,
        min_spendable: row.min_spendable,
        enabled: row.enabled,
      }
    : emptyForm()
  errors.value = {}
  password.value = ''
  dialog.value = true
}

function validate(): boolean {
  const e: Record<string, string> = {}
  if (!editing.value && !supplierCodeOk(f.value.code, f.value.adapter)) e.code = t('suppliers.err.code')
  if (!f.value.label.trim()) e.label = t('suppliers.err.label')
  if (!SUPPLIER_AMOUNT_RE.test(f.value.min_spendable.trim())) e.min_spendable = t('suppliers.err.amount')
  if (needsPassword.value && !password.value) e.password = t('suppliers.err.password')
  errors.value = e
  return Object.keys(e).length === 0
}

function explain(e: unknown) {
  if (isApiError(e) && e.code === 'supplier_code') errors.value = { code: t('suppliers.err.code') }
  else if (isApiError(e) && e.code === 'production_locked') errors.value = { enabled: t('suppliers.err.productionLocked') }
  else if (isApiError(e) && e.status === 403 && needsPassword.value) errors.value = { password: t('suppliers.err.passwordWrong') }
  else if (isApiError(e) && e.status === 409) errors.value = { code: t('suppliers.err.taken') }
  else toastError(e)
}

async function save() {
  if (saving.value || !validate()) return
  saving.value = true
  const body = { ...f.value, label: f.value.label.trim(), min_spendable: f.value.min_spendable.trim() }
  const pw = needsPassword.value ? { current_password: password.value } : {}
  try {
    if (editing.value) {
      const { label, env, priority, min_spendable, enabled } = body
      await api.update(editing.value.id, { label, env, priority, min_spendable, enabled, ...pw })
    } else {
      await api.create({ ...body, ...pw })
    }
    password.value = ''
    toastOk(t('suppliers.saved'))
    dialog.value = false
    void load()
  } catch (e) {
    explain(e)
  } finally {
    saving.value = false
  }
}

async function toggle(row: AdminSupplier, v: boolean) {
  if (v && row.env === 'production') {
    // switching a production supplier on needs the admin password: ask for it in the dialog
    openDialog(row)
    f.value.enabled = true
    return
  }
  try {
    Object.assign(row, await api.update(row.id, { enabled: v }))
  } catch (e) {
    if (isApiError(e) && e.code === 'production_locked') ElMessage.warning({ message: t('suppliers.err.productionLocked'), grouping: true })
    else toastError(e)
  }
}

async function resetBreaker(row: AdminSupplier) {
  if (!(await confirmBox(t('suppliers.breaker.confirm')))) return
  try {
    Object.assign(row, await api.resetBreaker(row.id))
    toastOk(t('suppliers.breaker.reset'))
  } catch (e) {
    toastError(e)
  }
}

async function probe(row: AdminSupplier) {
  if (probing.value) return
  probing.value = row.id
  try {
    const r = await api.probe(row.id)
    report.value = r.report
    probeOf.value = r.supplier ?? row
    if (r.supplier) Object.assign(row, r.supplier)
    probeOpen.value = true
  } catch (e) {
    if (isApiError(e) && ['procurement_off', 'procurement_unreachable', 'procurement_error'].includes(e.code))
      ElMessage.error({ message: t('suppliers.err.procurement'), grouping: true })
    else toastError(e)
  } finally {
    probing.value = null
  }
}

const healthType = (h: SupplierHealth) => (h === 'ok' ? 'success' : h === 'degraded' ? 'warning' : h === 'down' ? 'danger' : 'info')
const envType = (e: SupplierEnv) => (e === 'production' ? 'danger' : 'info')
const when = (v: string | null) => (v ? formatDateTime(v, locale.value) : '—')
const money = (v: string | null, cur: string | null) => (v == null ? '—' : `${v} ${cur ?? ''}`.trim())
const yesNo = (v: boolean | null | undefined) => (v == null ? t('suppliers.probe.unknown') : v ? t('suppliers.probe.yes') : t('suppliers.probe.no'))
const permissionRows = computed(() => Object.entries(report.value?.permissions ?? {}))
const permLabel = (k: string) => (['direct', 'cards', 'cdk', 'manual'].includes(k) ? t(`suppliers.perm.${k}`) : k)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <span class="muted">{{ t('suppliers.intro') }}</span>
      <el-button type="primary" :icon="Plus" @click="openDialog(null)">{{ t('suppliers.new') }}</el-button>
    </div>
    <el-alert v-if="productionLocked" type="info" :closable="false" show-icon class="lock" :title="t('suppliers.lockNote')" />
    <div class="panel">
      <el-table v-loading="loading" :data="items" row-key="id" :empty-text="t('suppliers.empty')">
        <el-table-column :label="t('suppliers.col.supplier')" min-width="200">
          <template #default="{ row }">
            <div class="name">{{ row.label }}</div>
            <div class="muted small"><span class="mono">{{ row.code }}</span> · {{ t(`suppliers.adapter.${row.adapter}`) }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="t('suppliers.col.env')" width="120">
          <template #default="{ row }"><el-tag size="small" :type="envType(row.env)" effect="plain">{{ t(`suppliers.env.${row.env}`) }}</el-tag></template>
        </el-table-column>
        <el-table-column :label="t('suppliers.col.health')" width="170">
          <template #default="{ row }">
            <el-tag size="small" :type="healthType(row.health)">{{ t(`suppliers.health.${row.health}`) }}</el-tag>
            <div v-if="row.health_detail" class="muted small mono">{{ row.health_detail }}</div>
            <div v-if="row.breaker_open" class="breaker small">
              {{ t(`suppliers.breaker.${row.breaker_kind === 'balance' ? 'balance' : 'hard'}`) }}
              <span v-if="row.breaker_reason" class="mono">{{ row.breaker_reason }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('suppliers.col.balance')" width="170">
          <template #default="{ row }">
            <span class="num">{{ money(row.last_balance, row.currency) }}</span>
            <div class="muted small">{{ when(row.last_balance_at) }}</div>
            <div v-if="row.adapter === 'zzshu' && row.points != null" class="muted small">
              {{ t('suppliers.points', { n: row.points, at: when(row.points_at ?? null) }) }}
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('suppliers.col.threshold')" width="130">
          <template #default="{ row }"><span class="num">{{ money(row.min_spendable, row.currency) }}</span></template>
        </el-table-column>
        <el-table-column :label="t('suppliers.col.priority')" prop="priority" width="90" />
        <el-table-column :label="t('suppliers.col.enabled')" width="100">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" :aria-label="t('suppliers.col.enabled')" @change="(v: string | number | boolean) => toggle(row as AdminSupplier, !!v)" />
          </template>
        </el-table-column>
        <el-table-column :label="t('common.actions')" width="300" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" :loading="probing === row.id" :disabled="!!probing && probing !== row.id" @click="probe(row as AdminSupplier)">{{ t('suppliers.test') }}</el-button>
            <el-button link type="primary" @click="openDialog(row as AdminSupplier)">{{ t('common.edit') }}</el-button>
            <el-button v-if="row.breaker_open && row.breaker_kind !== 'balance'" link type="danger" @click="resetBreaker(row as AdminSupplier)">{{ t('suppliers.breaker.resetBtn') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialog" :title="editing ? t('suppliers.edit') : t('suppliers.new')" width="min(560px, 96vw)" destroy-on-close>
      <el-form label-position="top" @submit.prevent="save">
        <div class="grid">
          <el-form-item :label="t('suppliers.f.code')" :error="errors.code">
            <el-input v-model="f.code" maxlength="32" class="mono" spellcheck="false" :disabled="!!editing" />
            <div class="muted small">{{ t('suppliers.f.codeHint') }}</div>
          </el-form-item>
          <el-form-item :label="t('suppliers.f.adapter')">
            <el-select v-model="f.adapter" :disabled="!!editing" style="width: 100%">
              <el-option v-for="a in adapters" :key="a" :value="a" :label="t(`suppliers.adapter.${a}`)" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item :label="t('suppliers.f.label')" :error="errors.label"><el-input v-model="f.label" maxlength="80" /></el-form-item>
        <div class="grid">
          <el-form-item :label="t('suppliers.f.env')">
            <el-radio-group v-model="f.env">
              <el-radio-button value="sandbox">{{ t('suppliers.env.sandbox') }}</el-radio-button>
              <el-radio-button value="production">{{ t('suppliers.env.production') }}</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item :label="t('suppliers.f.priority')"><el-input-number v-model="f.priority" :min="0" :max="10000" /></el-form-item>
          <el-form-item :label="t('suppliers.f.minSpendable')" :error="errors.min_spendable">
            <el-input v-model="f.min_spendable" inputmode="decimal"><template #suffix>USD</template></el-input>
            <div class="muted small">{{ t('suppliers.f.minSpendableHint') }}</div>
          </el-form-item>
          <el-form-item :label="t('suppliers.f.enabled')" :error="errors.enabled"><el-switch v-model="f.enabled" /></el-form-item>
        </div>
        <el-form-item v-if="needsPassword" :label="t('suppliers.f.password')" :error="errors.password">
          <el-input v-model="password" type="password" show-password autocomplete="current-password" maxlength="128" />
          <div class="muted small">{{ t('suppliers.f.passwordHint') }}</div>
        </el-form-item>
        <div class="muted small">{{ t('suppliers.f.secretsNote') }}</div>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="probeOpen" :title="t('suppliers.probe.title', { name: probeOf?.label ?? '' })" width="min(520px, 96vw)">
      <template v-if="report">
        <el-alert :type="report.ok ? 'success' : 'error'" :closable="false" show-icon :title="report.ok ? t('suppliers.probe.ok') : t('suppliers.probe.failed')" />
        <el-descriptions :column="1" border size="small" class="report">
          <el-descriptions-item :label="t('suppliers.probe.env')">{{ t(`suppliers.env.${report.env}`) }}</el-descriptions-item>
          <el-descriptions-item :label="t('suppliers.probe.auth')">{{ yesNo(report.auth_ok) }}</el-descriptions-item>
          <el-descriptions-item :label="t('suppliers.probe.ip')">{{ yesNo(report.ip_allowed) }}</el-descriptions-item>
          <el-descriptions-item v-for="[k, v] in permissionRows" :key="k" :label="permLabel(k)">{{ yesNo(v) }}</el-descriptions-item>
          <el-descriptions-item :label="t('suppliers.probe.balance')"><span class="num">{{ money(report.spendable_balance, report.currency) }}</span></el-descriptions-item>
          <el-descriptions-item :label="t('suppliers.probe.plans')"><span class="num">{{ report.purchasable_plans ?? '—' }}</span></el-descriptions-item>
          <el-descriptions-item :label="t('suppliers.probe.cards')"><span class="num">{{ report.card_products ?? '—' }}</span></el-descriptions-item>
          <el-descriptions-item :label="t('suppliers.probe.checked')">{{ when(report.checked_at) }}</el-descriptions-item>
        </el-descriptions>
        <div v-if="report.errors.length" class="errors">
          <div class="muted small">{{ t('suppliers.probe.errors') }}</div>
          <div v-for="e in report.errors" :key="e" class="mono small">{{ e }}</div>
        </div>
        <p class="muted small">{{ t('suppliers.probe.readOnly') }}</p>
      </template>
      <template #footer><el-button type="primary" @click="probeOpen = false">{{ t('common.ok') }}</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  column-gap: 16px;
}
.small {
  font-size: 12px;
}
.name {
  font-weight: 600;
}
.num {
  font-variant-numeric: tabular-nums;
}
.breaker {
  color: var(--el-color-danger);
  margin-top: 2px;
}
.lock {
  margin-bottom: 12px;
}
.report {
  margin-top: 12px;
}
.errors {
  margin-top: 12px;
  padding-top: 8px;
  border-top: 1px solid var(--a-line);
}
</style>
