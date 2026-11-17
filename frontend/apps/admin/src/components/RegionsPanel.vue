<script setup lang="ts">
/**
 * issue #27: the owner's region allowlist, the half-price check method and the manual checks that
 * wait for staff. The vendor's live region list decides what can be offered; this only narrows it.
 */
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatDateTime } from '@24goods/shared'
import { client } from '../api'
import { createRegionsApi, type HalfMethod, type ManualCheck, type VendorRegion } from '../api-procurement'
import { confirmBox, toastError, toastOk } from '../ui'

const api = createRegionsApi(client)
const { t, locale } = useI18n()
const METHODS: HalfMethod[] = ['vendor_preflight', 'chatgpt_direct', 'manual']
const method = ref<HalfMethod>('vendor_preflight')
const allow = ref<string[]>([])
const vendor = ref<VendorRegion[]>([])
const vendorOk = ref(true)
const checks = ref<ManualCheck[]>([])
const saving = ref(false)
const loading = ref(false)
// el-table slot rows are typed loosely by Element Plus: narrow them to the row shape of ``checks``
const asCheck = (row: unknown): ManualCheck => row as ManualCheck
const when = (v: string | null) => (v ? formatDateTime(v, locale.value) : '—')

async function load() {
  loading.value = true
  try {
    const o = await api.options()
    method.value = o.half_price_check
    allow.value = [...o.region_allowlist]
    const r = await api.regions('gpt')
    vendorOk.value = r.available
    vendor.value = r.regions
    checks.value = (await api.checks('all')).items
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function save() {
  saving.value = true
  try {
    const o = await api.saveOptions({ region_allowlist: allow.value, half_price_check: method.value })
    allow.value = [...o.region_allowlist]
    method.value = o.half_price_check
    toastOk(t('regions.saved'))
    const r = await api.regions('gpt')
    vendor.value = r.regions
    vendorOk.value = r.available
  } catch (e) {
    toastError(e)
  } finally {
    saving.value = false
  }
}
async function decide(c: ManualCheck, approve: boolean) {
  if (!approve && !(await confirmBox(t('regions.refuseConfirm')))) return
  try {
    await api.decide(c.id, approve)
    toastOk(t(approve ? 'regions.approved' : 'regions.refused'))
    checks.value = (await api.checks('all')).items
  } catch (e) {
    toastError(e)
  }
}
</script>

<template>
  <div v-loading="loading" class="panel">
    <h2 class="h">{{ t('regions.title') }}</h2>
    <p class="muted small">{{ t('regions.hint') }}</p>
    <el-alert v-if="!vendorOk" type="warning" :closable="false" show-icon :title="t('regions.vendorDown')" class="gap" />
    <el-form label-position="top" class="gap" @submit.prevent="save">
      <el-form-item :label="t('regions.allowlist')">
        <el-select v-model="allow" multiple filterable allow-create default-first-option style="width: 100%" :placeholder="t('regions.allowlistPh')">
          <el-option v-for="r in vendor" :key="r.country" :value="r.country" :label="`${r.country} · ${r.currency}`" />
        </el-select>
      </el-form-item>
      <el-form-item :label="t('regions.method')">
        <el-radio-group v-model="method" class="methods">
          <el-radio v-for="m in METHODS" :key="m" :value="m" border>{{ t(`regions.methods.${m}`) }}</el-radio>
        </el-radio-group>
        <p class="muted small method-hint">{{ t(`regions.methodHint.${method}`) }}</p>
      </el-form-item>
      <el-button type="primary" native-type="submit" :loading="saving">{{ t('regions.save') }}</el-button>
    </el-form>

    <h2 class="h gap">{{ t('regions.checks') }}</h2>
    <p class="muted small">{{ t('regions.checksHint') }}</p>
    <el-table :data="checks" row-key="id" :empty-text="t('regions.noChecks')">
      <el-table-column :label="t('regions.when')" width="170"><template #default="{ row }">{{ when(row.created_at) }}</template></el-table-column>
      <el-table-column :label="t('regions.order')" min-width="150">
        <template #default="{ row }"><span class="mono">{{ row.order_no ?? '—' }}</span><span v-if="row.order_status" class="muted small"> · {{ row.order_status }}</span></template>
      </el-table-column>
      <el-table-column :label="t('regions.plan')" min-width="120"><template #default="{ row }"><span class="mono">{{ row.plan }}</span></template></el-table-column>
      <el-table-column :label="t('regions.status')" width="150">
        <template #default="{ row }"><el-tag size="small" :type="row.status === 'manual_pending' ? 'warning' : row.status === 'manual_approved' ? 'success' : 'info'">{{ t(`regions.st.${row.status}`) }}</el-tag></template>
      </el-table-column>
      <el-table-column width="220">
        <template #default="{ row }">
          <template v-if="row.status === 'manual_pending'">
            <el-button size="small" type="primary" @click="decide(asCheck(row), true)">{{ t('regions.approve') }}</el-button>
            <el-button size="small" type="danger" plain @click="decide(asCheck(row), false)">{{ t('regions.refuse') }}</el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>
    <p class="muted small gap">{{ t('regions.afterApprove') }}</p>
  </div>
</template>

<style scoped>
.gap {
  margin-top: 16px;
}
.methods {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 8px;
}
.method-hint {
  margin: 8px 0 0;
}
</style>
