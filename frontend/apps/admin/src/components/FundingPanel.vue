<script setup lang="ts">
/**
 * Card funding (ADR-048, ADR-049): automatic top-ups of direct orders' funding cards from the
 * supplier wallet, automatic new cards, leftover sweeps and the wallet alert. Staff read; an administrator changes the settings with a fresh authenticator code.
 * Card ids and amounts only: no card number exists here.
 */
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Refresh } from '@element-plus/icons-vue'
import { isApiError } from '@24goods/shared'
import { client } from '../api'
import { BIN_RE, USD_RE, createFundingApi, type FundingTopup, type FundingView } from '../api-procurement'
import { useAuthStore } from '../stores/auth'
import { toastError, toastOk } from '../ui'

const api = createFundingApi(client)
const auth = useAuthStore()
const { t, te } = useI18n()

const view = ref<FundingView | null>(null)
const loading = ref(false)
const loadError = ref(false)
const form = reactive({
  auto_recharge: false,
  auto_open_cards: true,
  sweep_leftover: true,
  upgrade_pay_difference: false,
  cancel_renewal_before_upgrade: false,
  wallet_alert_usd: '',
  zzshu_card_margin_pct: '',
  zzshu_points_alert: '',
  daily_cap_usd: '',
  per_topup_max_usd: '',
  bins: '',
})
const dlg = reactive({ open: false, code: '', error: '', saving: false })
const errors = reactive<Record<string, string>>({})
const codeInput = ref<{ focus: () => void } | null>(null)

function fill(v: FundingView) {
  view.value = v
  Object.assign(form, {
    auto_recharge: v.settings.auto_recharge,
    auto_open_cards: v.settings.auto_open_cards,
    sweep_leftover: v.settings.sweep_leftover,
    upgrade_pay_difference: v.settings.upgrade_pay_difference ?? false,
    cancel_renewal_before_upgrade: v.settings.cancel_renewal_before_upgrade ?? false,
    wallet_alert_usd: v.settings.wallet_alert_usd,
    zzshu_card_margin_pct: v.settings.zzshu_card_margin_pct,
    zzshu_points_alert: String(v.settings.zzshu_points_alert ?? 3),
    daily_cap_usd: v.settings.daily_cap_usd,
    per_topup_max_usd: v.settings.per_topup_max_usd,
    bins: v.settings.bin_priority.join(' '),
  })
}
async function load() {
  loading.value = true
  loadError.value = false
  try {
    fill(await api.get())
  } catch (e) {
    loadError.value = true
    toastError(e)
  } finally {
    loading.value = false
  }
}
onMounted(load)

const bins = computed(() => form.bins.split(/[\s,]+/).filter(Boolean))
function validate(): boolean {
  for (const k of Object.keys(errors)) delete errors[k]
  if (!USD_RE.test(form.daily_cap_usd.trim()) || Number(form.daily_cap_usd) > 10000) errors.daily_cap_usd = t('funding.err.daily')
  if (!USD_RE.test(form.per_topup_max_usd.trim()) || Number(form.per_topup_max_usd) > 5000) errors.per_topup_max_usd = t('funding.err.perTopup')
  if (!errors.daily_cap_usd && !errors.per_topup_max_usd && Number(form.per_topup_max_usd) > Number(form.daily_cap_usd))
    errors.per_topup_max_usd = t('funding.err.perOverDaily')
  if (!USD_RE.test(form.wallet_alert_usd.trim()) || Number(form.wallet_alert_usd) > 100000) errors.wallet_alert_usd = t('funding.err.alert')
  if (!/^\d{1,3}(\.\d{1,2})?$/.test(form.zzshu_card_margin_pct.trim()) || Number(form.zzshu_card_margin_pct) > 100)
    errors.zzshu_card_margin_pct = t('funding.err.zzshuMargin')
  if (!/^\d{1,4}$/.test(form.zzshu_points_alert.trim()) || Number(form.zzshu_points_alert) > 1000)
    errors.zzshu_points_alert = t('funding.err.zzshuPoints')
  if (bins.value.length > 20 || bins.value.some((b) => !BIN_RE.test(b))) errors.bins = t('funding.err.bins')
  return !Object.keys(errors).length
}
const changed = computed(() => {
  const s = view.value?.settings
  if (!s) return false
  return (
    s.auto_recharge !== form.auto_recharge ||
    s.auto_open_cards !== form.auto_open_cards ||
    s.sweep_leftover !== form.sweep_leftover ||
    (s.upgrade_pay_difference ?? false) !== form.upgrade_pay_difference ||
    (s.cancel_renewal_before_upgrade ?? false) !== form.cancel_renewal_before_upgrade ||
    Number(s.wallet_alert_usd) !== Number(form.wallet_alert_usd) ||
    Number(s.zzshu_card_margin_pct) !== Number(form.zzshu_card_margin_pct) ||
    Number(s.zzshu_points_alert ?? 3) !== Number(form.zzshu_points_alert) ||
    Number(s.daily_cap_usd) !== Number(form.daily_cap_usd) ||
    Number(s.per_topup_max_usd) !== Number(form.per_topup_max_usd) ||
    s.bin_priority.join(' ') !== bins.value.join(' ')
  )
})
function askCode() {
  if (!validate()) return
  Object.assign(dlg, { open: true, code: '', error: '', saving: false })
}
async function save() {
  if (dlg.saving) return // Enter while saving: the code works once
  const code = dlg.code.replace(/\s/g, '')
  if (!/^\d{6}$/.test(code)) return void (dlg.error = t('funding.err.code'))
  dlg.saving = true
  try {
    fill(
      await api.save({
        auto_recharge: form.auto_recharge,
        auto_open_cards: form.auto_open_cards,
        sweep_leftover: form.sweep_leftover,
        upgrade_pay_difference: form.upgrade_pay_difference,
        cancel_renewal_before_upgrade: form.cancel_renewal_before_upgrade,
        wallet_alert_usd: form.wallet_alert_usd.trim(),
        zzshu_card_margin_pct: form.zzshu_card_margin_pct.trim(),
        zzshu_points_alert: Number(form.zzshu_points_alert),
        daily_cap_usd: form.daily_cap_usd.trim(),
        per_topup_max_usd: form.per_topup_max_usd.trim(),
        bin_priority: bins.value,
        totp_code: code,
      }),
    )
    dlg.open = false
    toastOk(t('funding.saved'))
  } catch (e) {
    if (isApiError(e) && te(`funding.err.${e.code}`)) dlg.error = t(`funding.err.${e.code}`)
    else toastError(e)
  } finally {
    dlg.code = ''
    dlg.saving = false
  }
}
const stateType = (s: FundingTopup['state']) => (s === 'done' ? 'success' : s === 'failed' ? 'danger' : s === 'void' ? 'info' : 'warning')
const when = (iso: string | null) => (iso ? iso.replace('T', ' ').slice(0, 16) + ' UTC' : '—')
</script>

<template>
  <section class="funding" :aria-busy="loading">
    <div class="head">
      <h2 class="h">{{ t('funding.title') }}</h2>
      <el-button :icon="Refresh" :loading="loading" @click="load">{{ t('cardPool.refresh') }}</el-button>
    </div>
    <p class="muted small">{{ t('funding.hint') }}</p>
    <el-alert v-if="loadError" type="error" :closable="false" show-icon :title="t('cardPool.loadFailed')" role="alert" />
    <template v-if="view">
      <div class="today">
        <el-tag :type="view.settings.auto_recharge ? 'success' : 'info'" effect="plain">
          {{ view.settings.auto_recharge ? t('funding.on') : t('funding.off') }}
        </el-tag>
        <span class="small">{{ t('funding.today', { spent: view.today.spent_usd, left: view.today.left_usd }) }}</span>
      </div>
      <el-form label-position="top" class="form" :disabled="!auth.isAdmin" @submit.prevent="askCode">
        <el-form-item :label="t('funding.auto')">
          <el-switch v-model="form.auto_recharge" :aria-label="t('funding.auto')" />
          <div class="muted small">{{ t('funding.autoHint') }}</div>
        </el-form-item>
        <el-form-item :label="t('funding.open')">
          <el-switch v-model="form.auto_open_cards" :disabled="!form.auto_recharge" :aria-label="t('funding.open')" />
          <div class="muted small">{{ t('funding.openHint') }}</div>
        </el-form-item>
        <el-form-item :label="t('funding.sweep')">
          <el-switch v-model="form.sweep_leftover" :disabled="!form.auto_recharge" :aria-label="t('funding.sweep')" />
          <div class="muted small">{{ t('funding.sweepHint') }}</div>
        </el-form-item>
        <el-form-item :label="t('funding.upgradeDiff')">
          <el-switch v-model="form.upgrade_pay_difference" :aria-label="t('funding.upgradeDiff')" />
          <div class="muted small">{{ t('funding.upgradeDiffHint') }}</div>
        </el-form-item>
        <el-form-item :label="t('funding.cancelRenewal')">
          <el-switch v-model="form.cancel_renewal_before_upgrade" :aria-label="t('funding.cancelRenewal')" />
          <div class="muted small">{{ t('funding.cancelRenewalHint') }}</div>
        </el-form-item>
        <el-form-item :label="t('funding.alert')" :error="errors.wallet_alert_usd">
          <el-input v-model="form.wallet_alert_usd" inputmode="decimal" class="alert-input"><template #suffix>USD</template></el-input>
          <div class="muted small">{{ t('funding.alertHint') }}</div>
        </el-form-item>
        <el-form-item :label="t('funding.zzshuMargin')" :error="errors.zzshu_card_margin_pct">
          <el-input v-model="form.zzshu_card_margin_pct" inputmode="decimal" class="alert-input"><template #suffix>%</template></el-input>
          <div class="muted small">{{ t('funding.zzshuMarginHint') }}</div>
        </el-form-item>
        <el-form-item :label="t('funding.zzshuPoints')" :error="errors.zzshu_points_alert">
          <el-input v-model="form.zzshu_points_alert" inputmode="numeric" class="alert-input" />
          <div class="muted small">{{ t('funding.zzshuPointsHint') }}</div>
        </el-form-item>
        <div class="row">
          <el-form-item :label="t('funding.daily')" :error="errors.daily_cap_usd">
            <el-input v-model="form.daily_cap_usd" inputmode="decimal"><template #suffix>USD</template></el-input>
          </el-form-item>
          <el-form-item :label="t('funding.perTopup')" :error="errors.per_topup_max_usd">
            <el-input v-model="form.per_topup_max_usd" inputmode="decimal"><template #suffix>USD</template></el-input>
          </el-form-item>
        </div>
        <el-form-item :label="t('funding.bins')" :error="errors.bins">
          <el-input v-model="form.bins" class="mono" spellcheck="false" autocomplete="off" />
          <div class="muted small">{{ t('funding.binsHint') }}</div>
        </el-form-item>
        <el-button v-if="auth.isAdmin" type="primary" native-type="submit" :disabled="!changed">{{ t('funding.save') }}</el-button>
        <p v-else class="muted small">{{ t('funding.adminOnly') }}</p>
      </el-form>

      <h3 class="h3">{{ t('funding.log') }}</h3>
      <el-empty v-if="!view.topups.length" :description="t('funding.none')" :image-size="60" />
      <el-table v-else :data="view.topups" size="small" class="log">
        <el-table-column :label="t('funding.col.when')" min-width="150"><template #default="{ row }">{{ when(row.created_at) }}</template></el-table-column>
        <el-table-column :label="t('funding.col.kind')" width="110">
          <template #default="{ row }">{{ t(`funding.kind.${row.kind || 'recharge'}`) }}</template>
        </el-table-column>
        <el-table-column :label="t('funding.col.card')" width="110"><template #default="{ row }"><span class="mono">#{{ row.card_id }}</span></template></el-table-column>
        <el-table-column :label="t('funding.col.amount')" width="110"><template #default="{ row }">{{ row.amount }} {{ row.currency }}</template></el-table-column>
        <el-table-column :label="t('funding.col.state')" width="110">
          <template #default="{ row }"><el-tag :type="stateType(row.state)" size="small">{{ t(`funding.state.${row.state}`) }}</el-tag></template>
        </el-table-column>
        <el-table-column :label="t('funding.col.error')" min-width="120"><template #default="{ row }"><span class="mono small">{{ row.error || '' }}</span></template></el-table-column>
      </el-table>
    </template>

    <el-dialog v-model="dlg.open" :title="t('funding.codeTitle')" width="min(420px, 96vw)" destroy-on-close @opened="nextTick(() => codeInput?.focus())" @closed="dlg.code = ''">
      <el-form label-position="top" @submit.prevent="save">
        <p class="small">{{ t('funding.codeHint') }}</p>
        <el-alert v-if="form.auto_recharge && !view?.settings.auto_recharge" type="warning" :closable="false" show-icon :title="t('funding.turningOn')" class="gap" />
        <el-form-item :label="t('funding.code')" :error="dlg.error">
          <el-input ref="codeInput" v-model="dlg.code" inputmode="numeric" maxlength="8" autocomplete="one-time-code" class="mono" />
          <span v-if="dlg.error" class="sr-only" role="alert">{{ dlg.error }}</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg.open = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="dlg.saving" @click="save">{{ t('common.confirm') }}</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.funding { margin-bottom: 28px; }
.head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.h { margin: 0; font-size: 18px; }
.h3 { margin: 20px 0 8px; font-size: 15px; }
.today { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 8px 0 12px; }
.form { max-width: 640px; }
.alert-input { max-width: 200px; }
.row { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 0 16px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 13px; }
.gap { margin-bottom: 12px; }
.log { width: 100%; }
</style>
