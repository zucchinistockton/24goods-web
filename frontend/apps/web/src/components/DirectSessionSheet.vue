<script setup lang="ts">
/**
 * Collects the buyer's account session for an `upstream_direct` product: the supplier applies the
 * plan to the buyer's own account, which needs this text once. It is handed to the parent in
 * memory, sent with the order, sealed by the server and deleted when the order ends. Nothing is
 * stored in the browser.
 *
 * A paste is taken out of the text box at once and replaced by a short confirmation (account,
 * plan, validity). The long token text is never rendered: some browser extensions freeze the page
 * while scanning kilobytes of text in a text box, and the token stays off the screen.
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { DirectProduct } from '@24goods/shared'
import { api } from '../api'
import { summariseSession, type SessionSummary } from '../lib/sessionSummary'

/** `check`: the session is read only to check half-price eligibility (not applied to an order) */
const props = defineProps<{ show: boolean; productId: string | null; known?: DirectProduct | null; purpose?: 'order' | 'check' }>()
const emit = defineEmits<{ 'update:show': [v: boolean]; confirm: [session: string, share: boolean] }>()
/** owner rule 2026-11-18: the buyer may let support see the session if the top-up fails (ticked by default) */
const share = ref(true)
const { t, locale } = useI18n()
const text = ref('')
const kind = ref<DirectProduct | null>(null)
const SESSION_URL = 'https://chatgpt.com/api/auth/session'
/** longer input (a paste the paste event missed, a drop) is taken out of the box as well */
const TAKE_AT = 200
let held = '' // the pasted session: plain variable, never reactive, never rendered
const summary = ref<SessionSummary | null>(null)
const ready = computed(() => (summary.value ? !summary.value.problem : text.value.trim().length >= 8))

function forget() {
  held = ''
  summary.value = null
  text.value = ''
}
function take(raw: string) {
  const v = raw.trim()
  text.value = ''
  if (!v) return
  held = v
  summary.value = summariseSession(v, kind.value)
}
function onPaste(e: ClipboardEvent) {
  const v = e.clipboardData?.getData('text/plain') ?? ''
  if (!v) return // nothing readable: let the browser paste, the input watch takes it
  e.preventDefault()
  take(v)
}
watch(text, (v) => {
  if (v.length > TAKE_AT) take(v)
})
watch(
  () => props.show,
  async (v) => {
    forget() // every opening starts empty; closing forgets what was pasted
    share.value = true
    if (!v) return
    kind.value = props.known ?? null
    if (kind.value || !props.productId) return
    try {
      kind.value = (await api.product(props.productId)).direct_product ?? null
    } catch {
      kind.value = null // the generic explanation is shown
    }
  },
)
function confirm() {
  if (!ready.value) return
  const v = summary.value ? held : text.value.trim()
  forget()
  emit('confirm', v, share.value)
}
const size = (bytes: number) => (bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`)
const when = (ms: number) => new Intl.DateTimeFormat(locale.value, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(ms))
</script>

<template>
  <van-popup :show="show" position="bottom" round closeable safe-area-inset-bottom teleport="body" class="buy-sheet" @update:show="emit('update:show', $event)">
    <form class="sheet" @submit.prevent="confirm">
      <h2>{{ purpose === 'check' ? t('halfcheck.sessionTitle') : t('direct.title') }}</h2>
      <p class="lead">{{ purpose === 'check' ? t('halfcheck.sessionLead') : t('direct.lead') }}</p>
      <ol class="how">
        <li>{{ t('direct.how1') }}</li>
        <li v-if="kind === 'gpt'">
          {{ t('direct.how2gpt') }}
          <a :href="SESSION_URL" target="_blank" rel="noopener noreferrer">chatgpt.com/api/auth/session</a>
        </li>
        <li v-else>{{ t('direct.how2other') }}</li>
        <li>{{ t('direct.how3') }}</li>
      </ol>
      <label class="label" for="direct-session">{{ t('direct.label') }}</label>
      <div v-if="summary" class="held" :class="{ bad: summary.problem }" role="status" data-testid="session-summary">
        <template v-if="!summary.problem">
          <p class="held-title">{{ t('direct.received', { size: size(summary.bytes) }) }}</p>
          <dl class="held-facts">
            <div v-if="summary.account">
              <dt>{{ t('direct.account') }}</dt>
              <dd>{{ summary.account }}</dd>
            </div>
            <div v-if="summary.plan">
              <dt>{{ t('direct.currentPlan') }}</dt>
              <dd>{{ summary.plan }}</dd>
            </div>
            <div v-if="summary.validUntil">
              <dt>{{ t('direct.validUntil') }}</dt>
              <dd>{{ when(summary.validUntil) }}</dd>
            </div>
          </dl>
        </template>
        <p v-else class="held-title">{{ t(`direct.problem.${summary.problem}`) }}</p>
        <van-button plain round size="small" native-type="button" class="again" @click="forget">{{ t('direct.pasteAgain') }}</van-button>
      </div>
      <textarea
        v-else
        id="direct-session"
        v-model="text"
        class="area"
        rows="5"
        maxlength="65536"
        :placeholder="t('direct.placeholder')"
        autocomplete="off"
        autocapitalize="none"
        autocorrect="off"
        spellcheck="false"
        writingsuggestions="false"
        data-gramm="false"
        data-gramm_editor="false"
        data-enable-grammarly="false"
        data-lpignore="true"
        data-1p-ignore="true"
        data-bwignore="true"
        data-form-type="other"
        @paste="onPaste"
      />
      <p class="note">{{ purpose === 'check' ? t('halfcheck.sessionNote') : t('direct.note') }}</p>
      <van-checkbox v-model="share" shape="square" icon-size="18px" class="share" data-testid="share-with-support">{{ t('direct.shareWithSupport') }}</van-checkbox>
      <van-button block round type="primary" size="large" native-type="submit" :disabled="!ready">{{ t('direct.continue') }}</van-button>
    </form>
  </van-popup>
</template>

<style scoped>
.sheet {
  padding: 20px 16px 16px;
}
.sheet h2 {
  margin: 0 0 8px;
  font-size: 18px;
}
.lead {
  margin: 0 0 12px;
  font-size: 14px;
  line-height: 1.6;
  color: var(--c-text-2);
}
.how {
  margin: 0 0 16px;
  padding-left: 20px;
  list-style: decimal;
  font-size: 14px;
  line-height: 1.6;
  color: var(--c-text-2);
}
.how a {
  color: inherit;
  text-underline-offset: 3px;
  word-break: break-all;
}
.label {
  display: block;
  margin-bottom: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--c-text-2);
}
.area {
  display: block;
  width: 100%;
  box-sizing: border-box;
  min-height: 120px;
  padding: 12px;
  border: 1px solid var(--c-fill-strong);
  border-radius: 10px;
  background: var(--c-fill);
  color: inherit;
  font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  word-break: break-all;
  resize: vertical;
}
.held {
  padding: 12px;
  border: 1px solid var(--c-fill-strong);
  border-radius: 10px;
  background: var(--c-fill);
}
.held.bad {
  border-color: var(--c-danger);
}
.held-title {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 600;
  line-height: 1.5;
}
.held-facts {
  margin: 0 0 10px;
  font-size: 13px;
}
.held-facts > div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 4px 0;
}
.held-facts dt {
  color: var(--c-text-2);
}
.held-facts dd {
  margin: 0;
  font-weight: 600;
  text-align: end;
  word-break: break-all;
}
.again {
  min-height: 44px;
  padding: 0 16px;
}
.note {
  margin: 12px 0 16px;
  padding: 10px 12px;
  border: 1px solid var(--c-note-line);
  border-radius: 8px;
  background: var(--c-note-bg);
  font-size: 13px;
  line-height: 1.5;
}
.share {
  margin: 0 0 12px;
  min-height: 44px;
  font-size: 13px;
  line-height: 1.5;
}
</style>
