<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { isUsdtChannel } from '@24goods/shared'
import { useSiteStore } from '../stores/site'

/**
 * Payment method / USDT network picker. Shown only when more than one channel is enabled
 * (GET /site/payment-channels). The pick only chooses which channel the server is asked to
 * open: price, fee and amount are always the server's.
 */
const { t } = useI18n()
const site = useSiteStore()
void site.loadChannels()
</script>

<template>
  <div v-if="site.channels && site.channels.length > 1" class="pcp" role="radiogroup" :aria-label="t('pay.method')">
    <div class="muted small">{{ t('pay.method') }}</div>
    <button
      v-for="c in site.channels"
      :key="c.id"
      type="button"
      class="opt"
      :class="{ on: site.channel?.id === c.id }"
      role="radio"
      :aria-checked="site.channel?.id === c.id"
      @click="site.select(c.id)"
    >
      <span class="dot" aria-hidden="true" />
      <span class="txt">
        <span class="lbl">{{ c.label }}</span>
        <span v-if="isUsdtChannel(c.id)" class="muted small">{{ t('pay.networkHint') }}</span>
      </span>
      <span v-if="c.permille > 0" class="fee muted small">{{ t('pay.rate', { rate: c.fee_rate }) }}</span>
    </button>
  </div>
</template>

<style scoped>
.pcp {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 8px 0;
}
.opt {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 52px;
  padding: 10px 14px;
  text-align: left;
  background: var(--c-surface);
  border: 1px solid var(--c-line);
  border-radius: var(--radius-s);
  color: inherit;
  cursor: pointer;
  transition: border-color 0.15s;
}
.opt.on {
  border-color: var(--c-text);
}
.dot {
  flex: none;
  width: 16px;
  height: 16px;
  border: 1.5px solid var(--c-muted);
  border-radius: 50%;
}
.opt.on .dot {
  border: 5px solid var(--c-text);
}
.txt {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.lbl {
  font-weight: 600;
  overflow-wrap: anywhere;
}
.fee {
  flex: none;
  font-variant-numeric: tabular-nums;
}
@media (prefers-reduced-motion: reduce) {
  .opt {
    transition: none;
  }
}
</style>
