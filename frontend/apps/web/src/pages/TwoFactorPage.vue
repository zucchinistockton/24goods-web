<script setup lang="ts">
/**
 * Optional two-factor login for the signed-in account: turn on (QR + one code), keep the recovery
 * codes (shown once), make new ones, turn off. The secret and the codes exist only in this page's
 * memory; nothing is cached or stored by the client.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { copyText, formatDateTime, goBack, type TwoFactorSetup, type TwoFactorStatus } from '@24goods/shared'
import { api } from '../api'
import TwoFactorCode from '../components/TwoFactorCode.vue'
import { useAuthStore } from '../stores/auth'
import { showSuccessToast, showToast, toastError } from '../ui'

const { t, locale } = useI18n()
const router = useRouter()
const auth = useAuthStore()
const status = ref<TwoFactorStatus | null>(null)
const setup = ref<TwoFactorSetup | null>(null)
const recovery = ref<string[] | null>(null)
const password = ref('')
const busy = ref(false)
/** Which action the code sheet confirms. */
const asking = ref<'off' | 'codes' | null>(null)
const codeBox = ref<InstanceType<typeof TwoFactorCode> | null>(null)
/** The sheet keeps its content while it slides out; a fresh field is mounted on every opening. */
const sheetBody = ref(false)
const sheetFor = ref<'off' | 'codes'>('off')
watch(asking, (v) => {
  if (!v) return
  sheetFor.value = v
  sheetBody.value = true
})
const qr = computed(() => (setup.value ? `data:image/svg+xml;base64,${btoa(setup.value.qr_svg)}` : ''))
const grouped = computed(() => setup.value?.secret.replace(/(.{4})/g, '$1 ').trim() ?? '')

async function load() {
  try {
    status.value = await api.twoFactor.status()
  } catch (e) {
    toastError(e)
  }
}
onMounted(load)

async function run<T>(fn: () => Promise<T>): Promise<T | undefined> {
  if (busy.value) return undefined
  busy.value = true
  try {
    return await fn()
  } catch (e) {
    codeBox.value?.clear()
    toastError(e)
    return undefined
  } finally {
    busy.value = false
  }
}

async function begin() {
  const out = await run(() => api.twoFactor.setup(password.value ? { password: password.value } : {}))
  password.value = ''
  if (out) setup.value = out
}
async function enable(code: string) {
  const out = await run(() => api.twoFactor.enable({ code }))
  if (!out) return
  setup.value = null
  recovery.value = out.recovery_codes
  await load()
}
async function confirm(code: string) {
  if (asking.value === 'off') {
    const ok = await run(async () => {
      await api.twoFactor.disable({ code })
      return true
    })
    if (!ok) return
    asking.value = null
    showSuccessToast(t('twoFactor.turnedOff'))
    await load()
  } else if (asking.value === 'codes') {
    const out = await run(() => api.twoFactor.recoveryCodes({ code }))
    if (!out) return
    asking.value = null
    recovery.value = out.recovery_codes
    await load()
  }
}
async function copy(text: string) {
  showToast((await copyText(text)) ? t('twoFactor.copied') : t('twoFactor.copyFailed'))
}
</script>

<template>
  <div class="page">
    <van-nav-bar :title="t('twoFactor.title')" class="dock-top" left-arrow fixed placeholder safe-area-inset-top @click-left="goBack(router, '/me/settings')" />

    <!-- recovery codes: shown once, right after turning on or after making new ones -->
    <section v-if="recovery" class="block">
      <h2 class="head">{{ t('twoFactor.recoveryTitle') }}</h2>
      <p class="lead">{{ t('twoFactor.recoveryLead') }}</p>
      <ol class="codes" :aria-label="t('twoFactor.recoveryTitle')">
        <li v-for="c in recovery" :key="c">{{ c }}</li>
      </ol>
      <van-button round block plain @click="copy(recovery.join('\n'))">{{ t('twoFactor.copyAll') }}</van-button>
      <van-button round block type="primary" class="gap" @click="recovery = null">{{ t('twoFactor.saved') }}</van-button>
    </section>

    <!-- step 2 of turning on: scan, then prove the app works -->
    <section v-else-if="setup" class="block">
      <h2 class="head">{{ t('twoFactor.scanTitle') }}</h2>
      <p class="lead">{{ t('twoFactor.scanLead') }}</p>
      <img class="qr" :src="qr" :alt="t('twoFactor.qrAlt')" width="208" height="208" />
      <div class="manual">
        <span class="manual-label">{{ t('twoFactor.manualKey') }}</span>
        <button type="button" class="key" :aria-label="t('twoFactor.copyKey')" @click="copy(setup.secret)">{{ grouped }}</button>
      </div>
      <hr class="rule" />
      <TwoFactorCode ref="codeBox" class="flush" :busy="busy" :allow-recovery="false" :submit-label="t('twoFactor.turnOn')" @submit="enable" />
      <button type="button" class="quiet" @click="setup = null">{{ t('common.cancel') }}</button>
    </section>

    <template v-else-if="status">
      <section class="block">
        <p class="state" :class="{ on: status.enabled }">
          <span class="dot" aria-hidden="true" />{{ status.enabled ? t('twoFactor.on') : t('twoFactor.off') }}
        </p>
        <p class="lead">{{ t('twoFactor.about') }}</p>
        <template v-if="status.enabled">
          <dl class="facts">
            <div v-if="status.enabled_at">
              <dt>{{ t('twoFactor.since') }}</dt>
              <dd>{{ formatDateTime(status.enabled_at, locale) }}</dd>
            </div>
            <div>
              <dt>{{ t('twoFactor.codesLeft') }}</dt>
              <dd :class="{ low: status.recovery_codes_left <= 2 }">{{ status.recovery_codes_left }}</dd>
            </div>
          </dl>
          <van-button round block plain @click="asking = 'codes'">{{ t('twoFactor.newCodes') }}</van-button>
          <van-button round block plain type="danger" class="gap" @click="asking = 'off'">{{ t('twoFactor.turnOff') }}</van-button>
        </template>
        <form v-else @submit.prevent="begin">
          <van-field
            v-if="!auth.user?.is_instant"
            v-model="password"
            class="pw"
            type="password"
            autocomplete="current-password"
            :label="t('me.currentPassword')"
          />
          <p v-if="!auth.user?.is_instant" class="pw-hint">{{ t('twoFactor.passwordHint') }}</p>
          <van-button round block type="primary" native-type="submit" :loading="busy">{{ t('twoFactor.start') }}</van-button>
        </form>
      </section>
    </template>
    <van-loading v-else class="wait" />

    <van-popup :show="asking !== null" position="bottom" round closeable safe-area-inset-bottom teleport="body" class="buy-sheet" @update:show="(v: boolean) => { if (!v) asking = null }" @closed="sheetBody = false">
      <div class="sheet">
        <h2>{{ sheetFor === 'off' ? t('twoFactor.turnOff') : t('twoFactor.newCodes') }}</h2>
        <p class="lead">{{ sheetFor === 'off' ? t('twoFactor.turnOffLead') : t('twoFactor.newCodesLead') }}</p>
        <TwoFactorCode
          v-if="sheetBody"
          ref="codeBox"
          class="flush"
          :busy="busy"
          :allow-recovery="sheetFor === 'off'"
          :submit-label="t('common.confirm')"
          @submit="confirm"
        />
      </div>
    </van-popup>
  </div>
</template>

<style scoped>
.page {
  min-height: 100vh;
}
.block {
  padding: 24px 16px;
}
.head {
  margin: 0 0 8px;
  font-size: 22px;
  line-height: 1.25;
}
.lead {
  margin: 0 0 16px;
  font-size: 14px;
  line-height: 1.6;
  color: var(--c-text-2);
}
.state {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 0 12px;
  font-size: 28px;
  font-weight: 700;
  line-height: 1.2;
}
.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--c-fill-strong);
}
.state.on .dot {
  background: var(--c-success);
}
.facts {
  margin: 0 0 24px;
  border-top: 1px solid var(--c-fill-strong);
}
.facts > div {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 0;
  border-bottom: 1px solid var(--c-fill-strong);
  font-size: 14px;
}
.facts dt {
  color: var(--c-text-2);
}
.facts dd {
  margin: 0;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.facts dd.low {
  color: var(--c-bad-ink);
}
.pw {
  margin: 0 -16px;
  width: auto;
}
.pw-hint {
  margin: 8px 0 16px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--c-muted);
}
.gap {
  margin-top: 12px;
}
.qr {
  display: block;
  margin: 8px auto 16px;
  padding: 8px;
  border-radius: 8px;
  background: #fff; /* a QR needs a white plate in both themes */
}
.manual {
  text-align: center;
}
.manual-label {
  display: block;
  font-size: 12px;
  color: var(--c-muted);
}
.key {
  min-height: 44px;
  padding: 8px 12px;
  border: 0;
  background: none;
  color: inherit;
  font: 600 15px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  letter-spacing: 0.04em;
  word-break: break-all;
}
.rule {
  margin: 16px 0 0;
  border: 0;
  border-top: 1px solid var(--c-fill-strong);
}
.quiet {
  display: block;
  width: 100%;
  min-height: 44px;
  margin-top: 4px;
  border: 0;
  background: none;
  color: var(--c-muted);
  font-size: 14px;
}
.codes {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 24px;
  margin: 0 0 20px;
  padding: 4px 16px;
  list-style: none;
  border: 1px solid var(--c-note-line);
  border-radius: 8px;
  background: var(--c-note-bg);
}
.codes li {
  padding: 10px 0;
  font: 600 15px/1.3 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  text-align: center;
  user-select: all;
}
.sheet {
  padding: 20px 16px 16px;
}
.sheet h2 {
  margin: 0 0 8px;
  font-size: 18px;
}
.wait {
  display: flex;
  justify-content: center;
  padding: 48px 0;
}
</style>
