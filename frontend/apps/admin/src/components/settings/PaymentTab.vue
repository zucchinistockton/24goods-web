<script setup lang="ts">
import GatewaySettings from './GatewaySettings.vue'
import CryptoWallets from './CryptoWallets.vue'
import { CRYPTO_CHANNEL_IDS, cryptoChannelLabel } from '@24goods/shared'
import type { CryptoView } from '../../api-crypto'
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { apiV5 } from '../../api'
import type { AdminSiteV5 } from '../../api-v5'
import { DEFAULT_SURCHARGE_PERMILLE, MAX_SURCHARGE_PERMILLE, type ChannelCfg } from '../../api-v4'
import { percentToPermille, permilleToPercent } from '../../lib/buyerInput'
import { useSave } from '../../composables/useSave'

const props = defineProps<{ site: AdminSiteV5 }>()
const emit = defineEmits<{ saved: [s: AdminSiteV5] }>()
const { t, locale } = useI18n()
const { saving, run } = useSave()
const errors = ref<Record<string, string>>({})
const other = ref<Record<string, ChannelCfg>>({})
// every crypto network of the catalogue (owner 2026-11-21); a row shows when it is on, has a wallet
// or "show all" is ticked
const USDT = CRYPTO_CHANNEL_IDS
const wallets = ref<Set<string>>(new Set())
const showAll = ref(false)
function onCrypto(v: CryptoView) {
  wallets.value = new Set(v.channels.filter((c) => c.has_wallet).map((c) => c.id))
}
const rows = computed(() => USDT.filter((id) => showAll.value || usdt[id]?.on || wallets.value.has(id)))
const chLabel = (id: string) => cryptoChannelLabel(id, locale.value) ?? id
const usdt = reactive<Record<string, { on: boolean; pct: number }>>(
  Object.fromEntries(USDT.map((id) => [id, { on: false, pct: 0 }])),
)
const feeWhole = ref(false)
const f = reactive({ on: true, pct: permilleToPercent(DEFAULT_SURCHARGE_PERMILLE), as_unit: 'month' as 'month' | 'hour', as_value: 1, manual: 72, auto_card: 24, eta: 24, note_en: '', note_zh: '' })
watch(
  () => props.site,
  (s) => {
    const { zpay_alipay, ...rest } = s.payments?.channels ?? {}
    for (const id of USDT) {
      usdt[id] = { on: rest[id]?.enabled ?? false, pct: permilleToPercent(rest[id]?.surcharge_permille ?? 0) }
      delete rest[id]
    }
    other.value = rest
    f.on = zpay_alipay?.enabled ?? true
    feeWhole.value = s.payments?.fee_round_whole === true
    f.pct = permilleToPercent(zpay_alipay?.surcharge_permille ?? DEFAULT_SURCHARGE_PERMILLE)
    f.as_unit = s.after_sales_default?.unit ?? 'hour'
    f.as_value = s.after_sales_default?.value ?? s.after_sales_default_hours
    f.manual = s.auto_confirm_hours.manual
    f.auto_card = s.auto_confirm_hours.auto_card
    f.eta = s.delivery?.manual_eta_hours ?? 24
    f.note_en = s.delivery?.manual_note_i18n?.en ?? ''
    f.note_zh = s.delivery?.manual_note_i18n?.['zh-CN'] ?? ''
  },
  { immediate: true },
)
const feeExample = computed(() => (percentToPermille(f.pct) / 100).toFixed(2))
const okHours = (n: number, max: number) => Number.isInteger(n) && n >= 0 && n <= max
async function save() {
  const e: Record<string, string> = {}
  const pm = percentToPermille(f.pct)
  if (!Number.isInteger(pm) || pm < 0 || pm > MAX_SURCHARGE_PERMILLE) e.fee = t('site.v.fee')
  const usdtCfg: Record<string, ChannelCfg> = {}
  for (const id of USDT) {
    const p = percentToPermille(usdt[id].pct)
    if (!Number.isInteger(p) || p < 0 || p > MAX_SURCHARGE_PERMILLE) e[id] = t('site.v.fee')
    usdtCfg[id] = { enabled: usdt[id].on, surcharge_permille: p }
  }
  if (f.as_unit === 'month' ? !(okHours(f.as_value, 12) && f.as_value >= 1) : !okHours(f.as_value, 8760)) e.as = t(f.as_unit === 'month' ? 'delivery.invalid.months' : 'delivery.hoursInvalid')
  if (!okHours(f.manual, 720) || !okHours(f.auto_card, 720) || f.manual < 1 || f.auto_card < 1) e.ac = t('site.v.autoConfirm')
  if (!okHours(f.eta, 720) || f.eta < 1) e.eta = t('v5.payment.etaInvalid')
  errors.value = e
  if (Object.keys(e).length) return
  const r = await run(() =>
    apiV5.site.put({
      payments: { channels: { ...other.value, ...usdtCfg, zpay_alipay: { enabled: f.on, surcharge_permille: pm } }, fee_round_whole: feeWhole.value },
      after_sales_default: { unit: f.as_unit, value: f.as_value },
      auto_confirm_hours: { manual: f.manual, auto_card: f.auto_card },
      delivery: { manual_eta_hours: f.eta, manual_note_i18n: { en: f.note_en.trim(), 'zh-CN': f.note_zh.trim() } },
    }),
  )
  if (r) emit('saved', r)
}
</script>

<template>
  <GatewaySettings />
  <el-form label-position="top" class="wrap" @submit.prevent="save">
    <section class="panel block">
      <h2 class="h2">{{ t('site.channels') }}</h2>
      <p class="muted small">{{ t('site.feeNote') }}</p>
      <el-alert v-if="site.zpay" :type="site.zpay.configured ? 'success' : 'warning'" :closable="false" class="zp" show-icon
        :title="site.zpay.configured ? t('v5.payment.zpayOk', { pid: site.zpay.pid_last4 ? `••${site.zpay.pid_last4}` : '••••' }) : t('v5.payment.zpayMissing')"
        :description="t('v5.payment.zpayNote')" />
      <el-form-item :label="t('site.alipayEnabled')"><el-switch v-model="f.on" /></el-form-item>
      <el-form-item :label="t('site.alipayFee')" :error="errors.fee">
        <el-input-number v-model="f.pct" :min="0" :max="MAX_SURCHARGE_PERMILLE / 10" :step="0.1" :precision="1" />
        <span class="unit muted">% ({{ percentToPermille(f.pct) }}‰)</span>
      </el-form-item>
      <p class="muted small">{{ t('site.feeExample', { fee: feeExample }) }}</p>
      <el-form-item :label="t('site.feeWhole')"><el-switch v-model="feeWhole" /></el-form-item>
      <p class="muted small">{{ t('site.feeWholeNote') }}</p>
    </section>
    <section class="panel block">
      <h2 class="h2">{{ t('v5.payment.usdtTitle') }}</h2>
      <p class="muted small">{{ t('v5.payment.usdtNote') }}</p>
      <el-alert v-if="site.bepusdt" :type="site.bepusdt.configured ? 'success' : 'warning'" :closable="false" class="zp" show-icon
        :title="site.bepusdt.configured ? t('v5.payment.usdtOk') : t('v5.payment.usdtMissing')" />
      <div class="crow head muted small" aria-hidden="true">
        <span>{{ t('crypto.network') }}</span><span>{{ t('crypto.on') }}</span><span>{{ t('crypto.fee') }}</span><span>{{ t('crypto.wallet') }}</span>
      </div>
      <div v-for="id in rows" :key="id" class="crow">
        <span class="name">{{ chLabel(id) }}</span>
        <el-switch v-model="usdt[id].on" :aria-label="t('v5.payment.usdtEnabled', { name: chLabel(id) })" />
        <el-form-item class="fee" :error="errors[id]">
          <el-input-number v-model="usdt[id].pct" size="small" :min="0" :max="MAX_SURCHARGE_PERMILLE / 10" :step="0.1" :precision="1" :aria-label="t('v5.payment.usdtFee', { name: chLabel(id) })" />
          <span class="unit muted">%</span>
        </el-form-item>
        <el-tag v-if="wallets.has(id)" size="small" type="success" effect="plain">{{ t('crypto.hasWallet') }}</el-tag>
        <el-tag v-else size="small" :type="usdt[id].on ? 'danger' : 'info'" effect="plain">{{ t('crypto.noWallet') }}</el-tag>
      </div>
      <el-checkbox v-model="showAll" class="all">{{ t('crypto.showAll', { n: USDT.length }) }}</el-checkbox>
    </section>
    <CryptoWallets @loaded="onCrypto" />
    <section class="panel block">
      <h2 class="h2">{{ t('site.defaults') }}</h2>
      <el-form-item :label="t('site.afterSalesHours')" :error="errors.as">
        <el-input-number v-model="f.as_value" :min="f.as_unit === 'month' ? 1 : 0" :max="f.as_unit === 'month' ? 12 : 8760" />
        <el-select v-model="f.as_unit" :aria-label="t('site.afterSalesUnit')" style="width: 120px; margin-left: 8px" @change="f.as_value = f.as_unit === 'month' ? 1 : 24">
          <el-option value="month" :label="t('site.months')" />
          <el-option value="hour" :label="t('site.hours')" />
        </el-select>
      </el-form-item>
      <el-form-item :label="t('site.autoConfirmManual')" :error="errors.ac"><el-input-number v-model="f.manual" :min="1" :max="720" /><span class="unit muted">{{ t('site.hours') }}</span></el-form-item>
      <el-form-item :label="t('site.autoConfirmAuto')"><el-input-number v-model="f.auto_card" :min="1" :max="720" /><span class="unit muted">{{ t('site.hours') }}</span></el-form-item>
      <p class="muted small">{{ t('site.autoConfirmNote') }}</p>
    </section>
    <section class="panel block">
      <h2 class="h2">{{ t('v5.payment.manualPromise') }}</h2>
      <p class="muted small">{{ t('v5.payment.manualPromiseNote') }}</p>
      <el-form-item :label="t('v5.payment.eta')" :error="errors.eta"><el-input-number v-model="f.eta" :min="1" :max="720" /><span class="unit muted">{{ t('site.hours') }}</span></el-form-item>
      <el-form-item :label="`${t('v5.payment.note')} (EN)`"><el-input v-model="f.note_en" type="textarea" :rows="2" maxlength="300" show-word-limit /></el-form-item>
      <el-form-item :label="`${t('v5.payment.note')} (中文)`"><el-input v-model="f.note_zh" type="textarea" :rows="2" maxlength="300" show-word-limit /></el-form-item>
      <p class="muted small">{{ t('v5.payment.noteHint') }}</p>
    </section>
    <el-button type="primary" native-type="submit" :loading="saving">{{ t('common.save') }}</el-button>
  </el-form>
</template>

<style scoped src="./settings.css"></style>
<style scoped>
.zp {
  margin-bottom: 12px;
}
.crow {
  display: grid;
  grid-template-columns: minmax(150px, 1.4fr) 56px minmax(150px, 1fr) minmax(90px, auto);
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.crow.head { border-bottom-width: 2px; }
.crow .fee { margin: 0; }
.all { margin-top: 8px; }
@media (max-width: 560px) {
  .crow { grid-template-columns: 1fr 56px; }
  .crow .fee, .crow .el-tag { grid-column: span 1; }
}
</style>
