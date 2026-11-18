<script setup lang="ts">
/**
 * "Recent purchases" (owner request 2026-11-18): real orders delivered in the last hour, as the
 * server lists them (product, option, masked buyer, time). Hidden when there are none; refreshed
 * every minute while the page is visible. Nothing here is invented or padded.
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatRelative, type RecentPurchase } from '@24goods/shared'
import { api } from '../api'

const { t, locale } = useI18n()
const items = ref<RecentPurchase[]>([])
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | null = null

async function load() {
  if (document.visibilityState === 'hidden') return
  try {
    items.value = (await api.recentPurchases()).items.slice(0, 10)
    now.value = Date.now()
  } catch {
    /* optional strip: on any failure it simply stays as it was */
  }
}
onMounted(() => {
  void load()
  timer = setInterval(load, 60_000)
})
onBeforeUnmount(() => timer && clearInterval(timer))
defineExpose({ load })
const shown = computed(() => items.value)
</script>

<template>
  <section v-if="shown.length" class="recent" :aria-label="t('home.recentPurchases.title')" data-testid="recent-purchases">
    <h2>{{ t('home.recentPurchases.title') }}</h2>
    <ul>
      <li v-for="(p, i) in shown" :key="`${p.product_id}-${p.at}-${i}`">
        <router-link :to="`/p/${encodeURIComponent(p.product_id)}`" class="row">
          <span class="who">{{ t('home.recentPurchases.bought', { buyer: p.buyer }) }}</span>
          <span class="what">{{ p.title }}<template v-if="p.variant"> · {{ p.variant }}</template></span>
          <time class="when" :datetime="p.at">{{ formatRelative(p.at, locale, now) }}</time>
        </router-link>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.recent {
  margin: 12px 16px 4px;
  padding: 12px;
  border-radius: 12px;
  background: var(--c-surface);
}
.recent h2 {
  margin: 0 0 8px;
  font-size: 15px;
}
.recent ul {
  list-style: none;
  margin: 0;
  padding: 0;
}
.row {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 8px;
  align-items: center;
  min-height: 44px;
  color: inherit;
  text-decoration: none;
  font-size: 13px;
  border-bottom: 1px solid var(--c-border, rgba(0, 0, 0, 0.06));
}
li:last-child .row {
  border-bottom: 0;
}
.who {
  color: var(--c-text-2);
  white-space: nowrap;
}
.what {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.when {
  color: var(--c-text-3, var(--c-text-2));
  font-size: 12px;
  white-space: nowrap;
}
</style>
