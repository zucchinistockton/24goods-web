<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import { ArrowDown, ArrowUp, Download, Plus, Refresh } from '@element-plus/icons-vue'
import { createCoalescer, formatDateTime, isApiError, type AdminVariant } from '@24goods/shared'
import { apiV3, client } from '../api'
import type { AdminProductV3 } from '../api-v3'
import {
  COST_RE,
  COUNTRY_RE,
  PLAN_RE,
  createCardsApi,
  createProcurementApi,
  type CostGroup,
  type CostSummaryRow,
  type JobAction,
  type MarginRow,
  type ProcJob,
  type ProcJobDetail,
  type ProcSource,
  type Routing,
  type SourceParams,
  type UnreconciledRow,
  type ChargeCheck,
} from '../api-procurement'
import PricingPanel from '../components/PricingPanel.vue'
import RegionsPanel from '../components/RegionsPanel.vue'
import CardsPanel from '../components/CardsPanel.vue'
import FundingPanel from '../components/FundingPanel.vue'
import RedeemPanel from '../components/RedeemPanel.vue'
import { createSuppliersApi, type AdminSupplier } from '../api-suppliers'
import { usePaged } from '../composables/usePaged'
import { useAuthStore } from '../stores/auth'
import { useRealtimeStore } from '../stores/realtime'
import { confirmBox, toastError, toastOk } from '../ui'

const api = createProcurementApi(client)
const suppliersApi = createSuppliersApi(client)
const auth = useAuthStore()
const { t, te, locale } = useI18n()
const route = useRoute()

const tab = ref<'jobs' | 'sources' | 'cost' | 'pricing' | 'regions' | 'cards' | 'redeem'>('jobs')
// decline-rate warning badge on the Cards tab (80 % of a vendor threshold); staff can read it
const riskLevel = ref<'ok' | 'warning' | 'critical'>('ok')
const when = (v: string | null | undefined) => (v ? formatDateTime(v, locale.value) : '—')
const money = (v: string | null | undefined, cur: string | null | undefined) => (v == null ? '—' : `${v} ${cur ?? ''}`.trim())
/** Server error codes of the procurement routes, translated when a key exists. */
function fail(e: unknown) {
  if (isApiError(e) && te(`procurement.err.${e.code}`)) ElMessage.error({ message: t(`procurement.err.${e.code}`), grouping: true })
  else toastError(e)
}

// ------------------------------------------------------------------ jobs
const STATES = ['queued', 'submitting', 'accepted_pending', 'needs_manual', 'fulfilled', 'failed', 'void_pending', 'voided'] as const
const stateFilter = ref('')
const counts = ref<Record<string, number>>({})
const jobs = usePaged<ProcJob>(async (page, pageSize) => {
  const r = await api.jobs.list({ state: stateFilter.value || undefined, page, page_size: pageSize })
  counts.value = r.counts
  return r
})
const stateType = (s: string) =>
  s === 'fulfilled' ? 'success' : s === 'needs_manual' || s === 'failed' ? 'danger' : s === 'voided' ? 'info' : 'warning'
const stateLabel = (s: string) => (te(`procurement.state.${s}`) ? t(`procurement.state.${s}`) : s)

const drawer = ref(false)
const detail = ref<ProcJobDetail | null>(null)
const detailLoading = ref(false)
const acting = ref<JobAction | null>(null)
async function openJob(row: ProcJob) {
  drawer.value = true
  detail.value = null
  detailLoading.value = true
  try {
    detail.value = await api.jobs.get(row.id)
  } catch (e) {
    drawer.value = false
    fail(e)
  } finally {
    detailLoading.value = false
  }
}
async function refreshJob(id: string) {
  try {
    detail.value = await api.jobs.get(id)
  } catch {
    /* the list reload below reports a real failure */
  }
  void jobs.load()
}
// the open job follows the supplier live (owner request 2026-11-18): a progress event of its order,
// or a polling tick while it is not finished, reloads it (debounced, never two loads at once)
const LIVE_STATES = new Set(['queued', 'submitting', 'accepted_pending', 'void_pending', 'needs_manual'])
const liveJob = createCoalescer(async () => {
  const d = detail.value
  if (!drawer.value || !d) return
  const fresh = await api.jobs.get(d.id)
  if (drawer.value && detail.value?.id === fresh.id) detail.value = fresh
}, 400)
const rt = useRealtimeStore()
const liveOffs = [
  rt.on('order_progress', (e) => {
    if (drawer.value && detail.value?.order.id === e.order_id) liveJob.trigger()
  }),
  rt.on('sync', () => {
    if (drawer.value && detail.value && LIVE_STATES.has(detail.value.state)) liveJob.trigger()
  }),
]
onBeforeUnmount(() => {
  liveOffs.forEach((off) => off())
  liveJob.cancel()
})
const can = (a: JobAction) => !!detail.value?.actions.includes(a) && (a !== 'void' || auth.isAdmin)

const dlg = reactive({ open: false, action: 'fulfil' as 'fulfil' | 'void' | 'fail', code: '', cardId: '', reason: '', error: '' })
// ADR-048: before a direct job is marked done by hand, staff see the charge evidence and confirm it
const chk = reactive({ loading: false, failed: false, data: null as ChargeCheck | null, confirmed: false, jobId: '', seq: 0 })
async function loadCharge(id: string) {
  // every load starts a new request and clears the tick: a late answer for another job (or an
  // older click) can never be confirmed in place of the current one
  const seq = ++chk.seq
  Object.assign(chk, { loading: true, failed: false, data: null, confirmed: false, jobId: id })
  try {
    const data = await api.jobs.chargeCheck(id)
    if (seq === chk.seq && detail.value?.id === id) chk.data = data
  } catch (e) {
    if (seq === chk.seq) {
      chk.failed = true
      toastError(e)
    }
  } finally {
    if (seq === chk.seq) chk.loading = false
  }
}
const stateText = (group: string, v: string | null | undefined) => (v && te(`${group}.${v}`) ? t(`${group}.${v}`) : v || '')
function askReason(action: 'fulfil' | 'void' | 'fail') {
  Object.assign(dlg, { open: true, action, code: '', cardId: '', reason: '', error: '' })
  if (action === 'fulfil' && detail.value?.kind === 'direct') void loadCharge(detail.value.id)
}
const aiCharges = computed(() => (chk.data?.supplier?.charges ?? []).filter((c) => c.ai && c.status !== 'DECLINED' && c.status !== 'FAILED'))
async function run(action: JobAction) {
  const j = detail.value
  if (!j || acting.value) return
  if (action === 'fulfil' || action === 'void' || action === 'fail') return askReason(action)
  if (!(await confirmBox(t(`procurement.confirm.${action}`)))) return
  acting.value = action
  try {
    if (action === 'requery') await api.jobs.requery(j.id)
    else if (action === 'retry') await api.jobs.retry(j.id)
    else await api.jobs.switchSource(j.id)
    toastOk(t('procurement.done'))
    await refreshJob(j.id)
  } catch (e) {
    fail(e)
  } finally {
    acting.value = null
  }
}
async function submitDlg() {
  const j = detail.value
  if (!j || acting.value) return
  const reason = dlg.reason.trim()
  const code = dlg.code.trim()
  const direct = j.kind === 'direct'
  if (dlg.action === 'fulfil' && !direct && !code) return void (dlg.error = t('procurement.err.codeRequired'))
  if (dlg.action === 'fulfil' && direct && reason.length < 12) return void (dlg.error = t('procurement.err.reason_required'))
  if (dlg.action === 'fulfil' && direct && (!chk.confirmed || !chk.data || chk.jobId !== j.id))
    return void (dlg.error = t('procurement.err.charge_check_required'))
  if (dlg.action !== 'fulfil' && reason.length < 3) return void (dlg.error = t('procurement.err.reasonRequired'))
  acting.value = dlg.action
  try {
    if (dlg.action === 'fulfil')
      await api.jobs.fulfil(j.id, direct ? '' : code, reason, j.kind === 'vcc' ? dlg.cardId.trim() || undefined : undefined, direct ? chk.confirmed : undefined)
    else if (dlg.action === 'void') await api.jobs.void(j.id, reason)
    else await api.jobs.fail(j.id, reason)
    dlg.code = '' // the pasted code does not stay in memory after it was sent
    dlg.open = false
    toastOk(t('procurement.done'))
    await refreshJob(j.id)
  } catch (e) {
    if (isApiError(e) && te(`procurement.err.${e.code}`)) dlg.error = t(`procurement.err.${e.code}`)
    else toastError(e)
  } finally {
    acting.value = null
  }
}

// ------------------------------------------------------------------ sources
const products = ref<AdminProductV3[]>([])
const searching = ref(false)
const productId = ref('')
const variantId = ref<string | null>(null)
const routing = ref<Routing>('priority')
const sources = ref<ProcSource[]>([])
const sourcesLoading = ref(false)
const suppliers = ref<AdminSupplier[]>([])
const checking = ref<string | null>(null)
const product = computed(() => products.value.find((p) => p.id === productId.value) ?? null)
const variants = computed<AdminVariant[]>(() => (product.value?.variants ?? []).filter((v) => v.status === 'active'))
const titleOf = (p: AdminProductV3) => (locale.value === 'zh-CN' && p.title_i18n['zh-CN']) || p.title_i18n.en
const variantName = (v: AdminVariant) => (locale.value === 'zh-CN' && v.name_i18n['zh-CN']) || v.name_i18n.en

async function searchProducts(q = '') {
  searching.value = true
  try {
    const r = await apiV3.products.list({ q: q || undefined, page: 1, page_size: 50 })
    const ups = r.items.filter((p) => p.delivery_mode === 'upstream_cdk' || p.delivery_mode === 'upstream_vcc' || p.delivery_mode === 'upstream_direct')
    const keep = product.value && !ups.some((p) => p.id === product.value!.id) ? [product.value] : []
    products.value = [...keep, ...ups]
  } catch (e) {
    toastError(e)
  } finally {
    searching.value = false
  }
}
async function loadSources() {
  if (!productId.value) return void (sources.value = [])
  sourcesLoading.value = true
  try {
    const r = await api.sources.list(productId.value, variantId.value)
    sources.value = r.items
    routing.value = r.routing
  } catch (e) {
    fail(e)
  } finally {
    sourcesLoading.value = false
  }
}
watch(productId, () => {
  variantId.value = variants.value[0]?.id ?? null
  void loadSources()
})
watch(variantId, () => void loadSources())

async function setRouting(mode: Routing) {
  const before = routing.value
  routing.value = mode
  try {
    await api.sources.routing(productId.value, variantId.value, mode)
    toastOk(t('procurement.done'))
  } catch (e) {
    routing.value = before
    fail(e)
  }
}
async function move(i: number, d: -1 | 1) {
  const ids = sources.value.map((s) => s.id)
  const j = i + d
  if (j < 0 || j >= ids.length) return
  ;[ids[i], ids[j]] = [ids[j]!, ids[i]!]
  try {
    sources.value = (await api.sources.order(productId.value, variantId.value, ids)).items
  } catch (e) {
    fail(e)
  }
}
async function toggleSource(row: ProcSource, v: boolean) {
  try {
    Object.assign(row, await api.sources.update(row.id, { enabled: v }))
  } catch (e) {
    fail(e)
    // a source without a cost cannot be switched on: open the form on the missing field
    if (isApiError(e) && e.code === 'cost_required') {
      openSource(row)
      srcDlg.enabled = true
      srcErr.value = { cost: t('procurement.err.cost_required') }
    }
  }
}
async function check(row: ProcSource) {
  if (checking.value) return
  checking.value = row.id
  try {
    const r = await api.sources.check(row.id)
    row.last_check_ok = r.available
    row.last_check_at = r.checked_at
    if (r.available) toastOk(t('procurement.src.available'))
    else ElMessage.warning({ message: t('procurement.src.unavailable', { reason: r.reason ?? '—' }), grouping: true })
  } catch (e) {
    fail(e)
  } finally {
    checking.value = null
  }
}

/** A card product's sources take a card product code and amounts instead of a plan. */
const isCard = computed(() => product.value?.delivery_mode === 'upstream_vcc')
/** A direct product's sources name the supplier product, the plan and OUR funding card. */
const isDirect = computed(() => product.value?.delivery_mode === 'upstream_direct')
const CURRENCY_RE = /^[A-Z]{3}$/
const CARD_ID_RE = /^[1-9]\d{0,11}$/
const CARD_CODE_RE = /^[A-Za-z0-9_-]{2,32}$/
const AMOUNT_RE = /^\d{1,6}(\.\d{1,2})?$/
const NAME_RE = /^[A-Za-z]{1,32}$/
const srcDlg = reactive({ open: false, saving: false, id: null as string | null, supplier_id: '', plan: '', country: '', cost: '', priority: 100, enabled: false, product_code: '', init_amount: '', recharge_amount: '', first_name: '', last_name: '', direct_product: 'gpt', funding_card: '', pay_currency: '', card_mode: 'fixed' as 'fixed' | 'auto_existing', vendor_switch: false })
const srcErr = ref<Record<string, string>>({})
function openSource(row: ProcSource | null) {
  Object.assign(srcDlg, {
    open: true,
    saving: false,
    id: row?.id ?? null,
    supplier_id: row?.supplier_id ?? suppliers.value[0]?.id ?? '',
    plan: row?.params.plan ?? '',
    country: row?.params.payment_country ?? '',
    cost: row?.cost_estimate ?? '',
    priority: row?.priority ?? 100,
    enabled: row?.enabled ?? false,
    product_code: row?.params.product_code ?? '',
    init_amount: row?.params.init_amount ?? '',
    recharge_amount: row?.params.recharge_amount ?? '',
    first_name: row?.params.first_name ?? '',
    last_name: row?.params.last_name ?? '',
    direct_product: row?.params.product ?? 'gpt',
    funding_card: row?.params.card_id != null ? String(row.params.card_id) : '',
    pay_currency: row?.params.payment_currency ?? '',
    card_mode: row?.params.card_mode ?? 'fixed',
    vendor_switch: row?.params.vendor_switch ?? false,
  })
  srcErr.value = {}
}
async function saveSource() {
  if (srcDlg.saving) return
  const plan = srcDlg.plan.trim()
  const country = srcDlg.country.trim().toUpperCase()
  const cost = srcDlg.cost.trim()
  const e: Record<string, string> = {}
  if (!srcDlg.supplier_id) e.supplier = t('procurement.err.supplierRequired')
  const card = {
    product_code: srcDlg.product_code.trim(),
    init_amount: srcDlg.init_amount.trim(),
    recharge_amount: srcDlg.recharge_amount.trim(),
    first_name: srcDlg.first_name.trim(),
    last_name: srcDlg.last_name.trim(),
  }
  if (isCard.value) {
    if (!CARD_CODE_RE.test(card.product_code)) e.product_code = t('procurement.err.productCode')
    if (!AMOUNT_RE.test(card.init_amount)) e.init_amount = t('procurement.err.amount')
    if (card.recharge_amount && !AMOUNT_RE.test(card.recharge_amount)) e.recharge_amount = t('procurement.err.amount')
    if (card.first_name && !NAME_RE.test(card.first_name)) e.first_name = t('procurement.err.name')
    if (card.last_name && !NAME_RE.test(card.last_name)) e.last_name = t('procurement.err.name')
  } else {
    if (!PLAN_RE.test(plan)) e.plan = t('procurement.err.plan')
    if (country && !COUNTRY_RE.test(country)) e.country = t('procurement.err.country')
  }
  const fundingCard = srcDlg.funding_card.trim()
  const payCurrency = srcDlg.pay_currency.trim().toUpperCase()
  if (isDirect.value) {
    if (srcDlg.card_mode === 'fixed' && !CARD_ID_RE.test(fundingCard)) e.funding_card = t('procurement.err.fundingCard')
    if (payCurrency && !CURRENCY_RE.test(payCurrency)) e.pay_currency = t('procurement.err.currency')
  }
  if (cost && !COST_RE.test(cost)) e.cost = t('procurement.err.cost')
  srcErr.value = e
  if (Object.keys(e).length) return
  // only the fields that were filled in: the server validates and applies its defaults
  const params: SourceParams = isCard.value
    ? Object.fromEntries(Object.entries(card).filter(([, v]) => v))
    : isDirect.value
      ? {
          product: srcDlg.direct_product,
          plan,
          ...(srcDlg.card_mode === 'fixed' ? { card_id: Number(fundingCard) } : { card_mode: 'auto_existing' as const }),
          ...(srcDlg.vendor_switch ? { vendor_switch: true } : {}),
          ...(country ? { payment_country: country } : {}),
          ...(payCurrency ? { payment_currency: payCurrency } : {}),
        }
      : country
      ? { plan, payment_country: country }
      : { plan }
  srcDlg.saving = true
  try {
    if (srcDlg.id) {
      await api.sources.update(srcDlg.id, { params, priority: srcDlg.priority, enabled: srcDlg.enabled, ...(cost ? { cost_estimate: cost } : { clear_cost: true }) })
    } else {
      await api.sources.create({
        product_id: productId.value,
        variant_id: variantId.value,
        supplier_id: srcDlg.supplier_id,
        params,
        priority: srcDlg.priority,
        enabled: srcDlg.enabled,
        ...(cost ? { cost_estimate: cost } : {}),
      })
    }
    srcDlg.open = false
    toastOk(t('procurement.done'))
    void loadSources()
  } catch (err) {
    if (isApiError(err) && err.code === 'cost_required') srcErr.value = { cost: t('procurement.err.cost_required') }
    else if (isApiError(err) && te(`procurement.err.${err.code}`))
      srcErr.value = { [isCard.value ? 'product_code' : err.code.startsWith('funding_card') ? 'funding_card' : 'plan']: t(`procurement.err.${err.code}`) }
    else toastError(err)
  } finally {
    srcDlg.saving = false
  }
}

// ------------------------------------------------------------------ cost
const today = () => new Date().toISOString().slice(0, 10)
const daysAgo = (n: number) => new Date(Date.now() - n * 86400_000).toISOString().slice(0, 10)
const range = ref<[string, string]>([daysAgo(29), today()])
const group = ref<CostGroup>('day')
const summary = ref<CostSummaryRow[]>([])
const summaryLoading = ref(false)
const q = computed(() => ({ date_from: range.value?.[0], date_to: range.value?.[1] }))
const margin = usePaged<MarginRow>((page, pageSize) => api.cost.margin({ ...q.value, page, page_size: pageSize }), 10)
const unreconciled = usePaged<UnreconciledRow>((page, pageSize) => api.cost.unreconciled({ page, page_size: pageSize }), 10)
const csvHref = computed(() => api.cost.csvUrl(q.value))
const summaryKey = (r: CostSummaryRow) => (group.value === 'supplier' ? `${r.supplier_code ?? r.key}` : r.key)
async function loadCost() {
  summaryLoading.value = true
  try {
    summary.value = (await api.cost.summary(group.value, q.value)).items
  } catch (e) {
    fail(e)
  } finally {
    summaryLoading.value = false
  }
  void margin.reload()
  void unreconciled.reload()
}
watch([range, group], () => tab.value === 'cost' && void loadCost())

let sourcesReady = false
let costReady = false
watch(tab, async (v) => {
  if (v === 'sources' && !sourcesReady) {
    sourcesReady = true
    void searchProducts()
    try {
      suppliers.value = (await suppliersApi.list()).items
    } catch (e) {
      toastError(e)
    }
  }
  if (v === 'cost' && !costReady) {
    costReady = true
    void loadCost()
  }
})
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
onMounted(() => {
  void jobs.load()
  // the order drawer links a held supplier order here: ?job=<id> opens that job
  const linked = route.query.job
  if (typeof linked === 'string' && UUID_RE.test(linked)) void openJob({ id: linked } as ProcJob)
  createCardsApi(client)
    .risk()
    .then((r) => (riskLevel.value = r.level))
    .catch(() => undefined) // a badge only: the Cards tab shows the real error
})
</script>

<template>
  <div class="page">
    <el-tabs v-model="tab">
      <!-- ============================================================ jobs -->
      <el-tab-pane name="jobs" :label="t('procurement.tab.jobs')">
        <div class="toolbar bar">
          <el-select v-model="stateFilter" clearable :placeholder="t('procurement.job.allStates')" style="width: 240px" :aria-label="t('procurement.job.state')" @change="jobs.reload()">
            <el-option v-for="s in STATES" :key="s" :value="s" :label="`${stateLabel(s)} (${counts[s] ?? 0})`" />
          </el-select>
          <el-button :icon="Refresh" @click="jobs.load()">{{ t('procurement.refresh') }}</el-button>
          <span v-if="counts.needs_manual" class="attn">{{ t('procurement.job.attention', { n: counts.needs_manual }) }}</span>
        </div>
        <div class="panel">
          <el-table v-loading="jobs.loading.value" :data="jobs.items.value" row-key="id" :empty-text="t('procurement.job.empty')" class="clickable" @row-click="(r: ProcJob) => openJob(r)">
            <el-table-column :label="t('procurement.job.order')" min-width="170">
              <template #default="{ row }">
                <span class="mono">{{ row.order_no }}</span>
                <span class="muted small"> · #{{ row.unit_no }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('procurement.job.state')" width="170">
              <template #default="{ row }"><el-tag size="small" :type="stateType(row.state)">{{ stateLabel(row.state) }}</el-tag></template>
            </el-table-column>
            <el-table-column :label="t('procurement.job.supplier')" min-width="150">
              <template #default="{ row }">
                <span class="mono">{{ row.supplier_code ?? '—' }}</span>
                <span v-if="row.supplier_env" class="muted small"> · {{ t(`suppliers.env.${row.supplier_env}`) }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('procurement.job.error')" min-width="170">
              <template #default="{ row }"><span class="mono small">{{ row.error_code ?? '—' }}</span></template>
            </el-table-column>
            <el-table-column :label="t('procurement.job.attempts')" prop="attempts" width="90" />
            <el-table-column :label="t('procurement.job.cost')" width="130">
              <template #default="{ row }"><span class="num">{{ money(row.cost, row.currency) }}</span></template>
            </el-table-column>
            <el-table-column :label="t('procurement.job.updated')" width="170">
              <template #default="{ row }">{{ when(row.updated_at) }}</template>
            </el-table-column>
          </el-table>
          <div class="pager">
            <el-pagination layout="total, prev, pager, next" :total="jobs.total.value" :page-size="jobs.pageSize" :current-page="jobs.page.value" @current-change="jobs.load" />
          </div>
        </div>
      </el-tab-pane>

      <!-- ============================================================ sources -->
      <el-tab-pane v-if="auth.isAdmin" name="sources" :label="t('procurement.tab.sources')">
        <p class="muted intro">{{ t('procurement.src.intro') }}</p>
        <div class="toolbar bar">
          <el-select
            v-model="productId"
            filterable
            remote
            :remote-method="searchProducts"
            :loading="searching"
            :placeholder="t('procurement.src.pickProduct')"
            :aria-label="t('procurement.src.pickProduct')"
            style="width: 320px"
          >
            <el-option v-for="p in products" :key="p.id" :value="p.id" :label="titleOf(p)" />
          </el-select>
          <el-select v-if="variants.length" v-model="variantId" :aria-label="t('procurement.src.variant')" style="width: 220px">
            <el-option v-for="v in variants" :key="v.id" :value="v.id" :label="variantName(v)" />
          </el-select>
          <template v-if="productId">
            <el-radio-group :model-value="routing" :aria-label="t('procurement.src.routing')" @change="(v: string | number | boolean | undefined) => setRouting(v as Routing)">
              <el-radio-button value="priority">{{ t('procurement.src.priority') }}</el-radio-button>
              <el-radio-button value="round_robin">{{ t('procurement.src.roundRobin') }}</el-radio-button>
            </el-radio-group>
            <el-button type="primary" :icon="Plus" @click="openSource(null)">{{ t('procurement.src.add') }}</el-button>
          </template>
        </div>
        <div class="panel">
          <el-table v-loading="sourcesLoading" :data="sources" row-key="id" :empty-text="productId ? t('procurement.src.empty') : t('procurement.src.pickFirst')">
            <el-table-column :label="t('procurement.src.order')" width="110">
              <template #default="{ $index }">
                <el-button link :icon="ArrowUp" :disabled="$index === 0" :aria-label="t('procurement.src.up')" @click="move($index, -1)" />
                <el-button link :icon="ArrowDown" :disabled="$index === sources.length - 1" :aria-label="t('procurement.src.down')" @click="move($index, 1)" />
              </template>
            </el-table-column>
            <el-table-column :label="t('procurement.job.supplier')" min-width="180">
              <template #default="{ row }">
                <span class="mono">{{ row.supplier_code }}</span>
                <span class="muted small"> · {{ t(`suppliers.env.${row.supplier_env}`) }}</span>
                <div v-if="!row.supplier_enabled || row.supplier_breaker_open" class="warn small">
                  {{ row.supplier_breaker_open ? t('procurement.src.breakerOpen') : t('procurement.src.supplierOff') }}
                </div>
              </template>
            </el-table-column>
            <el-table-column :label="t('procurement.src.plan')" min-width="150">
              <template #default="{ row }">
                <template v-if="row.kind === 'vcc'">
                  <span class="mono">{{ row.params.product_code }}</span>
                  <span class="muted small"> · {{ row.params.init_amount }}<template v-if="row.params.recharge_amount"> + {{ row.params.recharge_amount }}</template> USD</span>
                </template>
                <template v-else>
                  <span v-if="row.kind === 'direct'" class="mono">{{ row.params.product }}/</span>
                  <span class="mono">{{ row.params.plan }}</span>
                  <span v-if="row.kind === 'direct' && row.params.card_mode === 'auto_existing'" class="muted small"> · {{ t('procurement.src.cardModeAuto') }}</span>
                  <span v-if="row.kind === 'direct' && row.params.card_id" class="muted small"> · {{ t('procurement.src.fundingCardShort', { id: row.params.card_id }) }}</span>
                  <span v-if="row.params.payment_country" class="muted small"> · {{ row.params.payment_country }}</span>
                </template>
              </template>
            </el-table-column>
            <el-table-column :label="t('procurement.src.cost')" width="130">
              <template #default="{ row }"><span class="num">{{ money(row.cost_estimate, row.currency) }}</span></template>
            </el-table-column>
            <el-table-column :label="t('procurement.src.availability')" width="180">
              <template #default="{ row }">
                <el-tag size="small" :type="row.last_check_ok == null ? 'info' : row.last_check_ok ? 'success' : 'danger'">
                  {{ row.last_check_ok == null ? t('procurement.src.unchecked') : row.last_check_ok ? t('procurement.src.ok') : t('procurement.src.notOk') }}
                </el-tag>
                <div class="muted small">{{ when(row.last_check_at) }}</div>
              </template>
            </el-table-column>
            <el-table-column :label="t('suppliers.col.enabled')" width="100">
              <template #default="{ row }">
                <el-switch :model-value="row.enabled" :aria-label="t('suppliers.col.enabled')" @change="(v: string | number | boolean) => toggleSource(row as ProcSource, !!v)" />
              </template>
            </el-table-column>
            <el-table-column :label="t('common.actions')" width="200" fixed="right">
              <template #default="{ row }">
                <el-button link type="primary" :loading="checking === row.id" :disabled="!!checking && checking !== row.id" @click="check(row as ProcSource)">{{ t('procurement.src.check') }}</el-button>
                <el-button link type="primary" @click="openSource(row as ProcSource)">{{ t('common.edit') }}</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ============================================================ cost -->
      <el-tab-pane v-if="auth.isAdmin" name="cost" :label="t('procurement.tab.cost')">
        <div class="toolbar bar">
          <el-date-picker v-model="range" type="daterange" value-format="YYYY-MM-DD" :clearable="false" unlink-panels :aria-label="t('procurement.cost.range')" />
          <el-radio-group v-model="group" :aria-label="t('procurement.cost.groupBy')">
            <el-radio-button value="day">{{ t('procurement.cost.byDay') }}</el-radio-button>
            <el-radio-button value="supplier">{{ t('procurement.cost.bySupplier') }}</el-radio-button>
            <el-radio-button value="type">{{ t('procurement.cost.byType') }}</el-radio-button>
          </el-radio-group>
          <a :href="csvHref" download class="csv"><el-button :icon="Download">{{ t('procurement.cost.csv') }}</el-button></a>
        </div>
        <div class="panel">
          <h2 class="h">{{ t('procurement.cost.ledger') }}</h2>
          <el-table v-loading="summaryLoading" :data="summary" :empty-text="t('procurement.cost.empty')" max-height="420">
            <el-table-column :label="t(`procurement.cost.key.${group}`)" min-width="160">
              <template #default="{ row }"><span class="mono">{{ summaryKey(row as CostSummaryRow) }}</span></template>
            </el-table-column>
            <el-table-column :label="t('procurement.cost.rows')" prop="count" width="90" />
            <el-table-column :label="t('procurement.cost.debit')" width="150"><template #default="{ row }"><span class="num">{{ money(row.debit, row.currency) }}</span></template></el-table-column>
            <el-table-column :label="t('procurement.cost.credit')" width="150"><template #default="{ row }"><span class="num">{{ money(row.credit, row.currency) }}</span></template></el-table-column>
            <el-table-column :label="t('procurement.cost.net')" width="150"><template #default="{ row }"><span class="num strong">{{ money(row.net, row.currency) }}</span></template></el-table-column>
          </el-table>
        </div>
        <div class="panel gap">
          <h2 class="h">{{ t('procurement.cost.margin') }}</h2>
          <el-table v-loading="margin.loading.value" :data="margin.items.value" row-key="order_id" :empty-text="t('procurement.cost.empty')">
            <el-table-column :label="t('procurement.job.order')" min-width="160"><template #default="{ row }"><span class="mono">{{ row.order_no }}</span></template></el-table-column>
            <el-table-column :label="t('procurement.cost.paidAt')" width="170"><template #default="{ row }">{{ when(row.paid_at) }}</template></el-table-column>
            <el-table-column :label="t('procurement.cost.revenue')" width="140"><template #default="{ row }"><span class="num">{{ money(row.revenue, row.revenue_currency) }}</span></template></el-table-column>
            <el-table-column :label="t('procurement.job.cost')" min-width="170">
              <template #default="{ row }">
                <div v-for="c in (row as MarginRow).cost" :key="c.currency + c.source" class="num">
                  {{ money(c.amount, c.currency) }} <span class="muted small">{{ t(`procurement.cost.src.${c.source}`) }}</span>
                </div>
                <span v-if="!(row as MarginRow).cost.length" class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('procurement.cost.marginCol')" width="150">
              <template #default="{ row }">
                <span v-if="row.margin" class="num strong">{{ money(row.margin.amount, row.margin.currency) }}</span>
                <el-tooltip v-else :content="t('procurement.cost.noMargin')"><span class="muted">—</span></el-tooltip>
              </template>
            </el-table-column>
          </el-table>
          <div class="pager">
            <el-pagination layout="total, prev, pager, next" :total="margin.total.value" :page-size="margin.pageSize" :current-page="margin.page.value" @current-change="margin.load" />
          </div>
        </div>
        <div class="panel gap">
          <h2 class="h">{{ t('procurement.cost.unreconciled') }}</h2>
          <p class="muted small">{{ t('procurement.cost.unreconciledHint') }}</p>
          <el-table v-loading="unreconciled.loading.value" :data="unreconciled.items.value" row-key="id" :empty-text="t('procurement.cost.noneUnreconciled')">
            <el-table-column :label="t('procurement.cost.at')" width="170"><template #default="{ row }">{{ when(row.occurred_at) }}</template></el-table-column>
            <el-table-column :label="t('procurement.job.supplier')" width="150"><template #default="{ row }"><span class="mono">{{ row.supplier_code }}</span></template></el-table-column>
            <el-table-column :label="t('procurement.cost.key.type')" min-width="150"><template #default="{ row }"><span class="mono">{{ row.type }}</span></template></el-table-column>
            <el-table-column :label="t('procurement.cost.amount')" width="150"><template #default="{ row }"><span class="num">{{ money(row.amount, row.currency) }}</span></template></el-table-column>
            <el-table-column :label="t('procurement.cost.ref')" min-width="150">
              <template #default="{ row }"><span class="mono small">{{ row.ref_id ?? row.external_id }}</span><span v-if="row.card_mask" class="muted small"> · {{ row.card_mask }}</span></template>
            </el-table-column>
          </el-table>
          <div class="pager">
            <el-pagination layout="total, prev, pager, next" :total="unreconciled.total.value" :page-size="unreconciled.pageSize" :current-page="unreconciled.page.value" @current-change="unreconciled.load" />
          </div>
        </div>
      </el-tab-pane>
      <el-tab-pane name="cards" lazy>
        <template #label>
          {{ t('procurement.tab.cards') }}
          <el-badge v-if="riskLevel !== 'ok'" is-dot :type="riskLevel === 'critical' ? 'danger' : 'warning'" class="tab-dot" :title="t(`cardPool.riskDot.${riskLevel}`)" />
          <span v-if="riskLevel !== 'ok'" class="sr-only">{{ t(`cardPool.riskDot.${riskLevel}`) }}</span>
        </template>
        <FundingPanel />
        <CardsPanel @risk="(l) => (riskLevel = l)" />
      </el-tab-pane>
      <el-tab-pane v-if="auth.isAdmin" name="pricing" :label="t('procurement.tab.pricing')" lazy>
        <PricingPanel />
      </el-tab-pane>
      <el-tab-pane v-if="auth.isAdmin" name="regions" :label="t('procurement.tab.regions')" lazy>
        <RegionsPanel />
      </el-tab-pane>
      <el-tab-pane name="redeem" :label="t('procurement.tab.redeem')" lazy>
        <RedeemPanel />
      </el-tab-pane>
    </el-tabs>

    <!-- job detail -->
    <el-drawer v-model="drawer" :title="detail ? t('procurement.job.title', { no: detail.order_no, unit: detail.unit_no }) : ''" size="min(720px, 96vw)">
      <div v-loading="detailLoading" class="detail">
        <template v-if="detail">
          <div class="actions">
            <el-button v-if="can('requery')" :loading="acting === 'requery'" @click="run('requery')">{{ t('procurement.act.requery') }}</el-button>
            <el-button v-if="can('retry')" :loading="acting === 'retry'" @click="run('retry')">{{ t('procurement.act.retry') }}</el-button>
            <el-button v-if="can('switch')" :loading="acting === 'switch'" @click="run('switch')">{{ t('procurement.act.switch') }}</el-button>
            <el-button v-if="can('fulfil')" type="primary" @click="run('fulfil')">{{ t('procurement.act.fulfil') }}</el-button>
            <el-button v-if="can('fail')" type="danger" plain @click="run('fail')">{{ t('procurement.act.fail') }}</el-button>
            <el-button v-if="can('void')" type="danger" plain @click="run('void')">{{ t('procurement.act.void') }}</el-button>
          </div>
          <el-alert v-if="detail.charged_possible && !['fulfilled', 'voided'].includes(detail.state)" type="warning" :closable="false" show-icon :title="t('procurement.job.maybeCharged')" class="gap" />
          <el-alert v-if="detail.recovering" type="info" :closable="false" show-icon :title="t('procurement.job.recovering')" class="gap" />
          <el-descriptions :column="1" border size="small" class="gap">
            <el-descriptions-item :label="t('procurement.job.state')">
              <el-tag size="small" :type="stateType(detail.state)">{{ stateLabel(detail.state) }}</el-tag>
              <span v-if="detail.error_code" class="mono small"> {{ detail.error_code }}</span>
              <div v-if="detail.error_code && te(`procurement.reason.${detail.error_code}`)" class="small warn">{{ t(`procurement.reason.${detail.error_code}`) }}</div>
            </el-descriptions-item>
            <el-descriptions-item :label="t('procurement.job.orderStatus')">{{ detail.order.status }} · ×{{ detail.order.qty }}</el-descriptions-item>
            <el-descriptions-item :label="t('procurement.job.supplier')">
              <span class="mono">{{ detail.supplier_code ?? '—' }}</span>
              <span v-if="detail.supplier_env" class="muted small"> · {{ t(`suppliers.env.${detail.supplier_env}`) }}</span>
            </el-descriptions-item>
            <el-descriptions-item :label="t('procurement.job.upstream')">
              <span class="mono">{{ detail.upstream_ref ?? '—' }}</span>
              <span v-if="detail.card_mask" class="mono muted"> · {{ detail.card_mask }}</span>
              <span v-if="detail.account_mask" class="muted"> · {{ detail.account_mask }}</span>
              <span v-if="detail.kind === 'direct' && detail.plan" class="mono muted"> · {{ detail.plan }}</span>
              <span v-if="detail.upstream_status" class="muted small"> · {{ detail.upstream_status }}</span>
            </el-descriptions-item>
            <el-descriptions-item :label="t('procurement.job.code')">{{ detail.has_code ? t('procurement.job.codeStored') : t('procurement.job.codeNone') }}</el-descriptions-item>
            <el-descriptions-item :label="t('procurement.job.attempts')">{{ detail.attempts }} · {{ t('procurement.job.sourcesTried', { n: detail.tried_offer_ids.length }) }}</el-descriptions-item>
            <el-descriptions-item :label="t('procurement.job.cost')"><span class="num">{{ money(detail.cost, detail.currency) }}</span></el-descriptions-item>
            <el-descriptions-item :label="t('procurement.job.nextPoll')">{{ when(detail.next_poll_at) }}</el-descriptions-item>
          </el-descriptions>

          <template v-if="detail.ledger.length">
            <h2 class="h gap">{{ t('procurement.cost.ledger') }}</h2>
            <div v-for="l in detail.ledger" :key="l.id" class="row">
              <span class="mono small">{{ l.type }}</span><span class="num">{{ money(l.amount, l.currency) }}</span><span class="muted small">{{ when(l.occurred_at) }}</span>
            </div>
          </template>

          <template v-if="detail.buyer_progress">
            <h2 class="h gap">{{ t('procurement.job.supplierTimeline') }}</h2>
            <p class="small">
              <span class="muted">{{ t('procurement.job.buyerSees') }}:</span>
              <b> {{ detail.buyer_progress.step }} · {{ detail.buyer_progress.percent }}%</b>
              <span v-if="detail.buyer_progress.reason" class="muted"> · {{ detail.buyer_progress.reason }}</span>
            </p>
            <p v-if="!detail.supplier_timeline?.length" class="muted small">{{ t('procurement.job.supplierTimelineEmpty') }}</p>
            <el-timeline v-else>
              <el-timeline-item v-for="(e, i) in detail.supplier_timeline" :key="i" :timestamp="when(e.at)" :type="e.src === 'webhook' ? 'success' : 'info'" placement="top">
                <span class="muted small">{{ t(`procurement.job.src.${e.src === 'webhook' ? 'webhook' : 'poll'}`) }}</span>
                <div v-if="e.status || e.stage || e.payment_state || e.funding_state" class="mono small">
                  <span v-if="e.status">{{ t('procurement.job.vStatus') }}={{ e.status }} </span>
                  <span v-if="e.stage">{{ t('procurement.job.vStage') }}={{ e.stage }} </span>
                  <span v-if="e.payment_state">{{ t('procurement.job.vPayment') }}={{ e.payment_state }} </span>
                  <span v-if="e.funding_state">{{ t('procurement.job.vFunding') }}={{ e.funding_state }}</span>
                </div>
                <div v-if="e.code || e.step || e.cat" class="mono small">{{ [e.cat, e.step, e.code].filter(Boolean).join(' / ') }}<span v-if="e.to"> → {{ e.to }}</span><span v-if="e.fact"> ({{ e.fact }})</span></div>
                <div v-if="e.msg" class="small pre">{{ e.msg }}</div>
              </el-timeline-item>
            </el-timeline>
          </template>

          <h2 class="h gap">{{ t('procurement.job.timeline') }}</h2>
          <el-timeline>
            <el-timeline-item v-for="(e, i) in detail.timeline" :key="i" :timestamp="when(e.at)" :type="e.severity === 'critical' ? 'danger' : e.severity === 'warning' ? 'warning' : 'primary'" placement="top">
              <span class="mono small">{{ e.action }}</span>
              <span v-if="e.actor_role" class="muted small"> · {{ e.actor_role }}</span>
              <div v-if="e.error_code" class="mono small warn">{{ e.error_code }}</div>
              <div v-if="e.reason" class="small pre">{{ e.reason }}</div>
            </el-timeline-item>
          </el-timeline>
        </template>
      </div>
    </el-drawer>

    <!-- manual fulfil / void / fail -->
    <el-dialog v-model="dlg.open" :title="t(`procurement.act.${dlg.action}`)" width="min(520px, 96vw)" destroy-on-close @closed="dlg.code = ''">
      <el-form label-position="top" @submit.prevent="submitDlg">
        <p class="muted small">{{ t(`procurement.hint.${dlg.action}`) }}</p>
        <el-alert v-if="dlg.action === 'fulfil' && detail?.charged_possible" type="warning" :closable="false" show-icon :title="t('procurement.hint.fulfilMaybeSent')" class="warnbox" />
        <p v-if="dlg.action === 'fulfil' && detail?.kind === 'direct'" class="muted small">{{ t('procurement.hint.fulfilDirect') }}</p>
        <section v-if="dlg.action === 'fulfil' && detail?.kind === 'direct'" class="charge" :aria-busy="chk.loading" :aria-label="t('procurement.charge.title')">
          <div class="charge-head">
            <strong>{{ t('procurement.charge.title') }}</strong>
            <el-button size="small" :loading="chk.loading" @click="detail && loadCharge(detail.id)">{{ t('procurement.charge.run') }}</el-button>
          </div>
          <el-alert v-if="chk.failed" type="error" :closable="false" show-icon :title="t('procurement.charge.failed')" />
          <template v-if="chk.data">
            <p class="small">
              <el-tag :type="chk.data.payment.paid ? 'success' : 'danger'" size="small">{{ chk.data.payment.paid ? t('procurement.charge.paid') : t('procurement.charge.unpaid') }}</el-tag>
              <span class="muted"> {{ stateText('procurement.charge.order_status', chk.data.payment.order_status) }}<template v-if="chk.data.payment.paid_at"> · {{ chk.data.payment.paid_at.replace('T', ' ').slice(0, 16) }} UTC</template></span>
            </p>
            <template v-if="chk.data.supplier">
              <p class="small">
                {{ t('procurement.charge.order') }}:
                <el-tag :type="chk.data.supplier.state === 'completed' ? 'danger' : chk.data.supplier.state === 'pending' || chk.data.supplier.state === 'unknown' ? 'warning' : 'info'" size="small">{{ t(`procurement.charge.state.${chk.data.supplier.state}`) }}</el-tag>
                <span v-if="chk.data.supplier.upstream_ref" class="mono muted"> #{{ chk.data.supplier.upstream_ref }}</span>
              </p>
              <p v-for="(tp, i) in chk.data.supplier.topups" :key="i" class="small muted">{{ t('procurement.charge.topup', { amount: tp.amount, card: tp.card_id, state: stateText('funding.state', tp.state) }) }}</p>
              <p class="small">
                {{ t('procurement.charge.card', { card: chk.data.supplier.card_id || '—' }) }}
                <span v-if="chk.data.supplier.job_created_at" class="muted"> · {{ t('procurement.charge.since', { at: chk.data.supplier.job_created_at.replace('T', ' ').slice(0, 16) }) }}</span>
              </p>
              <el-alert v-if="chk.data.supplier.charges === null" type="warning" :closable="false" show-icon :title="t('procurement.charge.cardUnknown')" />
              <el-alert v-else-if="aiCharges.length" type="warning" :closable="false" show-icon :title="t('procurement.charge.aiSpends', { n: aiCharges.length })" role="alert" />
              <el-table v-if="chk.data.supplier.charges?.length" :data="chk.data.supplier.charges.slice(0, 8)" size="small" class="spends">
                <el-table-column prop="time" :label="t('procurement.charge.time')" min-width="130" />
                <el-table-column :label="t('procurement.charge.amount')" width="100"><template #default="{ row }">{{ row.amount }} {{ row.currency }}</template></el-table-column>
                <el-table-column prop="merchant" :label="t('procurement.charge.merchant')" min-width="110" />
                <el-table-column prop="status" :label="t('procurement.charge.status')" width="100" />
              </el-table>
            </template>
            <el-checkbox v-model="chk.confirmed" class="confirm" aria-describedby="charge-err">{{ t('procurement.charge.confirm') }}</el-checkbox>
          </template>
        </section>
        <el-form-item v-if="dlg.action === 'fulfil' && detail?.kind !== 'direct'" :label="detail?.kind === 'vcc' ? t('procurement.job.pasteCard') : t('procurement.job.pasteCode')">
          <el-input v-model="dlg.code" type="textarea" :rows="3" maxlength="512" class="mono" spellcheck="false" autocomplete="off" :placeholder="detail?.kind === 'vcc' ? '4111111111111111----12/29----123' : ''" />
          <div v-if="detail?.kind === 'vcc'" class="muted small">{{ t('procurement.hint.pasteCard') }}</div>
        </el-form-item>
        <el-form-item v-if="dlg.action === 'fulfil' && detail?.kind === 'vcc'" :label="t('procurement.job.cardIdOptional')">
          <el-input v-model="dlg.cardId" maxlength="20" inputmode="numeric" class="mono" autocomplete="off" />
        </el-form-item>
        <p v-if="dlg.error" id="charge-err" class="sr-only" role="alert">{{ dlg.error }}</p>
        <el-form-item :label="dlg.action === 'fulfil' && detail?.kind !== 'direct' ? t('procurement.job.reasonOptional') : t('procurement.job.reason')" :error="dlg.error">
          <el-input v-model="dlg.reason" maxlength="200" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg.open = false">{{ t('common.cancel') }}</el-button>
        <el-button :type="dlg.action === 'fulfil' ? 'primary' : 'danger'" :loading="!!acting" @click="submitDlg">{{ t('common.confirm') }}</el-button>
      </template>
    </el-dialog>

    <!-- source add / edit -->
    <el-dialog v-model="srcDlg.open" :title="srcDlg.id ? t('procurement.src.edit') : t('procurement.src.add')" width="min(520px, 96vw)" destroy-on-close>
      <el-form label-position="top" @submit.prevent="saveSource">
        <el-form-item :label="t('procurement.job.supplier')" :error="srcErr.supplier">
          <el-select v-model="srcDlg.supplier_id" :disabled="!!srcDlg.id" style="width: 100%">
            <el-option v-for="s in suppliers" :key="s.id" :value="s.id" :label="`${s.label} (${s.code} · ${t(`suppliers.env.${s.env}`)})`" />
          </el-select>
        </el-form-item>
        <div class="grid">
          <template v-if="isCard">
            <el-form-item :label="t('procurement.src.productCode')" :error="srcErr.product_code">
              <el-input v-model="srcDlg.product_code" maxlength="32" class="mono" spellcheck="false" placeholder="USMAB01" />
            </el-form-item>
            <el-form-item :label="t('procurement.src.initAmount')" :error="srcErr.init_amount">
              <el-input v-model="srcDlg.init_amount" inputmode="decimal"><template #suffix>USD</template></el-input>
            </el-form-item>
            <el-form-item :label="t('procurement.src.rechargeAmount')" :error="srcErr.recharge_amount">
              <el-input v-model="srcDlg.recharge_amount" inputmode="decimal"><template #suffix>USD</template></el-input>
            </el-form-item>
            <el-form-item :label="t('procurement.src.firstName')" :error="srcErr.first_name">
              <el-input v-model="srcDlg.first_name" maxlength="32" autocomplete="off" />
            </el-form-item>
            <el-form-item :label="t('procurement.src.lastName')" :error="srcErr.last_name">
              <el-input v-model="srcDlg.last_name" maxlength="32" autocomplete="off" />
            </el-form-item>
          </template>
          <template v-else>
            <el-form-item v-if="isDirect" :label="t('procurement.src.directProduct')">
              <el-select v-model="srcDlg.direct_product" style="width: 100%">
                <el-option value="gpt" label="ChatGPT" />
                <el-option value="claude" label="Claude" />
                <el-option value="grok" label="Grok" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="isDirect" :label="t('procurement.src.cardMode')">
              <el-select v-model="srcDlg.card_mode">
                <el-option value="fixed" :label="t('procurement.src.cardModeFixed')" />
                <el-option value="auto_existing" :label="t('procurement.src.cardModeAuto')" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="isDirect && srcDlg.card_mode === 'fixed'" :label="t('procurement.src.fundingCard')" :error="srcErr.funding_card">
              <el-input v-model="srcDlg.funding_card" maxlength="12" inputmode="numeric" class="mono" autocomplete="off" />
            </el-form-item>
            <el-form-item v-if="isDirect" :label="t('procurement.src.vendorSwitch')">
              <el-switch v-model="srcDlg.vendor_switch" />
              <div class="muted small form-hint">{{ t('procurement.src.vendorSwitchHint') }}</div>
            </el-form-item>
            <el-form-item v-if="isDirect" :label="t('procurement.src.payCurrency')" :error="srcErr.pay_currency">
              <el-input v-model="srcDlg.pay_currency" maxlength="3" class="mono" placeholder="USD" />
            </el-form-item>
            <el-form-item :label="t('procurement.src.plan')" :error="srcErr.plan">
              <el-input v-model="srcDlg.plan" maxlength="48" class="mono" spellcheck="false" placeholder="plus" />
            </el-form-item>
            <el-form-item :label="t('procurement.src.country')" :error="srcErr.country">
              <el-input v-model="srcDlg.country" maxlength="2" class="mono" placeholder="US" />
            </el-form-item>
          </template>
          <el-form-item :label="t('procurement.src.cost')" :error="srcErr.cost">
            <el-input v-model="srcDlg.cost" inputmode="decimal"><template #suffix>USD</template></el-input>
          </el-form-item>
          <el-form-item :label="t('suppliers.f.priority')"><el-input-number v-model="srcDlg.priority" :min="0" :max="10000" /></el-form-item>
        </div>
        <div class="muted small">{{ isCard ? t('procurement.src.cardHint') : isDirect ? t('procurement.src.directHint') : t('procurement.src.costHint') }}</div>
        <el-form-item :label="t('suppliers.f.enabled')" class="gap"><el-switch v-model="srcDlg.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="srcDlg.open = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="srcDlg.saving" @click="saveSource">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
.bar {
  margin-bottom: 12px;
}
.form-hint {
  /* el-form-item gives its content a 32px line height: a hint needs a normal one */
  line-height: 1.4;
  margin-top: 4px;
  width: 100%;
}
.tab-dot {
  margin-left: 2px;
}
.intro {
  margin: 0 0 12px;
}
.gap {
  margin-top: 16px;
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
.warn,
.attn {
  color: var(--el-color-danger);
}
.attn {
  font-weight: 600;
}
.warnbox {
  margin-bottom: 12px;
}
.h {
  font-size: 14px;
  font-weight: 600;
  margin: 0 0 8px;
}
.h.gap {
  margin-top: 20px;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.actions .el-button {
  margin-left: 0;
}
.row {
  display: flex;
  gap: 12px;
  align-items: baseline;
  padding: 4px 0;
  border-bottom: 1px solid var(--a-line);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  column-gap: 16px;
}
.csv {
  text-decoration: none;
}
.detail {
  min-height: 120px;
}
.clickable :deep(.el-table__row) {
  cursor: pointer;
}
.charge { border: 1px solid var(--el-border-color); border-radius: 6px; padding: 10px 12px; margin-bottom: 14px; }
.charge-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 6px; }
.charge .spends { width: 100%; margin: 6px 0; }
.charge .confirm { white-space: normal; height: auto; margin-top: 6px; }
</style>
