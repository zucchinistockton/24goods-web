<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Download, Refresh } from '@element-plus/icons-vue'
import { formatDateTime, isApiError } from '@24goods/shared'
import { client } from '../api'
import { createPricingApi, type ActualRow, type PricingSettings, type PricingSettingsPatch, type PricingSettingsView, type UnitRow } from '../api-procurement'
import { usePaged } from '../composables/usePaged'
import { confirmBox, toastError, toastOk } from '../ui'

/** Staff-only cost model (issue #28): fee schedule, per-unit estimates, recommended prices
 *  (never applied automatically) and the actual per-order margins. */
const api = createPricingApi(client)
const { t, te, locale } = useI18n()

const when = (v: string | null | undefined) => (v ? formatDateTime(v, locale.value) : '—')
const usd = (v: string | null | undefined) => (v == null ? '—' : `${v} USD`)
const pctText = (v: string | null | undefined) => (v == null ? '—' : `${v} %`)
const DEC = /^[0-9]{1,9}(\.[0-9]{1,6})?$/
function fail(e: unknown) {
  if (isApiError(e) && te(`procurement.pricing.err.${e.code}`)) ElMessage.error({ message: t(`procurement.pricing.err.${e.code}`), grouping: true })
  else toastError(e)
}

// ------------------------------------------------------------------ fee schedule
const MONEY_KEYS = ['card_open_fee', 'topup_fee_fixed', 'failed_fee', 'chargeback_tier1_surcharge', 'chargeback_tier2_deposit'] as const
const RATE_KEYS = ['topup_fee_rate', 'auth_reversal_rate', 'chargeback_tier1_rate', 'chargeback_tier2_rate', 'expected_failure_rate', 'target_margin', 'target_markup'] as const
const INT_KEYS = ['failed_free_count', 'max_auto_switches', 'chargeback_tier1_min_tx', 'round_step_minor'] as const
const INFO = new Set(['failed_free_count', 'auth_reversal_rate', 'chargeback_tier1_rate', 'chargeback_tier1_min_tx', 'chargeback_tier1_surcharge', 'chargeback_tier2_rate', 'chargeback_tier2_deposit'])
const DIRECT = ['gpt', 'grok', 'claude'] as const
const TIERS = ['standard', 'pro20x'] as const

const loaded = ref<PricingSettings | null>(null)
const channel = ref<{ id: string; fee_rate: string } | null>(null)
const form = reactive<Record<string, string>>({})
const direct = reactive<Record<string, string>>({})
const uses = reactive<Record<string, number>>({})
const fx = reactive({ currency: 'CNY', per_usd: '' })
const saving = ref(false)
const errs = reactive<Record<string, string>>({})

function fill(s: PricingSettings) {
  loaded.value = s
  for (const k of [...MONEY_KEYS, ...RATE_KEYS]) form[k] = s[k]
  for (const k of INT_KEYS) form[k] = String(s[k])
  for (const k of DIRECT) direct[k] = s.service_fee_direct[k]
  for (const k of TIERS) uses[k] = s.uses_per_card[k]
  fx.per_usd = s.fx[fx.currency]?.per_usd ?? ''
}
async function loadSettings() {
  try {
    const r = await api.settings()
    channel.value = r.channel
    fill(r.settings)
  } catch (e) {
    fail(e)
  }
}
const fxUpdated = computed(() => loaded.value?.fx[fx.currency]?.updated_at ?? '')

async function saveSettings() {
  for (const k of Object.keys(errs)) delete errs[k]
  for (const k of [...MONEY_KEYS, ...RATE_KEYS]) if (!DEC.test(form[k] ?? '')) errs[k] = t('procurement.pricing.err.decimal')
  for (const k of RATE_KEYS) if (!errs[k] && Number(form[k]) >= 1 && k !== 'target_margin') errs[k] = t('procurement.pricing.err.rate')
  if (!errs.target_margin && Number(form.target_margin) >= 1) errs.target_margin = t('procurement.pricing.err.rate')
  for (const k of INT_KEYS) if (!/^[0-9]{1,7}$/.test(form[k] ?? '') || (k === 'round_step_minor' && Number(form[k]) < 1)) errs[k] = t('procurement.pricing.err.int')
  // the vendor's own default is 2: the cost estimate may not assume fewer switches (server floor)
  if (!errs.max_auto_switches && (Number(form.max_auto_switches) < 2 || Number(form.max_auto_switches) > 10)) errs.max_auto_switches = t('procurement.pricing.err.switches')
  for (const k of DIRECT) if (!DEC.test(direct[k] ?? '')) errs[`direct_${k}`] = t('procurement.pricing.err.decimal')
  if (fx.per_usd.trim() && !DEC.test(fx.per_usd.trim())) errs.fx = t('procurement.pricing.err.decimal')
  if (Object.keys(errs).length) return
  const body: PricingSettingsPatch = { service_fee_direct: { ...direct }, uses_per_card: { ...uses } }
  for (const k of [...MONEY_KEYS, ...RATE_KEYS]) (body as Record<string, unknown>)[k] = form[k]
  for (const k of INT_KEYS) (body as Record<string, unknown>)[k] = Number(form[k])
  const rate = fx.per_usd.trim()
  if (rate) body.fx = { currency: fx.currency, per_usd: rate }
  saving.value = true
  try {
    let r: PricingSettingsView
    try {
      r = await api.saveSettings(body)
    } catch (e) {
      if (!(isApiError(e) && e.code === 'fx_change_confirm' && body.fx)) throw e
      if (!(await confirmBox(t('procurement.pricing.fxConfirm'), t('procurement.pricing.fx', { currency: fx.currency })))) return
      r = await api.saveSettings({ ...body, fx: { ...body.fx, confirm: true } })
    }
    channel.value = r.channel
    fill(r.settings)
    toastOk(t('procurement.pricing.saved'))
    void units.reload()
  } catch (e) {
    fail(e)
  } finally {
    saving.value = false
  }
}

// ------------------------------------------------------------------ per-unit table
const truncated = ref(false)
const units = usePaged<UnitRow>(async (page, pageSize) => {
  const r = await api.units({ page, page_size: pageSize })
  truncated.value = r.truncated
  return r
}, 20)
const staleFx = computed(() => units.items.value.some((r) => r.fx_stale))
const applying = ref('')
const rowName = (r: UnitRow) => (r.variant_name ? `${r.title} · ${r.variant_name}` : r.title)
const sourceName = (r: UnitRow) => `${r.source.supplier ?? t('procurement.pricing.manualCost')} (${r.source.kind})`
const reason = (r: UnitRow) => r.incomplete.map((c) => (te(`procurement.pricing.inc.${c}`) ? t(`procurement.pricing.inc.${c}`) : c)).join(', ')
const diffClass = (r: UnitRow) => (r.recommended && r.recommended.diff_minor > 0 ? 'up' : r.recommended && r.recommended.diff_minor < 0 ? 'down' : '')
async function apply(r: UnitRow) {
  if (!r.recommended) return
  const ok = await confirmBox(
    t('procurement.pricing.applyConfirm', { name: rowName(r), from: `${r.price.amount} ${r.price.currency}`, to: `${r.recommended.amount} ${r.recommended.currency}` }),
    t('procurement.pricing.apply'),
  )
  if (!ok) return
  applying.value = r.key
  try {
    try {
      await api.apply(r)
    } catch (e) {
      if (!(isApiError(e) && e.code === 'confirm_large_drop')) throw e
      if (!(await confirmBox(t('procurement.pricing.largeDrop'), t('procurement.pricing.apply')))) return
      await api.apply(r, true)
    }
    toastOk(t('procurement.pricing.applied'))
    void units.load()
  } catch (e) {
    fail(e)
    // the price, the recommendation or the rate moved meanwhile: show the fresh numbers
    if (isApiError(e) && ['recommendation_changed', 'fx_stale', 'offer_disabled'].includes(e.code)) void units.load()
  } finally {
    applying.value = ''
  }
}
const targetDlg = reactive({ open: false, row: null as UnitRow | null, value: '' })
function openTarget(r: UnitRow) {
  targetDlg.row = r
  targetDlg.value = r.target_margin_pct
  targetDlg.open = true
}
async function saveTarget(clear: boolean) {
  const r = targetDlg.row
  if (!r) return
  const v = Number(targetDlg.value)
  if (!clear && !(v >= 0 && v < 100)) return void ElMessage.error(t('procurement.pricing.err.rate'))
  try {
    await api.setTarget(r.product_id, r.variant_id, clear ? null : (v / 100).toFixed(4).replace(/0+$/, '').replace(/\.$/, '') || '0')
    targetDlg.open = false
    void units.load()
  } catch (e) {
    fail(e)
  }
}

// ------------------------------------------------------------------ actual margins
const today = () => new Date().toISOString().slice(0, 10)
const daysAgo = (n: number) => new Date(Date.now() - n * 86400_000).toISOString().slice(0, 10)
const range = ref<[string, string]>([daysAgo(29), today()])
const q = computed(() => ({ date_from: range.value?.[0], date_to: range.value?.[1] }))
const actual = usePaged<ActualRow>((page, pageSize) => api.actual({ ...q.value, page, page_size: pageSize }), 10)
const csvHref = computed(() => api.actualCsvUrl(q.value))
watch(range, () => void actual.reload())

onMounted(() => {
  void loadSettings()
  void units.load()
  void actual.load()
})
</script>

<template>
  <div>
    <el-alert type="info" :closable="false" show-icon :title="t('procurement.pricing.help')" class="bar" />

    <!-- ============================================================ fee schedule -->
    <div class="panel">
      <h2 class="h">{{ t('procurement.pricing.schedule') }}</h2>
      <p class="muted small">{{ t('procurement.pricing.scheduleHelp') }}</p>
      <el-form label-position="top" class="grid" @submit.prevent="saveSettings">
        <el-form-item v-for="k in MONEY_KEYS" :key="k" :label="t(`procurement.pricing.f.${k}`) + (INFO.has(k) ? ' ⓘ' : '')" :error="errs[k]">
          <el-input v-model="form[k]" inputmode="decimal" maxlength="16"><template #append>USD</template></el-input>
        </el-form-item>
        <el-form-item v-for="k in RATE_KEYS" :key="k" :label="t(`procurement.pricing.f.${k}`) + (INFO.has(k) ? ' ⓘ' : '')" :error="errs[k]">
          <el-input v-model="form[k]" inputmode="decimal" maxlength="16" />
        </el-form-item>
        <el-form-item v-for="k in INT_KEYS" :key="k" :label="t(`procurement.pricing.f.${k}`) + (INFO.has(k) ? ' ⓘ' : '')" :error="errs[k]">
          <el-input v-model="form[k]" inputmode="numeric" maxlength="7" />
        </el-form-item>
        <el-form-item v-for="k in DIRECT" :key="k" :label="t('procurement.pricing.f.service_fee_direct', { product: t(`procurement.pricing.prod.${k}`) })" :error="errs[`direct_${k}`]">
          <el-input v-model="direct[k]" inputmode="decimal" maxlength="16"><template #append>USD</template></el-input>
        </el-form-item>
        <el-form-item v-for="k in TIERS" :key="k" :label="t('procurement.pricing.f.uses_per_card', { tier: t(`procurement.pricing.tier.${k}`) })">
          <el-input-number v-model="uses[k]" :min="1" :max="1000" />
        </el-form-item>
        <el-form-item :label="t('procurement.pricing.fx', { currency: fx.currency })" :error="errs.fx">
          <el-input v-model="fx.per_usd" inputmode="decimal" maxlength="16" :placeholder="t('procurement.pricing.fxPlaceholder')"><template #prepend>1 USD =</template><template #append>{{ fx.currency }}</template></el-input>
          <div class="muted small">{{ fxUpdated ? t('procurement.pricing.fxUpdated', { at: when(fxUpdated) }) : t('procurement.pricing.fxMissing') }}</div>
        </el-form-item>
      </el-form>
      <p class="muted small">{{ t('procurement.pricing.channel', { id: channel?.id ?? '—', rate: channel ? (Number(channel.fee_rate) * 100).toFixed(2) : '—' }) }}</p>
      <p class="muted small">ⓘ {{ t('procurement.pricing.infoNote') }}</p>
      <el-button type="primary" :loading="saving" @click="saveSettings">{{ t('common.save') }}</el-button>
    </div>

    <!-- ============================================================ per unit -->
    <div class="panel gap">
      <div class="head">
        <h2 class="h">{{ t('procurement.pricing.units') }}</h2>
        <el-button :icon="Refresh" @click="units.load()">{{ t('procurement.refresh') }}</el-button>
      </div>
      <p class="muted small">{{ t('procurement.pricing.unitsHelp') }}</p>
      <el-alert v-if="staleFx" type="warning" :closable="false" show-icon :title="t('procurement.pricing.fxStale')" class="bar" />
      <el-alert v-if="truncated" type="warning" :closable="false" show-icon :title="t('procurement.pricing.truncated')" class="bar" />
      <el-table v-loading="units.loading.value" :data="units.items.value" row-key="key" :empty-text="t('procurement.cost.empty')" size="small">
        <el-table-column :label="t('procurement.pricing.col.unit')" min-width="200" fixed>
          <template #default="{ row }">
            <div>{{ rowName(row as UnitRow) }}</div>
            <div class="muted small">{{ sourceName(row as UnitRow) }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="t('procurement.pricing.col.price')" width="130">
          <template #default="{ row }"><span class="num">{{ row.price.amount }} {{ row.price.currency }}</span><div class="muted small num">{{ usd(row.price.usd) }}</div></template>
        </el-table-column>
        <el-table-column :label="t('procurement.pricing.col.cost')" min-width="230">
          <template #default="{ row }">
            <el-popover placement="bottom" :width="300" trigger="hover">
              <template #reference>
                <span class="num">{{ usd((row as UnitRow).cost_usd.total) }}</span>
              </template>
              <div v-for="k in ['supplier_price', 'service_fee', 'topup_fee', 'card_share', 'failure_cost', 'channel_fee']" :key="k" class="line">
                <span>{{ t(`procurement.pricing.c.${k}`) }}</span><span class="num">{{ usd((row as UnitRow).cost_usd[k as keyof UnitRow['cost_usd']]) }}</span>
              </div>
            </el-popover>
            <el-tag v-if="(row as UnitRow).cost_incomplete" size="small" type="warning" class="tag">{{ t('procurement.pricing.incomplete') }}</el-tag>
            <el-tag v-if="(row as UnitRow).fx_stale" size="small" type="warning" class="tag">{{ t('procurement.pricing.fxStaleTag') }}</el-tag>
            <div v-if="(row as UnitRow).cost_incomplete" class="muted small">{{ reason(row as UnitRow) }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="t('procurement.pricing.col.gross')" width="120"><template #default="{ row }"><span class="num">{{ usd(row.gross_usd) }}</span><div class="muted small num">{{ pctText(row.gross_margin_pct) }}</div></template></el-table-column>
        <el-table-column :label="t('procurement.pricing.col.net')" width="120"><template #default="{ row }"><span class="num strong">{{ usd(row.net_usd) }}</span><div class="muted small num">{{ pctText(row.net_margin_pct) }}</div></template></el-table-column>
        <el-table-column :label="t('procurement.pricing.col.target')" width="100">
          <template #default="{ row }"><el-link type="primary" :underline="false" @click="openTarget(row as UnitRow)">{{ pctText(row.target_margin_pct) }}</el-link></template>
        </el-table-column>
        <el-table-column :label="t('procurement.pricing.col.recommended')" width="160">
          <template #default="{ row }">
            <template v-if="(row as UnitRow).recommended">
              <span class="num strong">{{ (row as UnitRow).recommended!.amount }} {{ (row as UnitRow).recommended!.currency }}</span>
              <div class="small num" :class="diffClass(row as UnitRow)">{{ (row as UnitRow).recommended!.diff_minor > 0 ? '+' : '' }}{{ (row as UnitRow).recommended!.diff }}</div>
            </template>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('common.actions')" width="100" fixed="right">
          <template #default="{ row }">
            <el-button size="small" :disabled="!(row as UnitRow).recommended || (row as UnitRow).cost_incomplete || (row as UnitRow).fx_stale || (row as UnitRow).source.enabled === false || (row as UnitRow).recommended!.diff_minor === 0" :loading="applying === row.key" @click="apply(row as UnitRow)">{{ t('procurement.pricing.apply') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination layout="total, prev, pager, next" :total="units.total.value" :page-size="units.pageSize" :current-page="units.page.value" @current-change="units.load" />
      </div>
    </div>

    <!-- ============================================================ actual -->
    <div class="panel gap">
      <div class="head">
        <h2 class="h">{{ t('procurement.pricing.actual') }}</h2>
        <div class="tools">
          <el-date-picker v-model="range" type="daterange" value-format="YYYY-MM-DD" :clearable="false" unlink-panels :aria-label="t('procurement.cost.range')" />
          <a :href="csvHref" download class="csv"><el-button :icon="Download">{{ t('procurement.cost.csv') }}</el-button></a>
        </div>
      </div>
      <p class="muted small">{{ t('procurement.pricing.actualHelp') }}</p>
      <el-table v-loading="actual.loading.value" :data="actual.items.value" row-key="order_id" :empty-text="t('procurement.cost.empty')" size="small">
        <el-table-column :label="t('procurement.job.order')" min-width="150"><template #default="{ row }"><span class="mono">{{ row.order_no }}</span></template></el-table-column>
        <el-table-column :label="t('procurement.pricing.col.revenue')" width="140"><template #default="{ row }"><span class="num">{{ row.revenue }} {{ row.revenue_currency }}</span><div class="muted small num">{{ t('procurement.pricing.paid', { amount: (row as ActualRow).paid }) }}</div><div class="muted small num">{{ usd(row.revenue_usd) }}</div></template></el-table-column>
        <el-table-column :label="t('procurement.job.cost')" width="130"><template #default="{ row }"><span class="num">{{ usd(row.cost_usd) }}</span></template></el-table-column>
        <el-table-column :label="t('procurement.pricing.c.channel_fee')" width="130"><template #default="{ row }"><span class="num">{{ usd(row.channel_fee_usd) }}</span></template></el-table-column>
        <el-table-column :label="t('procurement.pricing.col.gross')" width="130"><template #default="{ row }"><span class="num">{{ usd(row.gross_usd) }}</span></template></el-table-column>
        <el-table-column :label="t('procurement.pricing.col.net')" width="140">
          <template #default="{ row }">
            <span class="num strong">{{ usd(row.net_usd) }}</span>
            <div class="muted small num">{{ pctText(row.net_margin_pct) }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="t('procurement.pricing.col.state')" min-width="170">
          <template #default="{ row }">
            <el-tag v-if="row.cost_incomplete" size="small" type="warning">{{ t('procurement.pricing.incomplete') }}</el-tag>
            <div v-if="row.cost_incomplete" class="muted small">{{ (row as ActualRow).reasons.map((c) => (te(`procurement.pricing.areason.${c}`) ? t(`procurement.pricing.areason.${c}`) : c)).join(', ') }}</div>
            <div v-else-if="(row as ActualRow).fee_estimated" class="muted small">{{ t('procurement.pricing.feeEstimated') }}</div>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination layout="total, prev, pager, next" :total="actual.total.value" :page-size="actual.pageSize" :current-page="actual.page.value" @current-change="actual.load" />
      </div>
    </div>

    <el-dialog v-model="targetDlg.open" :title="t('procurement.pricing.targetTitle')" width="min(420px, 96vw)">
      <p class="muted small">{{ targetDlg.row ? rowName(targetDlg.row) : '' }}</p>
      <el-input v-model="targetDlg.value" inputmode="decimal" maxlength="6"><template #append>%</template></el-input>
      <template #footer>
        <el-button @click="saveTarget(true)">{{ t('procurement.pricing.targetReset') }}</el-button>
        <el-button type="primary" @click="saveTarget(false)">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.bar {
  margin-bottom: 12px;
}
.gap {
  margin-top: 16px;
}
.h {
  font-size: 14px;
  font-weight: 600;
  margin: 0 0 8px;
}
.small {
  font-size: 12px;
}
.num {
  font-variant-numeric: tabular-nums;
}
.strong {
  font-weight: 600;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  column-gap: 16px;
}
.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.tools {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.csv {
  text-decoration: none;
}
.line {
  display: flex;
  justify-content: space-between;
  padding: 2px 0;
}
.tag {
  margin-left: 6px;
}
.up {
  color: var(--el-color-success);
}
.down {
  color: var(--el-color-danger);
}
</style>
