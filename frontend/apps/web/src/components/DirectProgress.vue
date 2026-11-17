<script setup lang="ts">
/**
 * Issue #30: the buyer's progress of a direct order. A bar with the percentage, then the steps
 * (done / current / todo), the time of each reached step and a plain message when the order
 * failed or needs attention. Only the coarse keys of the server are shown, translated here.
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { stepGlyph, stepStateKey } from '../lib/progressUi'
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
// the current step (or the outcome) as one text; announced politely only when it changes
const stepText = computed(() => {
  const cur = p.value.steps.find((s) => s.state === 'current' || s.state === 'failed' || s.state === 'attention')
  return label(cur?.key ?? p.value.step)
})
const announce = ref('')
watch(
  () => `${p.value.step}|${p.value.percent}|${p.value.outcome}`,
  () => {
    announce.value = t('direct.progress.announce', { percent: p.value.percent, step: stepText.value })
  },
)
const valueText = computed(() => t('direct.progress.valuetext', { percent: p.value.percent, step: stepText.value }))
const stateWord = (state: string) => t(`direct.progress.state.${stepStateKey(state)}`)
const iso = (at: string | number | null): string | undefined => {
  const ms = stepTime(at)
  return ms === null ? undefined : new Date(ms).toISOString()
}
function when(at: string | number | null): string {
  const ms = stepTime(at)
  if (ms === null) return ''
  return new Intl.DateTimeFormat(locale.value, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(ms))
}
</script>

<template>
  <section class="progress" :class="`tone-${tone}`" aria-labelledby="direct-progress-title">
    <div class="head">
      <h4 id="direct-progress-title" class="title">{{ t('direct.progress.title') }}</h4>
      <span class="pct" data-testid="progress-percent">{{ p.percent }}%</span>
    </div>
    <div
      class="bar"
      role="progressbar"
      :aria-valuenow="p.percent"
      aria-valuemin="0"
      aria-valuemax="100"
      :aria-valuetext="valueText"
      aria-labelledby="direct-progress-title"
    >
      <div class="fill" :style="{ width: `${p.percent}%` }"></div>
    </div>
    <p class="sr-only" role="status" aria-live="polite" data-testid="progress-announce">{{ announce }}</p>
    <p v-if="p.outcome === 'completed' && p.completed_at" class="done-at">
      {{ t('direct.progress.completedAt', { time: when(p.completed_at) }) }}
    </p>
    <p class="notice" :class="message ? p.outcome : null" :hidden="!message" role="status" aria-live="polite">{{ message }}</p>
    <ol class="steps">
      <li v-for="s in p.steps" :key="s.key" class="step" :class="s.state" :aria-current="s.state === 'current' ? 'step' : undefined">
        <span class="dot" aria-hidden="true">{{ stepGlyph(s.state) }}</span>
        <span class="name">{{ label(s.key) }}<span class="sr-only">: {{ stateWord(s.state) }}</span></span>
        <time v-if="when(s.at)" class="at" :datetime="iso(s.at)">{{ when(s.at) }}</time>
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
  /* visible boundary: the track reads against the page (>= 3:1), the fill against the track */
  box-shadow: inset 0 0 0 1px var(--c-muted);
  overflow: hidden;
}
.fill {
  height: 100%;
  border-radius: 4px;
  background: var(--c-accent-ink);
  transition: width 240ms ease;
}
.tone-ok .fill {
  background: var(--c-ok-ink);
}
.tone-bad .fill {
  background: var(--c-bad-ink);
}
.tone-warn .fill {
  background: var(--c-warn-ink);
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
.notice[hidden] {
  display: none;
}
.notice.failed {
  background: var(--c-bad-soft);
  color: var(--c-bad-ink);
}
.notice.attention {
  background: var(--c-warn-soft);
  color: var(--c-warn-ink);
  border-inline-start: 4px solid var(--c-warn-ink);
}
.steps {
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
}
.step {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  column-gap: 12px;
  row-gap: 2px;
  min-height: 44px;
  padding: 6px 0;
  border-bottom: 1px solid var(--c-fill-strong);
  font-size: 14px;
  color: var(--c-text-2);
}
.step .dot {
  flex: none;
  box-sizing: border-box;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  border: 2px solid var(--c-muted);
  background: transparent;
  color: var(--c-muted);
  font-size: 12px;
  font-weight: 700;
  line-height: 16px;
  text-align: center;
}
.step.done {
  color: var(--c-text);
}
.step.done .dot {
  border-color: var(--c-ok-ink);
  background: var(--c-ok-ink);
  color: var(--c-surface);
}
.step.current {
  color: var(--c-text);
  font-weight: 700;
}
.step.current .dot {
  border-color: var(--c-accent-ink);
  color: var(--c-accent-ink);
  font-size: 11px;
}
.step.failed .dot {
  border-color: var(--c-bad-ink);
  background: var(--c-bad-ink);
  color: var(--c-surface);
}
.step.attention .dot {
  border-color: var(--c-warn-ink);
  background: var(--c-warn-ink);
  color: var(--c-surface);
}
.step .name {
  flex: 1 1 0;
  min-width: 0;
  overflow-wrap: anywhere;
}
.step .at {
  font-size: 12px;
  color: var(--c-muted);
  font-variant-numeric: tabular-nums;
  text-align: end;
}
@media (max-width: 400px) {
  .step .at {
    flex: 0 0 100%;
    padding-inline-start: 32px;
    text-align: start;
  }
}
@media (prefers-reduced-motion: reduce) {
  .fill {
    transition: none;
  }
}
</style>
