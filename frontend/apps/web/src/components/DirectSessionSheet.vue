<script setup lang="ts">
/**
 * Collects the buyer's account session for an `upstream_direct` product: the supplier applies the
 * plan to the buyer's own account, which needs this text once. It is handed to the parent in
 * memory, sent with the order, sealed by the server and deleted when the order ends. Nothing is
 * stored in the browser.
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { DirectProduct } from '@24goods/shared'
import { api } from '../api'

const props = defineProps<{ show: boolean; productId: string | null; known?: DirectProduct | null }>()
const emit = defineEmits<{ 'update:show': [v: boolean]; confirm: [session: string] }>()
const { t } = useI18n()
const text = ref('')
const kind = ref<DirectProduct | null>(null)
const SESSION_URL = 'https://chatgpt.com/api/auth/session'
const ready = computed(() => text.value.trim().length >= 8)

watch(
  () => props.show,
  async (v) => {
    text.value = '' // every opening starts empty; closing forgets what was pasted
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
  const v = text.value.trim()
  text.value = ''
  emit('confirm', v)
}
</script>

<template>
  <van-popup :show="show" position="bottom" round closeable safe-area-inset-bottom teleport="body" class="buy-sheet" @update:show="emit('update:show', $event)">
    <form class="sheet" @submit.prevent="confirm">
      <h2>{{ t('direct.title') }}</h2>
      <p class="lead">{{ t('direct.lead') }}</p>
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
      <textarea
        id="direct-session"
        v-model="text"
        class="area"
        rows="5"
        maxlength="65536"
        autocomplete="off"
        autocapitalize="none"
        autocorrect="off"
        spellcheck="false"
      />
      <p class="note">{{ t('direct.note') }}</p>
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
.note {
  margin: 12px 0 16px;
  padding: 10px 12px;
  border: 1px solid var(--c-note-line);
  border-radius: 8px;
  background: var(--c-note-bg);
  font-size: 13px;
  line-height: 1.5;
}
</style>
