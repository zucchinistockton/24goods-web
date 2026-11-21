<script setup lang="ts">
/**
 * Crypto receiving wallets (BEpusdt) for every USDT / USDC network (owner 2026-11-21). Changes apply
 * at once (they are not part of the payment form's Save) and are audited. Addresses only: BEpusdt
 * watches public addresses and never holds keys.
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessageBox } from 'element-plus'
import { isApiError } from '@24goods/shared'
import { client } from '../../api'
import { ADDRESS_RE, createCryptoApi, type CryptoView, type CryptoWallet } from '../../api-crypto'
import { toastError, toastOk } from '../../ui'

const emit = defineEmits<{ loaded: [v: CryptoView] }>()
const api = createCryptoApi(client)
const { t, te, locale } = useI18n()
const view = ref<CryptoView | null>(null)
const loading = ref(false)
const busy = ref(false)
const add = reactive({ trade_type: 'usdt.polygon', address: '', remark: '', error: '' })

const types = computed(() => (view.value?.channels ?? []).map((c) => ({ value: c.trade_type, label: lbl(c.label), family: c.family })))
function lbl(l: { en: string; 'zh-CN': string }) {
  return locale.value.startsWith('zh') ? l['zh-CN'] : l.en
}
function typeLabel(tt: string) {
  return types.value.find((x) => x.value === tt)?.label ?? tt
}
function fail(e: unknown) {
  if (isApiError(e) && te(`crypto.err.${e.code}`)) toastError(new Error(t(`crypto.err.${e.code}`)))
  else toastError(e)
}
function show(v: CryptoView) {
  view.value = v
  emit('loaded', v)
}
async function load() {
  loading.value = true
  try {
    show(await api.get())
  } catch (e) {
    fail(e)
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function submit() {
  add.error = ''
  const fam = types.value.find((x) => x.value === add.trade_type)?.family
  const address = add.address.trim()
  if (!fam || !ADDRESS_RE[fam].test(address)) return void (add.error = t('crypto.err.bad_address'))
  busy.value = true
  try {
    show(await api.add({ trade_type: add.trade_type, address, remark: add.remark.trim() || undefined }))
    add.address = ''
    add.remark = ''
    toastOk(t('crypto.added'))
  } catch (e) {
    fail(e)
  } finally {
    busy.value = false
  }
}
async function toggle(w: CryptoWallet, on: boolean) {
  busy.value = true
  try {
    show(await api.edit(w.id, { enabled: on }))
  } catch (e) {
    fail(e)
  } finally {
    busy.value = false
  }
}
async function editAddress(w: CryptoWallet) {
  let value: string
  try {
    ;({ value } = await ElMessageBox.prompt(t('crypto.newAddress', { network: typeLabel(w.trade_type) }), t('crypto.edit'), { inputValue: w.address }))
  } catch {
    return
  }
  busy.value = true
  try {
    show(await api.edit(w.id, { address: value.trim() }))
    toastOk(t('crypto.saved'))
  } catch (e) {
    fail(e)
  } finally {
    busy.value = false
  }
}
async function remove(w: CryptoWallet) {
  try {
    await ElMessageBox.confirm(t('crypto.removeAsk', { network: typeLabel(w.trade_type), address: w.address }), t('crypto.remove'), { type: 'warning' })
  } catch {
    return
  }
  busy.value = true
  try {
    show(await api.remove(w.id))
    toastOk(t('crypto.removed'))
  } catch (e) {
    fail(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="panel block" :aria-busy="loading || busy">
    <h2 class="h2">{{ t('crypto.title') }}</h2>
    <p class="muted small">{{ t('crypto.note') }}</p>
    <el-alert v-if="view?.error" type="warning" :closable="false" show-icon :title="te(`crypto.err.${view.error}`) ? t(`crypto.err.${view.error}`) : view.error" role="alert" />
    <el-table v-if="view && view.wallets.length" :data="view.wallets" size="small" class="tbl">
      <el-table-column :label="t('crypto.network')" min-width="150"><template #default="{ row }">{{ typeLabel(row.trade_type) }}</template></el-table-column>
      <el-table-column :label="t('crypto.address')" min-width="260"><template #default="{ row }"><span class="mono addr">{{ row.address }}</span></template></el-table-column>
      <el-table-column :label="t('crypto.on')" width="80">
        <template #default="{ row }"><el-switch :model-value="row.enabled" :disabled="busy" :aria-label="t('crypto.on')" @change="(v: string | number | boolean) => toggle(row, !!v)" /></template>
      </el-table-column>
      <el-table-column :label="t('crypto.remark')" min-width="100"><template #default="{ row }">{{ row.remark }}</template></el-table-column>
      <el-table-column :label="t('crypto.actions')" width="150">
        <template #default="{ row }">
          <el-button size="small" link :disabled="busy" @click="editAddress(row)">{{ t('crypto.edit') }}</el-button>
          <el-button size="small" link type="danger" :disabled="busy" @click="remove(row)">{{ t('crypto.remove') }}</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-empty v-else-if="view && !view.error" :description="t('crypto.none')" :image-size="50" />
    <h3 class="h3">{{ t('crypto.addTitle') }}</h3>
    <el-form label-position="top" class="add" @submit.prevent="submit">
      <el-form-item :label="t('crypto.network')">
        <el-select v-model="add.trade_type" filterable style="width: 100%">
          <el-option v-for="o in types" :key="o.value" :value="o.value" :label="o.label" />
        </el-select>
      </el-form-item>
      <el-form-item :label="t('crypto.address')" :error="add.error">
        <el-input v-model="add.address" class="mono" spellcheck="false" autocomplete="off" :placeholder="t('crypto.addressHint')" />
      </el-form-item>
      <el-form-item :label="t('crypto.remark')"><el-input v-model="add.remark" maxlength="128" /></el-form-item>
      <el-button type="primary" native-type="submit" :loading="busy" :disabled="!view || !!view.error">{{ t('crypto.add') }}</el-button>
    </el-form>
  </section>
</template>

<style scoped src="./settings.css"></style>
<style scoped>
.tbl { margin: 8px 0 12px; }
.addr { word-break: break-all; }
.add { max-width: 560px; }
.h3 { margin: 12px 0 4px; font-size: 15px; }
</style>
