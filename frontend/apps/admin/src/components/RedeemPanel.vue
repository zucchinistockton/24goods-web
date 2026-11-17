<script setup lang="ts">
/**
 * issue #31 (review-v12 §23): the redemption site's admin. Staff read the attempt log, look a code
 * up and see the stats; administrators change the site settings and disable / re-enable a code at
 * the vendor. A pasted code lives only in this component's memory for one request and is cleared
 * afterwards; the log shows masked codes only.
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatDateTime } from '@24goods/shared'
import { client } from '../api'
import {
  createRedeemApi,
  type RedeemAttempt,
  type RedeemAttemptFilter,
  type RedeemLookup,
  type RedeemOutcome,
  type RedeemSettings,
  type RedeemStats,
  type RedeemStep,
} from '../api-procurement'
import { usePaged } from '../composables/usePaged'
import { ratePct } from '../lib/cardPool'
import { LIMIT_STEPS, activeDays, barPercents, cdkActions, codeValid, outcomeTag, searchTerm, settingsErrors, settingsPatch, toForm, type SettingsForm } from '../lib/redeem'
import { useAuthStore } from '../stores/auth'
import { confirmBox, toastError, toastOk } from '../ui'

const api = createRedeemApi(client)
const auth = useAuthStore()
const { t, te, locale } = useI18n()
const sub = ref<'log' | 'lookup' | 'stats' | 'settings'>('log')
const when = (v: string | null) => (v ? formatDateTime(v, locale.value) : '—')
const word = (ns: string, k: string | null) => (k ? (te(`redeem.${ns}.${k}`) ? t(`redeem.${ns}.${k}`) : k) : '—')
const asAttempt = (row: unknown) => row as RedeemAttempt
function fail(e: unknown) {
  const code = (e as { code?: string })?.code
  if (code && te(`redeem.err.${code}`)) ElMessage.error({ message: t(`redeem.err.${code}`), grouping: true })
  else toastError(e)
}

// ------------------------------------------------------------------ log
const OUTCOMES: RedeemOutcome[] = ['ok', 'completed', 'failed', 'error', 'rate_limited', 'invalid', 'maintenance']
const STEPS: RedeemStep[] = ['preview', 'preflight', 'redeem', 'result']
const filter = reactive({ q: '', outcome: '' as RedeemOutcome | '', step: '' as RedeemStep | '', order_ref: '', range: null as [string, string] | null })
const retention = ref(90)
function activeFilter(): RedeemAttemptFilter {
  const f: RedeemAttemptFilter = {}
  if (searchTerm(filter.q)) f.q = searchTerm(filter.q)
  if (filter.outcome) f.outcome = filter.outcome
  if (filter.step) f.step = filter.step
  if (filter.order_ref.trim()) f.order_ref = filter.order_ref.trim()
  if (filter.range) [f.date_from, f.date_to] = filter.range
  return f
}
const log = usePaged<RedeemAttempt>(async (p, s) => {
  const r = await api.attempts(activeFilter(), p, s)
  retention.value = r.retention_days
  return r
})

// ------------------------------------------------------------------ lookup
const lookupCode = ref('')
const lookupOrder = ref('')
const looking = ref(false)
const found = ref<RedeemLookup | null>(null)
const lastCode = ref('') // kept only so a disable / enable can name the code again; cleared with the result
const acting = ref(false)
async function lookup() {
  const code = lookupCode.value.trim()
  const order = lookupOrder.value.trim()
  if (!!code === !!order || (code && !codeValid(code))) {
    ElMessage.error(t('redeem.err.code_or_order'))
    return
  }
  looking.value = true
  try {
    found.value = await api.lookup(code ? { code } : { order_ref: order })
    lastCode.value = code
  } catch (e) {
    found.value = null
    lastCode.value = ''
    fail(e)
  } finally {
    looking.value = false
    lookupCode.value = ''
  }
}
function clearLookup() {
  found.value = null
  lastCode.value = ''
  lookupCode.value = ''
  lookupOrder.value = ''
}
const actions = computed(() => (found.value?.vendor && found.value.vendor.available ? cdkActions(found.value.vendor.status) : { disable: false, enable: false }))
async function setCdk(action: 'disable' | 'enable') {
  if (!lastCode.value || !found.value) return
  const ok = await confirmBox(t(`redeem.lookup.confirm.${action}`, { code: found.value.code ?? '' }), t(`redeem.lookup.${action}`))
  if (!ok) return
  acting.value = true
  try {
    await api.setCdk(action, lastCode.value)
    toastOk(t('redeem.lookup.done'))
    const code = lastCode.value
    found.value = await api.lookup({ code })
  } catch (e) {
    fail(e)
  } finally {
    acting.value = false
  }
}
const vendorText = computed(() => {
  const v = found.value?.vendor
  if (!v) return ''
  if (!v.available) return t('redeem.lookup.vendorDown')
  return v.found ? word('cdkStatus', v.status) : t('redeem.lookup.vendorNone')
})

// ------------------------------------------------------------------ stats
const stats = ref<RedeemStats | null>(null)
const statsLoading = ref(false)
async function loadStats() {
  statsLoading.value = true
  try {
    stats.value = await api.stats(30)
  } catch (e) {
    stats.value = null
    fail(e)
  } finally {
    statsLoading.value = false
  }
}
const bars = computed(() => (stats.value ? barPercents(stats.value.days.map((d) => d.attempts)) : []))
const dayRows = computed(() => (stats.value ? activeDays(stats.value.days) : []))

// ------------------------------------------------------------------ settings (admins)
const settings = ref<RedeemSettings | null>(null)
const form = ref<SettingsForm | null>(null)
const loadedForm = ref<SettingsForm | null>(null)
const saving = ref(false)
const errors = computed(() => (settings.value && form.value ? settingsErrors(form.value, settings.value) : {}))
const dirty = computed(() => !!form.value && !!loadedForm.value && Object.keys(settingsPatch(loadedForm.value, form.value)).length > 0)
async function loadSettings() {
  try {
    settings.value = await api.settings()
    form.value = toForm(settings.value)
    loadedForm.value = toForm(settings.value)
  } catch (e) {
    settings.value = null
    fail(e)
  }
}
async function saveSettings() {
  if (!settings.value || !form.value || !loadedForm.value || Object.keys(errors.value).length) return
  const patch = settingsPatch(loadedForm.value, form.value)
  if (!Object.keys(patch).length) return
  if (patch.maintenance === true && !(await confirmBox(t('redeem.settings.confirmMaintenance')))) return
  saving.value = true
  try {
    settings.value = await api.saveSettings(patch)
    form.value = toForm(settings.value)
    loadedForm.value = toForm(settings.value)
    toastOk(t('redeem.settings.saved'))
  } catch (e) {
    fail(e)
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  void log.reload()
  void loadStats()
  if (auth.isAdmin) void loadSettings()
})
</script>

<template>
  <div class="redeem">
    <el-tabs v-model="sub" type="card">
      <!-- ============================================================ log -->
      <el-tab-pane name="log" :label="t('redeem.tab.log')">
        <p class="muted small">{{ t('redeem.log.help', { days: retention }) }}</p>
        <el-form inline class="filters" @submit.prevent="log.reload()">
          <el-form-item><el-input v-model="filter.q" :placeholder="t('redeem.log.searchCode')" clearable maxlength="40" style="width: 190px" @keyup.enter="log.reload()" /></el-form-item>
          <el-form-item><el-input v-model="filter.order_ref" :placeholder="t('redeem.log.orderRef')" clearable maxlength="16" style="width: 150px" @keyup.enter="log.reload()" /></el-form-item>
          <el-form-item>
            <el-select v-model="filter.outcome" clearable :placeholder="t('redeem.log.outcome')" style="width: 150px" @change="log.reload()">
              <el-option v-for="o in OUTCOMES" :key="o" :value="o" :label="word('outcome', o)" />
            </el-select>
          </el-form-item>
          <el-form-item>
            <el-select v-model="filter.step" clearable :placeholder="t('redeem.log.step')" style="width: 130px" @change="log.reload()">
              <el-option v-for="s in STEPS" :key="s" :value="s" :label="word('step', s)" />
            </el-select>
          </el-form-item>
          <el-form-item><el-date-picker v-model="filter.range" type="daterange" value-format="YYYY-MM-DD" :start-placeholder="t('redeem.log.from')" :end-placeholder="t('redeem.log.to')" style="width: 250px" @change="log.reload()" /></el-form-item>
          <el-form-item><el-button type="primary" native-type="submit" :loading="log.loading.value">{{ t('redeem.search') }}</el-button></el-form-item>
        </el-form>
        <el-table v-loading="log.loading.value" :data="log.items.value" row-key="id" :empty-text="t('redeem.log.empty')">
          <el-table-column :label="t('redeem.log.time')" width="170"><template #default="{ row }">{{ when(asAttempt(row).at) }}</template></el-table-column>
          <el-table-column :label="t('redeem.log.step')" width="110"><template #default="{ row }">{{ word('step', asAttempt(row).step) }}</template></el-table-column>
          <el-table-column :label="t('redeem.log.outcome')" width="130"><template #default="{ row }"><el-tag :type="outcomeTag(asAttempt(row).outcome)" size="small">{{ word('outcome', asAttempt(row).outcome) }}</el-tag></template></el-table-column>
          <el-table-column :label="t('redeem.log.reason')" min-width="150"><template #default="{ row }">{{ asAttempt(row).reason ? word('reason', asAttempt(row).reason) : '—' }}</template></el-table-column>
          <el-table-column :label="t('redeem.log.code')" width="150"><template #default="{ row }"><span class="mono">{{ asAttempt(row).code ?? '—' }}</span></template></el-table-column>
          <el-table-column :label="t('redeem.log.plan')" prop="plan" width="110" />
          <el-table-column :label="t('redeem.log.order')" width="140"><template #default="{ row }"><span class="mono">{{ asAttempt(row).order_no ?? '—' }}</span></template></el-table-column>
          <el-table-column :label="t('redeem.log.client')" width="120"><template #default="{ row }"><span class="mono small">{{ asAttempt(row).ip_class ?? '—' }}</span></template></el-table-column>
        </el-table>
        <div class="pager"><el-pagination layout="total, prev, pager, next" :total="log.total.value" :page-size="log.pageSize" :current-page="log.page.value" @current-change="log.load" /></div>
      </el-tab-pane>

      <!-- ============================================================ lookup -->
      <el-tab-pane name="lookup" :label="t('redeem.tab.lookup')">
        <p class="muted small">{{ t('redeem.lookup.help') }}</p>
        <el-form inline @submit.prevent="lookup">
          <el-form-item><el-input v-model="lookupCode" type="password" show-password autocomplete="off" :placeholder="t('redeem.lookup.code')" maxlength="128" style="width: 300px" /></el-form-item>
          <el-form-item>{{ t('redeem.lookup.or') }}</el-form-item>
          <el-form-item><el-input v-model="lookupOrder" :placeholder="t('redeem.log.orderRef')" maxlength="16" style="width: 160px" /></el-form-item>
          <el-form-item><el-button type="primary" native-type="submit" :loading="looking">{{ t('redeem.lookup.go') }}</el-button></el-form-item>
          <el-form-item v-if="found"><el-button @click="clearLookup">{{ t('redeem.lookup.clear') }}</el-button></el-form-item>
        </el-form>
        <div v-if="found" class="result">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item :label="t('redeem.log.code')"><span class="mono">{{ found.code ?? '—' }}</span></el-descriptions-item>
            <el-descriptions-item :label="t('redeem.lookup.ours')">
              <template v-if="found.ours">
                <span class="mono">{{ found.ours.order_no }}</span> · {{ found.ours.status }} · {{ t('redeem.lookup.buyer') }} {{ found.ours.buyer ?? '—' }} ·
                {{ t('redeem.lookup.delivered') }} {{ when(found.ours.delivered_at) }}
              </template>
              <span v-else class="muted">{{ t('redeem.lookup.notOurs') }}</span>
            </el-descriptions-item>
            <el-descriptions-item v-if="found.vendor" :label="t('redeem.lookup.vendor')">
              <el-tag :type="found.vendor.available && found.vendor.found ? (cdkActions(found.vendor.status).disable ? 'success' : 'info') : 'warning'" size="small">{{ vendorText }}</el-tag>
              <span v-if="found.vendor.available && found.vendor.plan" class="muted"> · {{ found.vendor.plan }}</span>
              <span v-if="found.vendor.available && found.vendor.env" class="muted"> · {{ found.vendor.env }}</span>
            </el-descriptions-item>
            <el-descriptions-item v-else :label="t('redeem.lookup.vendor')"><span class="muted">{{ t('redeem.lookup.pasteForVendor') }}</span></el-descriptions-item>
          </el-descriptions>
          <div v-if="auth.isAdmin && lastCode" class="acts">
            <el-button type="warning" :disabled="!actions.disable" :loading="acting" @click="setCdk('disable')">{{ t('redeem.lookup.disable') }}</el-button>
            <el-button :disabled="!actions.enable" :loading="acting" @click="setCdk('enable')">{{ t('redeem.lookup.enable') }}</el-button>
          </div>
          <h4>{{ t('redeem.lookup.attempts') }}</h4>
          <el-table :data="found.attempts" row-key="id" :empty-text="t('redeem.log.empty')" size="small">
            <el-table-column :label="t('redeem.log.time')" width="170"><template #default="{ row }">{{ when(asAttempt(row).at) }}</template></el-table-column>
            <el-table-column :label="t('redeem.log.step')" width="110"><template #default="{ row }">{{ word('step', asAttempt(row).step) }}</template></el-table-column>
            <el-table-column :label="t('redeem.log.outcome')" width="130"><template #default="{ row }"><el-tag :type="outcomeTag(asAttempt(row).outcome)" size="small">{{ word('outcome', asAttempt(row).outcome) }}</el-tag></template></el-table-column>
            <el-table-column :label="t('redeem.log.reason')" min-width="140"><template #default="{ row }">{{ asAttempt(row).reason ? word('reason', asAttempt(row).reason) : '—' }}</template></el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ============================================================ stats -->
      <el-tab-pane name="stats" :label="t('redeem.tab.stats')">
        <div v-loading="statsLoading">
          <el-button size="small" @click="loadStats">{{ t('redeem.refresh') }}</el-button>
          <template v-if="stats">
            <div class="tiles">
              <div class="tile"><b>{{ stats.totals.attempts }}</b><span>{{ t('redeem.stats.attempts') }}</span></div>
              <div class="tile"><b>{{ stats.totals.completed }}</b><span>{{ t('redeem.stats.completed') }}</span></div>
              <div class="tile"><b>{{ stats.totals.failed }}</b><span>{{ t('redeem.stats.failed') }}</span></div>
              <div class="tile"><b>{{ ratePct(stats.totals.success_rate) }}</b><span>{{ t('redeem.stats.rate') }}</span></div>
            </div>
            <p class="muted small">{{ t('redeem.stats.help') }}</p>
            <div class="bars" role="img" :aria-label="t('redeem.stats.perDay')">
              <span v-for="(d, i) in stats.days" :key="d.day" class="bar" :title="`${d.day}: ${d.attempts}`" :style="{ height: bars[i] + '%' }" />
            </div>
            <el-row :gutter="16">
              <el-col :xs="24" :md="14">
                <h4>{{ t('redeem.stats.perDay') }}</h4>
                <el-table :data="dayRows" row-key="day" size="small" :empty-text="t('redeem.log.empty')" max-height="320">
                  <el-table-column prop="day" :label="t('redeem.stats.day')" width="120" />
                  <el-table-column prop="attempts" :label="t('redeem.stats.attempts')" width="90" />
                  <el-table-column prop="completed" :label="t('redeem.stats.completed')" width="100" />
                  <el-table-column prop="failed" :label="t('redeem.stats.failed')" width="90" />
                  <el-table-column :label="t('redeem.stats.rate')" width="100"><template #default="{ row }">{{ ratePct(row.success_rate) }}</template></el-table-column>
                </el-table>
              </el-col>
              <el-col :xs="24" :md="10">
                <h4>{{ t('redeem.stats.reasons') }}</h4>
                <el-table :data="stats.top_reasons" row-key="reason" size="small" :empty-text="t('redeem.log.empty')">
                  <el-table-column :label="t('redeem.log.reason')"><template #default="{ row }">{{ word('reason', row.reason) }}</template></el-table-column>
                  <el-table-column prop="count" width="80" />
                </el-table>
              </el-col>
            </el-row>
          </template>
        </div>
      </el-tab-pane>

      <!-- ============================================================ settings -->
      <el-tab-pane v-if="auth.isAdmin" name="settings" :label="t('redeem.tab.settings')">
        <el-form v-if="settings && form" label-position="top" class="settings" @submit.prevent="saveSettings">
          <el-form-item :label="t('redeem.settings.maintenance')">
            <el-switch v-model="form.maintenance" />
            <span class="muted small gap">{{ t('redeem.settings.maintenanceHelp') }}</span>
          </el-form-item>
          <el-form-item :label="t('redeem.settings.maintenanceEn')" :error="errors.maintenance_en ? t('redeem.settings.tooLong', { n: settings.caps.maintenance }) : ''">
            <el-input v-model="form.maintenance_en" type="textarea" :rows="2" />
          </el-form-item>
          <el-form-item :label="t('redeem.settings.maintenanceZh')" :error="errors.maintenance_zh ? t('redeem.settings.tooLong', { n: settings.caps.maintenance }) : ''">
            <el-input v-model="form.maintenance_zh" type="textarea" :rows="2" />
          </el-form-item>
          <el-form-item :label="t('redeem.settings.announcementEn')" :error="errors.announcement_en ? t('redeem.settings.tooLong', { n: settings.caps.announcement }) : ''">
            <el-input v-model="form.announcement_en" type="textarea" :rows="3" />
          </el-form-item>
          <el-form-item :label="t('redeem.settings.announcementZh')" :error="errors.announcement_zh ? t('redeem.settings.tooLong', { n: settings.caps.announcement }) : ''">
            <el-input v-model="form.announcement_zh" type="textarea" :rows="3" />
          </el-form-item>
          <p class="muted small">{{ t('redeem.settings.textHelp') }}</p>
          <h4>{{ t('redeem.settings.limits') }}</h4>
          <p class="muted small">{{ t('redeem.settings.limitsHelp') }}</p>
          <div class="limits">
            <el-form-item v-for="s in LIMIT_STEPS" :key="s" :label="word('step', s)" :error="errors[`limit_${s}`] ? t(`redeem.settings.limitErr.${errors[`limit_${s}`]}`, { min: settings.bounds[s].min, max: settings.bounds[s].max }) : ''">
              <el-input v-model="form.ip_limits[s]" :placeholder="t('redeem.settings.default', { n: settings.defaults[s] })" inputmode="numeric" maxlength="3" />
            </el-form-item>
          </div>
          <el-button type="primary" native-type="submit" :loading="saving" :disabled="!dirty || Object.keys(errors).length > 0">{{ t('common.save') }}</el-button>
        </el-form>
        <el-empty v-else :description="t('redeem.settings.unavailable')" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.filters :deep(.el-form-item) { margin-bottom: 8px; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.small { font-size: 12px; }
.muted { color: var(--el-text-color-secondary); }
.gap { margin-left: 12px; }
.acts { display: flex; gap: 8px; margin: 12px 0; }
.result { margin-top: 12px; max-width: 900px; }
.tiles { display: flex; flex-wrap: wrap; gap: 12px; margin: 12px 0; }
.tile { min-width: 130px; padding: 10px 14px; border: 1px solid var(--el-border-color); border-radius: 8px; display: flex; flex-direction: column; }
.tile b { font-size: 22px; }
.tile span { font-size: 12px; color: var(--el-text-color-secondary); }
.bars { display: flex; align-items: flex-end; gap: 3px; height: 90px; margin: 8px 0 16px; border-bottom: 1px solid var(--el-border-color); }
.bar { flex: 1; min-width: 4px; background: var(--el-color-primary); border-radius: 2px 2px 0 0; }
.settings { max-width: 640px; }
.limits { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 0 12px; }
</style>
