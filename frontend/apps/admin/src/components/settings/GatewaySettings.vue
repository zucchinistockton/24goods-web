<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { client } from '../../api'
import { useSave } from '../../composables/useSave'
import { useRead } from '../../composables/useRead'
import { toastError } from '../../ui'
import { formatMoney, type GatewayRoutingMode, type PaymentGatewayAdmin, type PaymentGatewayList, type TestPaymentView } from '@24goods/shared'
import { minorToYuan, yuanToMinor } from '../../lib/money'
import { byPriority, moveId, tryOrder } from '../../lib/gatewayRouting'
import GatewayTestDialog from './GatewayTestDialog.vue'
type Gateway = PaymentGatewayAdmin
const { t, locale } = useI18n()
const { saving, run } = useSave()
const reader=useRead()
const items = ref<Gateway[]>([])
const routingMode = ref<GatewayRoutingMode>('sticky')
const candidates = ref<string[]>([])
const testGateway = ref<Gateway | null>(null)
const recent = ref<TestPaymentView[]>([])
const editing = ref(false)
const activating = ref<Gateway | null>(null)
const password = ref('')
const ownership = ref(false)
const form = reactive({ label: '', gateway: 'https://pay.xiaodouap.cn/submit.php', merchant_id: '', secret: '', mode: 'qr' as 'qr' | 'redirect', current_password: '' })
type Health = { status: 'healthy' | 'degraded' | 'unreachable'; last_test: { ok: boolean; at: string; latency_ms: number | null } | null }
const health = ref<Record<string, Health>>({})
const usdt = ref<Health | null>(null)
const testing = ref('')
const testMsg = ref<Record<string, string>>({})
async function loadHealth() {
  try {
    const r = await client.get<{ gateways: Record<string, Health>; bepusdt: Health | null }>('/admin/payment-gateways-health')
    health.value = r.gateways
    usdt.value = r.bepusdt
  } catch { /* advisory */ }
}
async function loadRecent() {
  try { recent.value = (await client.get<{ items: TestPaymentView[] }>('/admin/payment-gateways/test-payments')).items } catch { /* advisory */ }
}
async function load() {
  const r = await reader.run(() => client.get<PaymentGatewayList>('/admin/payment-gateways'))
  if (r) { items.value = byPriority(r.items); routingMode.value = r.routing_mode; candidates.value = r.candidates }
  await Promise.all([loadHealth(), loadRecent()])
}
async function setMode(mode: GatewayRoutingMode) {
  if (mode === routingMode.value) return
  const r = await run(() => client.put('/admin/payment-gateways/routing', { mode }))
  if (r) await load()
}
async function setEnabled(row: Gateway, enabled: boolean) {
  const r = await run(() => client.patch(`/admin/payment-gateways/${row.id}`, { enabled }))
  await load()
  return r
}
async function move(row: Gateway, dir: -1 | 1) {
  const ids = items.value.map((g) => g.id)
  const next = moveId(ids, row.id, dir)
  if (next === ids) return
  const r = await run(() => client.put('/admin/payment-gateways/order', { ids: next }))
  if (r) await load()
}
async function remove(row: Gateway) {
  if (!window.confirm(t('gwRouting.deleteConfirm', { label: row.label }))) return
  try { await client.delete(`/admin/payment-gateways/${row.id}`); await load() } catch (e) { toastError(e) }
}
const cny = (minor: number | null) => (minor === null ? t('gwDeposit.none') : formatMoney(minorToYuan(minor), 'CNY', locale.value))
const depositFor = ref<Gateway | null>(null)
const dep = reactive({ paid: '', pct: '', floor: '' })
const depErr = reactive({ paid: '', pct: '', floor: '' })
function openDeposit(row: Gateway) {
  depositFor.value = row
  dep.paid = row.deposit_minor === null ? '' : minorToYuan(row.deposit_minor)
  dep.pct = row.deposit_pct === null ? '' : String(row.deposit_pct)
  dep.floor = row.deposit_floor_minor === null ? '' : minorToYuan(row.deposit_floor_minor)
  depErr.paid = depErr.pct = depErr.floor = ''
}
function shortBy(row: Gateway): number {
  return row.required_minor !== null && row.deposit_minor !== null && row.required_minor > row.deposit_minor ? row.required_minor - row.deposit_minor : 0
}
async function saveDeposit() {
  const row = depositFor.value
  if (!row) return
  const money = (s: string) => (s.trim() === '' ? null : yuanToMinor(s))
  const pctRaw = dep.pct.trim()
  const pct = pctRaw === '' ? null : /^\d{1,3}$/.test(pctRaw) ? Number(pctRaw) : NaN
  depErr.paid = money(dep.paid) === null && dep.paid.trim() !== '' ? t('gwDeposit.invalidMoney') : ''
  depErr.floor = money(dep.floor) === null && dep.floor.trim() !== '' ? t('gwDeposit.invalidMoney') : ''
  depErr.pct = pct !== null && !(pct >= 1 && pct <= 100) ? t('gwDeposit.invalidPct') : ''
  if (depErr.paid || depErr.floor || depErr.pct) return
  const r = await run(() => client.patch(`/admin/payment-gateways/${row.id}`, {
    deposit_cny: dep.paid.trim() === '' ? null : dep.paid.trim(),
    deposit_pct: pct,
    deposit_floor_cny: dep.floor.trim() === '' ? null : dep.floor.trim(),
  }))
  if (r) { depositFor.value = null; await load() }
}
function when(iso: string | null) { return iso ? new Date(iso).toLocaleString() : t('gwRouting.never') }
onMounted(load)
interface TestResult { ok: boolean; reachable: boolean; latency_ms?: number; message?: string; route?: 'relay' | 'direct' }
async function runTest(id: string) {
  testing.value = id
  try {
    const r = await client.post<TestResult>(`/admin/payment-gateways/${encodeURIComponent(id)}/test`, {})
    const route = r.route ? t(r.route === 'relay' ? 'gateways.routeRelay' : 'gateways.routeDirect') : ''
    testMsg.value[id] = r.ok
      ? t('gateways.tested', { route, ms: r.latency_ms ?? 0, msg: r.message ?? '' })
      : route ? t('gateways.testFailedVia', { route }) : t('gateways.testFailed')
    await loadHealth()
  } catch (e) { toastError(e) } finally { testing.value = '' }
}
async function create() {
  const r = await run(() => client.post('/admin/payment-gateways', { ...form }))
  // Clear authentication material on every submission, including errors.
  form.secret = ''; form.current_password = ''
  if (r) { editing.value = false; await load() }
}
function activate(row: Gateway) { activating.value = row; password.value = ''; ownership.value = false }
async function confirmActivate() {
  if (!activating.value) return
  const r = await run(() => client.post(`/admin/payment-gateways/${activating.value!.id}/activate`, { current_password: password.value, ownership_confirmed: ownership.value }))
  password.value = ''
  if (r) { activating.value = null; await load() }
}
function close() { form.secret = ''; form.current_password = ''; editing.value = false }
</script>
<template>
  <section class="panel block">
    <h2 class="h2">{{ t('gateways.title') }}</h2>
    <p class="muted small">{{ t('gateways.note') }}</p>
    <el-alert v-if="items.some(row => Date.now() - Date.parse(row.created_at) > 90*86400000)" :title="t('gateways.review')" type="warning" :closable="false" />
    <el-alert :title="t('gateways.privacy')" type="info" :closable="false" show-icon />
    <div class="routing">
      <strong>{{ t('gwRouting.mode') }}</strong>
      <el-radio-group :model-value="routingMode" :disabled="saving" @change="(v: string | number | boolean | undefined) => setMode(v as GatewayRoutingMode)">
        <el-radio-button value="sticky">{{ t('gwRouting.sticky') }}</el-radio-button>
        <el-radio-button value="priority">{{ t('gwRouting.fixed') }}</el-radio-button>
      </el-radio-group>
      <p class="muted small">{{ routingMode === 'sticky' ? t('gwRouting.stickyHint') : t('gwRouting.fixedHint') }}</p>
    </div>
    <el-table :data="items" row-key="id" class="gateways">
      <el-table-column prop="label" :label="t('gateways.label')" />
      <el-table-column prop="gateway" :label="t('gateways.url')" min-width="200" />
      <el-table-column :label="t('gateways.pid')"><template #default="{ row }">••{{ row.merchant_id.slice(-4) }}</template></el-table-column>
      <el-table-column :label="t('gwRouting.priority')" min-width="150"><template #default="{ row, $index }">
        <span class="muted small">{{ row.priority }}</span>
        <el-button size="small" :disabled="saving || $index === 0" :aria-label="t('gwRouting.up')" @click="move(row as Gateway, -1)">↑</el-button>
        <el-button size="small" :disabled="saving || $index === items.length - 1" :aria-label="t('gwRouting.down')" @click="move(row as Gateway, 1)">↓</el-button>
        <el-tag v-if="tryOrder(candidates, row.id)" size="small" :type="row.in_use ? 'success' : 'info'" effect="plain">{{ row.in_use ? t('gwRouting.inUse') : t('gwRouting.tryOrder', { n: tryOrder(candidates, row.id) }) }}</el-tag>
      </template></el-table-column>
      <el-table-column :label="t('gwRouting.enabled')" width="90"><template #default="{ row }">
        <el-switch :model-value="row.enabled" :disabled="saving" :aria-label="t('gwRouting.enabled')" @change="(v: boolean | string | number) => setEnabled(row as Gateway, !!v)" />
      </template></el-table-column>
      <el-table-column :label="t('gwRouting.lastOk')" min-width="150"><template #default="{ row }">
        <div class="small">{{ when(row.last_ok_at) }}</div>
        <div v-if="row.last_fail_at" class="small fail">{{ t('gwRouting.lastFail') }}: {{ when(row.last_fail_at) }} <span v-if="row.last_fail_reason">({{ t(`gwRouting.reasons.${row.last_fail_reason}`) }})</span></div>
      </template></el-table-column>
      <el-table-column :label="t('gwDeposit.button')" min-width="230"><template #default="{ row }">
        <div class="small">{{ row.required_minor === null ? t('gwDeposit.summaryNoDue', { max: cny(row.max30_minor) }) : t('gwDeposit.summary', { max: cny(row.max30_minor), due: cny(row.required_minor), cap: cny(row.cap_minor) }) }}</div>
        <el-tag v-if="shortBy(row as Gateway) > 0" size="small" type="warning" effect="plain">{{ t('gwDeposit.short', { short: cny(shortBy(row as Gateway)) }) }}</el-tag>
        <el-button size="small" :disabled="saving" @click="openDeposit(row as Gateway)">{{ t('gwDeposit.button') }}</el-button>
      </template></el-table-column>
      <el-table-column :label="t('gateways.health.title')" min-width="140"><template #default="{ row }">
        <el-tag :type="health[row.id]?.status === 'healthy' ? 'success' : health[row.id]?.status === 'degraded' ? 'warning' : health[row.id]?.status === 'unreachable' ? 'danger' : 'info'" effect="plain">{{ t(`gateways.health.${health[row.id]?.status ?? 'unknown'}`) }}</el-tag>
        <div v-if="testMsg[row.id]" class="muted small">{{ testMsg[row.id] }}</div>
      </template></el-table-column>
      <el-table-column :label="t('gateways.actions')" min-width="260"><template #default="{ row }">
        <el-button size="small" :loading="testing === row.id" @click="runTest(row.id)">{{ t('gateways.test') }}</el-button>
        <el-button size="small" type="primary" plain @click="testGateway = row as Gateway">{{ t('gwRouting.testPay') }}</el-button>
        <el-button size="small" type="danger" plain :disabled="saving" @click="remove(row as Gateway)">{{ t('gwRouting.delete') }}</el-button>
        <el-button v-if="!row.active" size="small" :disabled="saving" @click="activate(row as Gateway)">{{ t('gateways.activate') }}</el-button>
      </template></el-table-column>
    </el-table>
    <p class="muted small">{{ t('gateways.testNote') }}</p>
    <p v-if="usdt" class="muted small">{{ t('gateways.usdtTitle') }}:
      <el-tag size="small" :type="usdt.status === 'healthy' ? 'success' : 'danger'" effect="plain">{{ t(`gateways.health.${usdt.status}`) }}</el-tag>
      <el-button size="small" :loading="testing === 'bepusdt'" @click="runTest('bepusdt')">{{ t('gateways.test') }}</el-button>
    </p>
    <el-button @click="editing = true">{{ t('gateways.add') }}</el-button>
    <GatewayTestDialog :gateway-id="testGateway?.id ?? null" :label="testGateway?.label ?? ''" @close="testGateway = null" @done="loadRecent(); load()" />
    <template v-if="recent.length">
      <h3 class="h3">{{ t('gwRouting.recent') }}</h3>
      <el-table :data="recent" size="small" row-key="id">
        <el-table-column prop="gateway_label" :label="t('gwRouting.gateway')" />
        <el-table-column :label="t('gwRouting.amount')"><template #default="{ row }">¥{{ row.amount }}</template></el-table-column>
        <el-table-column :label="t('gateways.status')"><template #default="{ row }">{{ t(`gwRouting.status.${row.status}`) }}</template></el-table-column>
        <el-table-column :label="t('gwRouting.at')" min-width="150"><template #default="{ row }">{{ when(row.created_at) }}</template></el-table-column>
      </el-table>
      <p class="muted small">{{ t('gwRouting.refundNote') }}</p>
    </template>
    <el-dialog :model-value="editing" :title="t('gateways.add')" width="min(92vw, 600px)" @close="close">
      <el-form label-position="top" @submit.prevent="create">
        <el-form-item :label="t('gateways.label')" required><el-input v-model="form.label" maxlength="80" /></el-form-item>
        <el-form-item :label="t('gateways.url')" required><el-input v-model="form.gateway" maxlength="500" /></el-form-item>
        <el-form-item :label="t('gateways.pid')" required><el-input v-model="form.merchant_id" maxlength="64" autocomplete="off" /></el-form-item>
        <el-form-item :label="t('gateways.key')" required><el-input v-model="form.secret" type="password" maxlength="256" autocomplete="new-password" /></el-form-item>
        <el-form-item :label="t('gateways.mode')"><el-select v-model="form.mode"><el-option value="qr" :label="t('gateways.qr')" /><el-option value="redirect" :label="t('gateways.redirect')" /></el-select></el-form-item>
        <el-form-item :label="t('gateways.password')" required><el-input v-model="form.current_password" type="password" autocomplete="current-password" /></el-form-item>
        <p>{{ t('gateways.rotation') }}</p>
        <el-button type="primary" native-type="submit" :loading="saving">{{ t('common.save') }}</el-button>
      </el-form>
    </el-dialog>
    <el-dialog :model-value="!!depositFor" :title="t('gwDeposit.title', { label: depositFor?.label ?? '' })" width="min(92vw, 520px)" @close="depositFor = null">
      <el-form label-position="top" @submit.prevent="saveDeposit">
        <p class="muted small">{{ t('gwDeposit.note') }}</p>
        <el-form-item :label="t('gwDeposit.paid')" :error="depErr.paid"><el-input v-model="dep.paid" inputmode="decimal" maxlength="12" /></el-form-item>
        <el-form-item :label="t('gwDeposit.pct')" :error="depErr.pct"><el-input v-model="dep.pct" inputmode="numeric" maxlength="3" /></el-form-item>
        <el-form-item :label="t('gwDeposit.floor')" :error="depErr.floor"><el-input v-model="dep.floor" inputmode="decimal" maxlength="12" /></el-form-item>
        <el-button type="primary" native-type="submit" :loading="saving">{{ t('common.save') }}</el-button>
      </el-form>
    </el-dialog>
    <el-dialog :model-value="!!activating" :title="t('gateways.activate')" width="min(92vw, 600px)" @close="activating = null; password = ''">
      <el-form label-position="top" @submit.prevent="confirmActivate">
        <p>{{ t('gateways.confirmNote') }}</p>
        <el-checkbox v-model="ownership">{{ t('gateways.ownership') }}</el-checkbox>
        <el-form-item :label="t('gateways.password')"><el-input v-model="password" type="password" autocomplete="current-password" /></el-form-item>
        <el-button type="primary" native-type="submit" :disabled="!ownership || !password" :loading="saving">{{ t('gateways.activate') }}</el-button>
      </el-form>
    </el-dialog>
  </section>
</template>
<style scoped>.gateways { margin: 16px 0 } .routing { margin: 12px 0 } .fail { color: var(--el-color-danger) }</style>
