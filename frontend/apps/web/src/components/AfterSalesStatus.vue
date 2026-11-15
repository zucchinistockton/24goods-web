<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { formatDateTime, type AfterSales } from '@24goods/shared'

defineProps<{ ticket: AfterSales }>()
const { t, locale } = useI18n()
</script>

<template>
  <div class="ast">
    <div class="ast-top">
      <strong>{{ t('afterSales.ticket') }}</strong>
      <span class="ast-st" :class="`s-${ticket.status}`">{{ t(`afterSales.status.${ticket.status}`) }}</span>
    </div>
    <div class="muted small">{{ t(`afterSales.reasons.${ticket.reason}`) }} · {{ formatDateTime(ticket.created_at, locale) }}</div>
    <p v-if="ticket.resolution_note" class="note">{{ ticket.resolution_note }}</p>
  </div>
</template>

<style scoped>
.ast-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}
.ast-st {
  font-size: 12px;
  padding: 0 8px;
  line-height: 20px;
  border-radius: 10px;
  background: var(--c-warn-soft);
  color: var(--c-warn-ink);
}
.ast-st.s-replaced,
.ast-st.s-refunded,
.ast-st.s-resolved {
  background: var(--c-ok-soft);
  color: var(--c-ok-ink);
}
.ast-st.s-rejected {
  background: var(--c-bad-soft);
  color: var(--c-bad-ink);
}
.small {
  font-size: 12px;
}
.note {
  margin: 8px 0 0;
  font-size: 13px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
