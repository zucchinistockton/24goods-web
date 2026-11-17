<script setup lang="ts">
/**
 * issue #29 part 2 (review-v12 §21.8): our funding cards at the vendor, the schedule preview, the
 * decline-rate monitor (staff read it) and the card rules editor / failed-usage reset (admins).
 * Only ids and last four digits ever reach this page.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Refresh } from '@element-plus/icons-vue'
import { isApiError } from '@24goods/shared'
import { client } from '../api'
import {
  createCardsApi,
  type CardProduct,
  type CardPool,
  type CardRule,
  type CardSupplier,
  type RiskReport,
  type SchedulePreview,
} from '../api-procurement'
import { cardLabel, codeText, levelTag, planValid, ratePct, ruleDiff, ruleErrors, switchMismatch, usageText, type RuleForm } from '../lib/cardPool'
import { useAuthStore } from '../stores/auth'
import { confirmBox, toastError, toastOk } from '../ui'

const emit = defineEmits<{ risk: [level: 'ok' | 'warning' | 'critical'] }>()
const api = createCardsApi(client)
const auth = useAuthStore()
const { t, te } = useI18n()
const PRODUCTS: CardProduct[] = ['gpt', 'claude', 'grok']

const suppliers = ref<CardSupplier[]>([])
const supplierId = ref('')
const product = ref<CardProduct>('gpt')
const pool = ref<CardPool | null>(null)
const risk = ref<RiskReport | null>(null)
const loading = ref(false)
const poolError = ref(false)
const suppliersError = ref(false)
const suppliersLoaded = ref(false)
const riskError = ref(false)
const selected = ref<number[]>([])

function fail(e: unknown) {
  if (isApiError(e) && te(`cardPool.err.${e.code}`)) ElMessage.error({ message: t(`cardPool.err.${e.code}`), grouping: true })
  else toastError(e)
}
const sid = () => supplierId.value || undefined

async function loadRisk() {
  riskError.value = false
  try {
    risk.value = await api.risk(sid())
    emit('risk', risk.value.level)
  } catch (e) {
    risk.value = null
    riskError.value = true
    fail(e)
  }
}
async function loadPool() {
  loading.value = true
  poolError.value = false
  selected.value = []
  try {
    pool.value = await api.pool(product.value, sid())
  } catch (e) {
    pool.value = null
    poolError.value = true
    if (!(isApiError(e) && e.code === 'supplier_unavailable')) fail(e)
  } finally {
    loading.value = false
  }
}
async function reload() {
  await Promise.all([loadPool(), loadRisk()])
}
onMounted(async () => {
  try {
    suppliers.value = (await api.suppliers()).items
    if (suppliers.value.length === 1) supplierId.value = suppliers.value[0].id
    suppliersLoaded.value = true
  } catch (e) {
    suppliersError.value = true
    fail(e)
  }
  await reload()
  if (auth.isAdmin) await loadRules()
})
watch([supplierId, product], () => void reload())
watch(supplierId, () => {
  if (auth.isAdmin) void loadRules()
})

const asCard = (row: unknown) => row as CardPool['cards'][number]
const flags = (c: CardPool['cards'][number]) =>
  [c.usage.cooling && 'cooling', c.usage.busy && 'busy', c.usage.locked && 'locked'].filter(Boolean) as string[]

// ------------------------------------------------------------------ schedule preview
const plan = ref('')
const planError = ref(false)
const preview = ref<SchedulePreview | null>(null)
const previewing = ref(false)
async function runPreview() {
  planError.value = !planValid(plan.value)
  if (planError.value) return
  previewing.value = true
  try {
    preview.value = await api.schedule(product.value, plan.value.trim(), sid())
  } catch (e) {
    preview.value = null
    fail(e)
  } finally {
    previewing.value = false
  }
}
const skipText = (r: string | null) => (r ? codeText('cardPool.skip', r, t, te) : t('cardPool.candidateOk'))
const envText = (env: string) => (te(`cardPool.envLabel.${env}`) ? t(`cardPool.envLabel.${env}`) : env)
const statusText = (s: string) => codeText('cardPool.cardStatus', s, t, te)
const levelWord = (l: string) => t(`cardPool.risk.st.${l}`)

// ------------------------------------------------------------------ rules (admins)
const rules = ref<Partial<Record<CardProduct, CardRule>>>({})
const costSwitches = ref(2)
const forms = reactive<Record<string, RuleForm>>({})
const loaded = reactive<Record<string, RuleForm>>({})
const rulesError = ref(false)
const saving = ref('')
const toForm = (r: CardRule): RuleForm => ({
  count_failures: r.count_failures ?? true,
  light_max_uses: r.light_max_uses ?? 5,
  pro20_max_uses: r.pro20_max_uses ?? 3,
  select_mode: r.select_mode ?? 'default',
  auto_switch_on_fail: r.auto_switch_on_fail ?? true,
  max_auto_switches: r.max_auto_switches ?? 2,
  strict_select: r.strict_select ?? false,
})
async function loadRules() {
  rulesError.value = false
  try {
    const r = await api.rules(sid())
    rules.value = r.rules
    costSwitches.value = r.cost_model_max_auto_switches
    for (const p of PRODUCTS) {
      const rule = r.rules[p]
      if (rule) {
        forms[p] = toForm(rule)
        loaded[p] = toForm(rule)
      }
    }
  } catch (e) {
    rulesError.value = true
    if (!(isApiError(e) && e.code === 'supplier_unavailable')) fail(e)
  }
}
const ruleErr = (p: CardProduct) => ruleErrors(forms[p])
const mismatch = (p: CardProduct) => forms[p] != null && switchMismatch(forms[p], costSwitches.value)
const prod = computed(() => pool.value?.supplier.env === 'production' || suppliers.value.find((s) => s.id === supplierId.value)?.env === 'production')
async function saveRule(p: CardProduct) {
  if (Object.keys(ruleErr(p)).length) return ElMessage.error(t('cardPool.rules.range'))
  const patch = ruleDiff(loaded[p], forms[p])
  if (!Object.keys(patch).length) return ElMessage.info(t('cardPool.rules.nothing'))
  if (!(await confirmBox(t('cardPool.rules.confirm', { product: t(`cardPool.product.${p}`) }), t('cardPool.rules.save')))) return
  saving.value = p
  try {
    await api.saveRule(p, patch, sid())
    toastOk(t('cardPool.rules.saved'))
    await loadRules()
    void loadPool()
  } catch (e) {
    fail(e)
  } finally {
    saving.value = ''
  }
}

// ------------------------------------------------------------------ reset failed usage (admins)
const resetting = ref(false)
async function resetFailed() {
  if (!selected.value.length) return
  const ok = await confirmBox(t('cardPool.reset.confirm', { n: selected.value.length }), t('cardPool.reset.button'))
  if (!ok) return
  resetting.value = true
  try {
    await api.resetFailed(selected.value, sid())
    toastOk(t('cardPool.reset.done'))
    await loadPool()
  } catch (e) {
    fail(e)
  } finally {
    resetting.value = false
  }
}
const onSelect = (rows: unknown[]) => (selected.value = rows.map((r) => asCard(r).id))
</script>

<template>
  <div v-loading="loading" class="panel">
    <div class="bar">
      <el-select v-if="suppliers.length > 1" v-model="supplierId" :placeholder="t('cardPool.supplier')" :aria-label="t('cardPool.supplier')" clearable style="width: 220px">
        <el-option v-for="s in suppliers" :key="s.id" :value="s.id" :label="`${s.label} (${envText(s.env)})`" />
      </el-select>
      <el-select v-model="product" :aria-label="t('cardPool.productLabel')" style="width: 150px">
        <el-option v-for="p in PRODUCTS" :key="p" :value="p" :label="t(`cardPool.product.${p}`)" />
      </el-select>
      <el-button :icon="Refresh" @click="reload">{{ t('cardPool.refresh') }}</el-button>
    </div>
    <el-alert v-if="suppliersError" type="error" :closable="false" show-icon :title="t('cardPool.loadFailed')" />
    <el-alert v-else-if="suppliersLoaded && !suppliers.length" type="info" :closable="false" show-icon :title="t('cardPool.noSupplier')" />

    <!-- ===================================================== risk monitor -->
    <h2 class="h">{{ t('cardPool.risk.title') }}</h2>
    <p class="muted small hint">{{ t('cardPool.risk.hint') }}</p>
    <el-alert v-if="riskError" type="error" :closable="false" show-icon :title="t('cardPool.loadFailed')" class="gap" role="alert" />
    <template v-if="risk">
      <el-alert
        v-if="risk.level !== 'ok'"
        :type="risk.level === 'critical' ? 'error' : 'warning'"
        :closable="false"
        show-icon
        :title="t(`cardPool.risk.level.${risk.level}`)"
        class="gap"
      />
      <el-table :data="(['7', '30'] as const).map((d) => ({ days: d, ...risk!.windows[d].overall }))" size="small" class="gap">
        <el-table-column :label="t('cardPool.risk.window')" width="110"><template #default="{ row }">{{ t('cardPool.risk.days', { n: row.days }) }}</template></el-table-column>
        <el-table-column prop="sent" :label="t('cardPool.risk.sent')" width="90" />
        <el-table-column prop="declined" :label="t('cardPool.risk.declined')" width="100" />
        <el-table-column prop="in_flight" :label="t('cardPool.risk.inFlight')" width="100" />
        <el-table-column :label="t('cardPool.risk.rate')" width="130"><template #default="{ row }">{{ ratePct(row.rate) }}</template></el-table-column>
        <el-table-column :label="t('cardPool.risk.status')"><template #default="{ row }"><el-tag size="small" :type="levelTag(row.level)">{{ t(`cardPool.risk.st.${row.level}`) }}</el-tag></template></el-table-column>
      </el-table>
      <p class="muted small">
        {{ t('cardPool.risk.thresholds', { t1: ratePct(risk.thresholds.tier1), t2: ratePct(risk.thresholds.tier2), min: risk.thresholds.min_sample }) }}
      </p>
      <p class="muted small">{{ t(risk.chargebacks.available ? 'cardPool.risk.cbYes' : 'cardPool.risk.cbNo') }}</p>
      <el-collapse v-if="risk.windows['30'].cards.length" class="gap">
        <el-collapse-item :title="t('cardPool.risk.perCard')" name="cards">
          <el-table :data="risk.windows['30'].cards" size="small">
            <el-table-column :label="t('cardPool.card')" width="120"><template #default="{ row }"><span class="mono">#{{ row.card_id }}</span></template></el-table-column>
            <el-table-column prop="sent" :label="t('cardPool.risk.sent')" width="90" />
            <el-table-column prop="declined" :label="t('cardPool.risk.declined')" width="100" />
            <el-table-column :label="t('cardPool.risk.rate')" width="130"><template #default="{ row }">{{ ratePct(row.rate) }}</template></el-table-column>
            <el-table-column :label="t('cardPool.risk.status')"><template #default="{ row }"><el-tag size="small" :type="levelTag(row.level)">{{ t(`cardPool.risk.st.${row.level}`) }}</el-tag></template></el-table-column>
          </el-table>
        </el-collapse-item>
      </el-collapse>
    </template>

    <!-- ===================================================== pool -->
    <h2 class="h gap">{{ t('cardPool.pool.title') }}</h2>
    <p class="muted small hint">{{ t('cardPool.pool.hint') }}</p>
    <el-alert v-if="poolError" type="warning" :closable="false" show-icon :title="t('cardPool.pool.down')" />
    <template v-if="pool">
      <p class="muted small">{{ t('cardPool.pool.ours', { sent: pool.ours_30d.sent, declined: pool.ours_30d.declined, flight: pool.ours_30d.in_flight }) }}</p>
      <el-table :data="pool.cards" row-key="id" size="small" :empty-text="t('cardPool.pool.empty')" @selection-change="onSelect">
        <el-table-column v-if="auth.isAdmin" type="selection" width="44" />
        <el-table-column :label="t('cardPool.card')" min-width="130"><template #default="{ row }"><span class="mono">{{ cardLabel(asCard(row).id, asCard(row).last4) }}</span></template></el-table-column>
        <el-table-column :label="t('cardPool.status')" width="100"><template #default="{ row }"><el-tag size="small" :type="row.status === 'ACTIVE' ? 'success' : 'warning'">{{ statusText(row.status) }}</el-tag></template></el-table-column>
        <el-table-column :label="t('cardPool.balance')" width="110" align="right"><template #default="{ row }">{{ row.balance ?? '—' }}</template></el-table-column>
        <el-table-column :label="t('cardPool.light')" min-width="120"><template #default="{ row }">{{ usageText(asCard(row).usage.light, t) }}</template></el-table-column>
        <el-table-column :label="t('cardPool.pro20')" min-width="120"><template #default="{ row }">{{ usageText(asCard(row).usage.pro20, t) }}</template></el-table-column>
        <el-table-column :label="t('cardPool.failed')" width="90"><template #default="{ row }">{{ asCard(row).usage.light?.failed ?? asCard(row).usage.failure_streak ?? '—' }}</template></el-table-column>
        <el-table-column :label="t('cardPool.state')" min-width="140">
          <template #default="{ row }">
            <el-tag v-for="f in flags(asCard(row))" :key="f" size="small" type="warning" class="tag">{{ t(`cardPool.flag.${f}`) }}</el-tag>
            <span v-if="!flags(asCard(row)).length && asCard(row).usage.readable !== false" class="muted">—</span>
            <el-tag v-if="asCard(row).usage.readable === false" size="small" type="info">{{ t('cardPool.flag.unreadable') }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('cardPool.ours')" min-width="170">
          <template #default="{ row }">
            <span class="num">{{ asCard(row).ours_30d.sent }} / {{ asCard(row).ours_30d.declined }} / {{ asCard(row).ours_30d.in_flight }}</span>
            <el-tag v-if="asCard(row).ours_30d.level !== 'ok'" size="small" :type="levelTag(asCard(row).ours_30d.level)" class="tag" :title="t('cardPool.oursLevel', { level: levelWord(asCard(row).ours_30d.level) })">{{ ratePct(asCard(row).ours_30d.rate) }}<span class="sr-only"> {{ t('cardPool.oursLevel', { level: levelWord(asCard(row).ours_30d.level) }) }}</span></el-tag>
          </template>
        </el-table-column>
      </el-table>
      <p class="muted small">{{ t('cardPool.pool.oursHint') }}</p>
      <p v-if="pool.truncated" class="muted small">{{ t('cardPool.pool.truncated') }}</p>
      <p v-if="pool.excluded_buyer_cards" class="muted small">{{ t('cardPool.pool.excluded', { n: pool.excluded_buyer_cards }) }}</p>
      <div v-if="auth.isAdmin" class="gap">
        <el-button type="warning" plain :disabled="!selected.length" :loading="resetting" @click="resetFailed">{{ t('cardPool.reset.button') }} ({{ selected.length }})</el-button>
        <span class="muted small hint-inline">{{ t('cardPool.reset.hint') }}</span>
      </div>
    </template>

    <!-- ===================================================== schedule -->
    <h2 class="h gap">{{ t('cardPool.schedule.title') }}</h2>
    <p class="muted small hint">{{ t('cardPool.schedule.hint') }}</p>
    <div class="bar">
      <el-input v-model="plan" maxlength="48" class="mono" style="width: 220px" :placeholder="t('cardPool.schedule.plan')" :aria-label="t('cardPool.schedule.plan')" :aria-invalid="planError" @input="planError = false" @keyup.enter="runPreview" />
      <el-button :loading="previewing" @click="runPreview">{{ t('cardPool.schedule.run') }}</el-button>
    </div>
    <p v-if="planError" class="warn small" role="alert">{{ t('cardPool.planInvalid') }}</p>
    <template v-if="preview">
      <p>
        <strong>{{ t('cardPool.schedule.pick') }}:</strong>
        <span v-if="preview.pick != null" class="mono"> #{{ preview.pick }}</span>
        <span v-else class="warn"> {{ t('cardPool.schedule.none') }}</span>
      </p>
      <el-table :data="preview.candidates" size="small" :empty-text="t('cardPool.schedule.noCandidates')">
        <el-table-column :label="t('cardPool.card')" width="130"><template #default="{ row }"><span class="mono">#{{ row.card_id }}</span></template></el-table-column>
        <el-table-column :label="t('cardPool.schedule.skipReason')"><template #default="{ row }"><span :class="{ muted: !row.skip_reason }">{{ skipText(row.skip_reason) }}</span></template></el-table-column>
      </el-table>
    </template>

    <!-- ===================================================== rules (admins) -->
    <template v-if="auth.isAdmin">
      <h2 class="h gap">{{ t('cardPool.rules.title') }}</h2>
      <p class="muted small hint">{{ t('cardPool.rules.hint') }}</p>
      <el-alert v-if="prod" type="warning" :closable="false" show-icon :title="t('cardPool.rules.production')" class="gap" />
      <el-alert v-if="rulesError" type="warning" :closable="false" show-icon :title="t('cardPool.rules.down')" class="gap" />
      <el-row :gutter="16">
        <el-col v-for="p in PRODUCTS.filter((x) => forms[x])" :key="p" :xs="24" :md="8">
          <el-card shadow="never" class="rule">
            <template #header><strong>{{ t(`cardPool.product.${p}`) }}</strong><el-tag v-if="rules[p]?.is_default" size="small" type="info" class="tag">{{ t('cardPool.rules.default') }}</el-tag></template>
            <el-form label-position="top" @submit.prevent="saveRule(p)">
              <el-form-item :label="t('cardPool.rules.countFailures')"><el-switch v-model="forms[p].count_failures" /></el-form-item>
              <el-form-item :label="t('cardPool.rules.lightMax')" :error="ruleErr(p).light_max_uses ? t('cardPool.rules.range') : ''"><el-input-number v-model="forms[p].light_max_uses" :min="0" :max="1000" :step="1" /></el-form-item>
              <el-form-item :label="t('cardPool.rules.pro20Max')" :error="ruleErr(p).pro20_max_uses ? t('cardPool.rules.range') : ''"><el-input-number v-model="forms[p].pro20_max_uses" :min="0" :max="1000" :step="1" /></el-form-item>
              <el-form-item :label="t('cardPool.rules.selectMode')">
                <el-select v-model="forms[p].select_mode">
                  <el-option v-for="m in ['default', 'lowest_usage', 'newest']" :key="m" :value="m" :label="t(`cardPool.rules.mode.${m}`)" />
                </el-select>
              </el-form-item>
              <el-form-item :label="t('cardPool.rules.autoSwitch')"><el-switch v-model="forms[p].auto_switch_on_fail" /></el-form-item>
              <el-form-item :label="t('cardPool.rules.maxSwitches')" :error="ruleErr(p).max_auto_switches ? t('cardPool.rules.range') : ''">
                <el-input-number v-model="forms[p].max_auto_switches" :min="0" :max="10" :step="1" />
                <div class="form-hint" :class="{ warn: mismatch(p) }">{{ t(mismatch(p) ? 'cardPool.rules.mismatch' : 'cardPool.rules.matches', { n: costSwitches }) }}</div>
              </el-form-item>
              <el-form-item :label="t('cardPool.rules.strict')"><el-switch v-model="forms[p].strict_select" /></el-form-item>
              <el-button type="primary" native-type="submit" :loading="saving === p">{{ t('cardPool.rules.save') }}</el-button>
            </el-form>
          </el-card>
        </el-col>
      </el-row>
    </template>
  </div>
</template>

<style scoped>
.bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}
.gap {
  margin-top: 16px;
}
.h {
  font-size: 15px;
  margin: 16px 0 4px;
}
.hint {
  margin: 0 0 8px;
}
.hint-inline {
  margin-left: 8px;
}
.tag {
  margin-left: 4px;
}
.num {
  font-variant-numeric: tabular-nums;
}
.warn {
  color: var(--el-color-danger);
}
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
.rule {
  margin-top: 12px;
}
.form-hint {
  /* el-form-item inherits a 32px line height: hints get a normal one */
  line-height: 1.4;
  font-size: 12px;
  margin-top: 4px;
  width: 100%;
}
</style>
