<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { PreorderWindow } from '@24goods/shared'
import { preorderRange } from '../lib/preorder'

/** U10: "Pre-order · ships 1–3 days after payment" (+ the seller's note unless `compact`). */
const props = defineProps<{ window: PreorderWindow; compact?: boolean }>()
const { t } = useI18n()
const label = computed(() => t('preorder.badge', { range: preorderRange(t, props.window) }))
</script>

<template>
  <div class="preorder">
    <span class="badge-preorder">{{ label }}</span>
    <p v-if="!compact && window.note" class="muted note">{{ window.note }}</p>
  </div>
</template>

<style scoped>
.note {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.4;
}
</style>
