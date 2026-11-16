<script setup lang="ts">
/**
 * One field for the second factor: six digits from an authenticator app, or (after a tap) one of
 * the account's recovery codes. The parent owns the request; this only collects and emits.
 */
import { computed, nextTick, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

const props = withDefaults(defineProps<{ busy?: boolean; allowRecovery?: boolean; submitLabel?: string }>(), {
  busy: false,
  allowRecovery: true,
  submitLabel: undefined,
})
const emit = defineEmits<{ submit: [code: string] }>()
const { t } = useI18n()
const recovery = ref(false)
const value = ref('')
const input = ref<HTMLInputElement | null>(null)
const ready = computed(() =>
  recovery.value ? value.value.replace(/[\s-]/g, '').length === 10 : /^\d{6}$/.test(value.value),
)

function onInput(e: Event) {
  const raw = (e.target as HTMLInputElement).value
  value.value = recovery.value ? raw.slice(0, 16) : raw.replace(/\D/g, '').slice(0, 6)
  // six digits are a complete answer: send them without a second tap
  if (!recovery.value && value.value.length === 6 && !props.busy) emit('submit', value.value)
}
function submit() {
  if (ready.value && !props.busy) emit('submit', value.value.trim())
}
async function toggle() {
  recovery.value = !recovery.value
  value.value = ''
  await nextTick()
  input.value?.focus()
}
function clear() {
  value.value = ''
  input.value?.focus()
}
defineExpose({ clear })
onMounted(() => input.value?.focus())
</script>

<template>
  <form class="tf" @submit.prevent="submit">
    <label class="tf-label" for="tf-code">{{ recovery ? t('twoFactor.recoveryLabel') : t('twoFactor.codeLabel') }}</label>
    <input
      id="tf-code"
      ref="input"
      class="tf-input"
      :class="{ wide: recovery }"
      :value="value"
      :inputmode="recovery ? 'text' : 'numeric'"
      :autocomplete="recovery ? 'off' : 'one-time-code'"
      autocapitalize="none"
      autocorrect="off"
      spellcheck="false"
      :maxlength="recovery ? 16 : 6"
      :placeholder="recovery ? 'xxxxx-xxxxx' : '000000'"
      :aria-describedby="'tf-hint'"
      @input="onInput"
    />
    <p id="tf-hint" class="tf-hint">{{ recovery ? t('twoFactor.recoveryHint') : t('twoFactor.codeHint') }}</p>
    <van-button round block type="primary" native-type="submit" :loading="busy" :disabled="!ready">
      {{ submitLabel ?? t('twoFactor.verify') }}
    </van-button>
    <button v-if="allowRecovery" type="button" class="tf-switch" @click="toggle">
      {{ recovery ? t('twoFactor.useApp') : t('twoFactor.useRecovery') }}
    </button>
  </form>
</template>

<style scoped>
.tf {
  padding: 16px;
}
.tf.flush {
  padding: 16px 0 0;
}
.tf-label {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: var(--c-text-2);
}
.tf-input {
  display: block;
  width: 100%;
  box-sizing: border-box;
  margin: 8px 0 0;
  padding: 12px 8px;
  border: 0;
  border-bottom: 2px solid var(--c-fill-strong);
  border-radius: 0;
  background: transparent;
  color: inherit;
  font: 600 32px/1.2 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.32em;
  text-align: center;
  transition: border-color 160ms;
}
.tf-input.wide {
  font-size: 22px;
  letter-spacing: 0.08em;
}
.tf-input:focus-visible {
  border-bottom-color: var(--c-focus-ring);
}
.tf-input::placeholder {
  color: var(--c-fill-strong);
}
.tf-hint {
  margin: 10px 0 16px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--c-muted);
}
.tf-switch {
  display: block;
  width: 100%;
  min-height: 44px;
  margin-top: 8px;
  border: 0;
  background: none;
  color: inherit;
  font-size: 14px;
  text-decoration: underline;
  text-underline-offset: 3px;
}
@media (prefers-reduced-motion: reduce) {
  .tf-input {
    transition: none;
  }
}
</style>
