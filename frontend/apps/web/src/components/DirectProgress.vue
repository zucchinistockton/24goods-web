<script setup lang="ts">
/**
 * Issue #30: the buyer's progress of a direct order. A bar with the percentage, then the steps
 * (done / current / todo), the time of each reached step and a plain message when the order
 * failed or needs attention. Only the coarse keys of the server are shown, translated here.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { normaliseProgress, progressTone, stepTime, type DirectProgress } from '@24goods/shared'

const props = defineProps<{ progress: DirectProgress }>()
const { t, te, locale } = useI18n()

const KNOWN_STEPS = [
  'paid',
  'half_check',
  'waiting_supplier',
  'sent',
  'queued',
  'funding',
  'processing',
  'payment_submitted',
  'completed',
  'failed',
  'refunded',
  'attention',
]
const p = computed(() => normaliseProgress(props.progress) ?? props.progress)
const tone = computed(() => progressTone(p.value))
const label = (key: string) => (KNOWN_STEPS.includes(key) ? t(`direct.progress.step.${key}`) : t('direct.progress.step.unknown'))
const message = computed(() => {
  const pr = p.value
  if (pr.outcome === 'failed' || pr.outcome === 'attention') {
    const k = `direct.progress.reason.${pr.reason ?? ''}`
    return te(k) ? t(k) : t(`direct.progress.reason.${pr.outcome}`)
  }
  return null
})
function when(at: string | number | null): string {
  const ms = stepTime(at)
  if (ms === null) return ''
  return new Intl.DateTimeFormat(locale.value, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(ms))
}
</script>

<template>
  <section class="progress" :class="`tone-${tone}`" :aria-label="t('direct.progress.title')">
    <div class="head">
      <h4 class="title">{{ t('direct.progress.title') }}</h4>
      <span class="pct" data-testid="progress-percent">{{ p.percent }}%</span>
    </div>
    <div class="bar" role="progressbar" :aria-valuenow="p.percent" aria-valuemin="0" aria-valuemax="100" :aria-label="t('direct.progress.title')">
      <div class="fill" :style="{ width: `${p.percent}%` }"></div>
    </div>
    <p v-if="p.outcome === 'completed' && p.completed_at" class="done-at">
      {{ t('direct.progress.completedAt', { time: when(p.completed_at) }) }}
    </p>
    <p v-if="message" class="notice" :class="p.outcome" role="status">{{ message }}</p>
    <ol class="steps">
      <li v-for="s in p.steps" :key="s.key" class="step" :class="s.state" :aria-current="s.state === 'current' ? 'step' : undefined">
        <span class="dot" aria-hidden="true"></span>
        <span class="name">{{ label(s.key) }}</span>
        <span v-if="when(s.at)" class="at">{{ when(s.at) }}</span>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.progress {
  margin: 0 0 16px;
}
.head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 8px;
}
.title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}
.pct {
  font-size: 18px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.bar {
  height: 8px;
  border-radius: 4px;
  background: var(--c-fill-strong);
  overflow: hidden;
}
.fill {
  height: 100%;
  border-radius: 4px;
  background: var(--c-accent);
  transition: width 240ms ease;
}
.tone-ok .fill {
  background: var(--c-success);
}
.tone-bad .fill {
  background: var(--c-danger);
}
.tone-warn .fill {
  background: var(--c-warning);
}
.done-at {
  margin: 8px 0 0;
  font-size: 13px;
  color: var(--c-text-2);
}
.notice {
  margin: 12px 0 0;
  padding: 10px 12px;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.5;
  background: var(--c-fill-strong);
}
.notice.failed {
  color: var(--c-danger);
}
.steps {
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
}
.step {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 44px;
  border-bottom: 1px solid var(--c-fill-strong);
  font-size: 14px;
  color: var(--c-text-2);
}
.step .dot {
  flex: none;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  border: 2px solid var(--c-fill-strong);
  background: transparent;
}
.step.done {
  color: var(--c-text);
}
.step.done .dot {
  border-color: var(--c-success);
  background: var(--c-success);
}
.step.current {
  color: var(--c-text);
  font-weight: 700;
}
.step.current .dot {
  border-color: var(--c-accent);
  background: var(--c-accent);
}
.step.failed .dot {
  border-color: var(--c-danger);
  background: var(--c-danger);
}
.step.attention .dot {
  border-color: var(--c-warning);
  background: var(--c-warning);
}
.step .name {
  flex: 1;
}
.step .at {
  font-size: 12px;
  color: var(--c-muted);
  font-variant-numeric: tabular-nums;
  text-align: end;
}
@media (prefers-reduced-motion: reduce) {
  .fill {
    transition: none;
  }
}
</style>
