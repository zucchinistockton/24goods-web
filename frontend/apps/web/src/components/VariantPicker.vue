<script setup lang="ts">
/**
 * issue #27: plan x region selectors for a product whose active variants all carry options.
 * Plan chips first, then the regions that plan is sold in (with the price and the region's own
 * charge currency). Every combination is an ordinary variant: this only picks its id.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ProductVariant } from '@24goods/shared'
import { moneyParts } from '@24goods/shared'
import { planLabel, plansOf, regionLabel, regionsOf, soldOutVariant } from '../lib/variantOptions'

const props = defineProps<{ variants: readonly ProductVariant[]; modelValue: string | null; currency: string }>()
const emit = defineEmits<{ 'update:modelValue': [id: string] }>()
const { t, te, locale } = useI18n()

const current = computed(() => props.variants.find((v) => v.id === props.modelValue) ?? null)
const plan = computed(() => current.value?.options?.plan ?? plansOf(props.variants)[0]?.plan ?? '')
const plans = computed(() => plansOf(props.variants))
const regions = computed(() => regionsOf(props.variants, plan.value))
const cheapest = computed(() => {
  const open = regions.value.filter((v) => !soldOutVariant(v))
  if (open.length < 2) return null
  return open.reduce((a, b) => (Number(b.price) < Number(a.price) ? b : a)).id
})

function pickPlan(p: string) {
  const rows = regionsOf(props.variants, p)
  const keep = rows.find((v) => v.options?.region === current.value?.options?.region && !soldOutVariant(v))
  const next = keep ?? rows.find((v) => !soldOutVariant(v)) ?? rows[0]
  if (next) emit('update:modelValue', next.id)
}
function pickRegion(v: ProductVariant) {
  if (!soldOutVariant(v)) emit('update:modelValue', v.id)
}
const label = (p: string) => planLabel(p, (k) => (te(k) ? t(k) : null))
const price = (v: ProductVariant) => moneyParts(v.price, props.currency, locale.value)
</script>

<template>
  <div class="vp">
    <div class="muted small">{{ t('product.planLabel') }}</div>
    <div class="row" role="radiogroup" :aria-label="t('product.planLabel')">
      <button
        v-for="p in plans"
        :key="p.plan"
        type="button"
        role="radio"
        class="chip"
        :class="{ on: p.plan === plan, off: p.soldOut }"
        :aria-checked="p.plan === plan"
        @click="pickPlan(p.plan)"
      >
        {{ label(p.plan) }}
      </button>
    </div>
    <div class="muted small gap">{{ t('product.regionLabel') }}</div>
    <div class="row" role="radiogroup" :aria-label="t('product.regionLabel')">
      <button
        v-for="v in regions"
        :key="v.id"
        type="button"
        role="radio"
        class="chip region"
        :class="{ on: v.id === modelValue, off: soldOutVariant(v) }"
        :aria-checked="v.id === modelValue"
        :aria-disabled="soldOutVariant(v) ? 'true' : undefined"
        @click="pickRegion(v)"
      >
        <span class="r-name">{{ regionLabel(v.options?.region ?? '', locale) }}</span>
        <span class="r-price">
          <b>{{ price(v).symbol }}{{ price(v).value }}</b>
          <span v-if="v.region_currency" class="r-cur">{{ v.region_currency }}</span>
        </span>
        <span v-if="v.id === cheapest" class="r-tag">{{ t('product.lowest') }}</span>
        <span v-if="soldOutVariant(v)" class="r-tag off-tag">{{ t('product.regionUnavailable') }}</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 6px;
}
.gap {
  margin-top: 12px;
}
.chip {
  min-height: 44px;
  min-width: 44px;
  padding: 0 14px;
  border: 1px solid var(--c-line);
  border-radius: 22px;
  background: var(--c-surface);
  color: inherit;
  font: inherit;
  font-size: 14px;
  cursor: pointer;
}
.chip.on {
  border-color: var(--c-accent-strong);
  color: var(--c-accent-ink);
  background: var(--c-accent-soft);
  font-weight: 600;
}
.chip.off {
  color: var(--c-muted);
  background: var(--c-bg);
  cursor: not-allowed;
}
.region {
  display: inline-flex;
  flex-direction: column;
  align-items: flex-start;
  justify-content: center;
  gap: 2px;
  padding: 6px 14px;
  border-radius: 14px;
  text-align: start;
}
.r-name {
  font-size: 13px;
}
.r-price {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
}
.r-cur {
  font-size: 12px;
  color: var(--c-muted);
}
.r-tag {
  font-size: 11px;
  color: var(--c-accent-ink);
}
.off-tag {
  color: var(--c-muted);
}
</style>
