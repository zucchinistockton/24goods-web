<script setup lang="ts">
/**
 * issue #27: "Generate plan × region variants" for a code (CDK) or direct top-up product. Plans and
 * regions are chosen here; the server creates the missing combinations as archived variants with one
 * disabled source each (never a price, never switched on) and keeps the ones that exist.
 */
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { DeliveryMode } from '@24goods/shared'
import { client } from '../api'
import { createRegionsApi, type VendorRegion } from '../api-procurement'
import { createSuppliersApi, type AdminSupplier } from '../api-suppliers'
import { toastError, toastOk } from '../ui'

const props = defineProps<{ productId: string; deliveryMode: DeliveryMode }>()
const emit = defineEmits<{ changed: [] }>()
const api = createRegionsApi(client)
const suppliersApi = createSuppliersApi(client)
const { t } = useI18n()

const open = ref(false)
const busy = ref(false)
const suppliers = ref<AdminSupplier[]>([])
const regions = ref<VendorRegion[]>([])
const regionsOk = ref(true)
const note = ref('')
const form = reactive({
  supplier_id: '',
  vendor_product: 'gpt' as 'gpt' | 'claude' | 'grok',
  plans: [] as string[],
  regions: [] as string[],
  card_id: null as number | null,
  cost: '',
  costs: {} as Record<string, string>,
})
const supported = computed(() => props.deliveryMode === 'upstream_cdk' || props.deliveryMode === 'upstream_direct')
const isDirect = computed(() => props.deliveryMode === 'upstream_direct')
const allowed = computed(() => regions.value.filter((r) => r.allowed))

async function loadRegions() {
  try {
    const r = await api.regions(form.vendor_product)
    regionsOk.value = r.available
    regions.value = r.regions
    form.regions = form.regions.filter((c) => r.regions.some((x) => x.country === c && x.allowed))
  } catch (e) {
    toastError(e)
  }
}
async function show() {
  note.value = ''
  open.value = true
  try {
    suppliers.value = (await suppliersApi.list()).items.filter((s) => s.adapter === 'zovocard')
    if (!form.supplier_id) form.supplier_id = suppliers.value[0]?.id ?? ''
  } catch (e) {
    toastError(e)
  }
  await loadRegions()
}
watch(() => form.vendor_product, () => open.value && void loadRegions())

const ready = computed(() => !!form.supplier_id && form.plans.length > 0 && form.regions.length > 0 && (!isDirect.value || !!form.card_id))
/** per-region costs for the selected regions only; undefined when none is filled in */
function regionCosts(): Record<string, string> | undefined {
  const out: Record<string, string> = {}
  for (const r of form.regions) {
    const v = (form.costs[r] ?? '').trim()
    if (v) out[r] = v
  }
  return Object.keys(out).length ? out : undefined
}
async function run() {
  if (!ready.value || busy.value) return
  busy.value = true
  try {
    const r = await api.generate({
      product_id: props.productId,
      supplier_id: form.supplier_id,
      vendor_product: form.vendor_product,
      plans: form.plans.map((p) => p.trim().toLowerCase()),
      regions: form.regions,
      ...(isDirect.value && form.card_id ? { card_id: form.card_id } : {}),
      ...(form.cost.trim() ? { cost_estimate: form.cost.trim() } : {}),
      ...(regionCosts() ? { cost_estimates: regionCosts() } : {}),
    })
    note.value = t('regions.gen.done', { c: r.created.length, k: r.kept.length, s: r.skipped.length })
    toastOk(note.value)
    emit('changed')
  } catch (e) {
    toastError(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <el-button v-if="supported" size="small" @click="show">{{ t('regions.generate') }}</el-button>
  <el-dialog v-model="open" :title="t('regions.gen.title')" width="min(560px, 96vw)" append-to-body destroy-on-close>
    <p class="muted small">{{ t('regions.gen.hint') }}</p>
    <el-alert v-if="!regionsOk" type="warning" :closable="false" show-icon :title="t('regions.gen.vendorDown')" />
    <el-form label-position="top" @submit.prevent="run">
      <el-form-item :label="t('regions.gen.supplier')" required>
        <el-select v-model="form.supplier_id" style="width: 100%">
          <el-option v-for="s in suppliers" :key="s.id" :value="s.id" :label="`${s.code} (${s.env})`" />
        </el-select>
      </el-form-item>
      <el-form-item :label="t('regions.gen.vendorProduct')">
        <el-select v-model="form.vendor_product" style="width: 160px">
          <el-option value="gpt" label="gpt" />
          <el-option v-if="isDirect" value="claude" label="claude" />
          <el-option v-if="isDirect" value="grok" label="grok" />
        </el-select>
      </el-form-item>
      <el-form-item :label="t('regions.gen.plans')" required>
        <el-select v-model="form.plans" multiple filterable allow-create default-first-option style="width: 100%" placeholder="plus, plus_half" />
      </el-form-item>
      <el-form-item :label="t('regions.gen.regions')" required>
        <el-checkbox-group v-model="form.regions">
          <el-checkbox v-for="r in allowed" :key="r.country" :value="r.country">{{ r.country }} · {{ r.currency }}</el-checkbox>
        </el-checkbox-group>
      </el-form-item>
      <el-form-item v-if="isDirect" :label="t('regions.gen.card')" required>
        <el-input-number v-model="form.card_id" :min="1" :controls="false" />
      </el-form-item>
      <el-form-item :label="t('regions.gen.cost')">
        <el-input v-model="form.cost" inputmode="decimal" style="width: 160px" />
      </el-form-item>
      <el-form-item v-if="form.regions.length" :label="t('regions.gen.costPerRegion')">
        <div class="costs">
          <label v-for="r in form.regions" :key="r" class="cost">
            <span>{{ r }}</span>
            <el-input v-model="form.costs[r]" inputmode="decimal" :placeholder="form.cost" style="width: 120px" />
          </label>
        </div>
      </el-form-item>
    </el-form>
    <p v-if="note" class="small">{{ note }}</p>
    <template #footer>
      <el-button type="primary" :loading="busy" :disabled="!ready" @click="run">{{ t('regions.gen.run') }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.costs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
}
.cost {
  display: flex;
  align-items: center;
  gap: 6px;
}
</style>
