<script setup lang="ts">
/**
 * The signed-in staff member's own two-factor login: turn on (QR + one code), recovery codes
 * (shown once), new codes, turn off. Nothing is kept after the dialog closes.
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { copyText, formatDateTime, type TwoFactorSetup, type TwoFactorStatus } from '@24goods/shared'
import { api } from '../api'
import { toastError, toastOk } from '../ui'

const open = defineModel<boolean>({ required: true })
const { t, locale } = useI18n()
const status = ref<TwoFactorStatus | null>(null)
const setup = ref<TwoFactorSetup | null>(null)
const recovery = ref<string[] | null>(null)
const asking = ref<'off' | 'codes' | null>(null)
const password = ref('')
const code = ref('')
const busy = ref(false)
const qr = computed(() => (setup.value ? `data:image/svg+xml;base64,${btoa(setup.value.qr_svg)}` : ''))
const grouped = computed(() => setup.value?.secret.replace(/(.{4})/g, '$1 ').trim() ?? '')

async function load() {
  try {
    status.value = await api.twoFactor.status()
  } catch (e) {
    toastError(e)
  }
}
watch(open, (v) => {
  // every opening starts clean; closing forgets the secret and the codes
  status.value = setup.value = recovery.value = asking.value = null
  password.value = code.value = ''
  if (v) void load()
})

async function run<T>(fn: () => Promise<T>): Promise<T | undefined> {
  if (busy.value) return undefined
  busy.value = true
  try {
    return await fn()
  } catch (e) {
    toastError(e)
    return undefined
  } finally {
    busy.value = false
    code.value = ''
  }
}
async function begin() {
  const out = await run(() => api.twoFactor.setup({ password: password.value }))
  password.value = ''
  if (out) setup.value = out
}
async function enable() {
  const out = await run(() => api.twoFactor.enable({ code: code.value.trim() }))
  if (!out) return
  setup.value = null
  recovery.value = out.recovery_codes
  await load()
}
async function confirm() {
  const typed = code.value.trim()
  if (asking.value === 'off') {
    const ok = await run(async () => {
      await api.twoFactor.disable({ code: typed })
      return true
    })
    if (!ok) return
    asking.value = null
    toastOk(t('twoFactor.turnedOff'))
    await load()
  } else if (asking.value === 'codes') {
    const out = await run(() => api.twoFactor.recoveryCodes({ code: typed }))
    if (!out) return
    asking.value = null
    recovery.value = out.recovery_codes
    await load()
  }
}
async function copy(text: string) {
  if (await copyText(text)) toastOk(t('twoFactor.copied'))
}
</script>

<template>
  <el-dialog v-model="open" :title="t('twoFactor.title')" width="440px" destroy-on-close>
    <template v-if="recovery">
      <p class="lead">{{ t('twoFactor.recoveryLead') }}</p>
      <ol class="codes">
        <li v-for="c in recovery" :key="c">{{ c }}</li>
      </ol>
      <div class="row">
        <el-button @click="copy(recovery.join('\n'))">{{ t('twoFactor.copyAll') }}</el-button>
        <el-button type="primary" @click="recovery = null">{{ t('twoFactor.saved') }}</el-button>
      </div>
    </template>

    <form v-else-if="setup" @submit.prevent="enable">
      <p class="lead">{{ t('twoFactor.scanLead') }}</p>
      <img class="qr" :src="qr" :alt="t('twoFactor.qrAlt')" width="196" height="196" />
      <button type="button" class="key" :title="t('twoFactor.copyKey')" @click="copy(setup.secret)">{{ grouped }}</button>
      <el-input v-model="code" class="code" size="large" maxlength="6" inputmode="numeric" autocomplete="one-time-code" :placeholder="t('twoFactor.codeLabel')" />
      <div class="row">
        <el-button @click="setup = null">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" native-type="submit" :loading="busy" :disabled="code.trim().length !== 6">{{ t('twoFactor.turnOn') }}</el-button>
      </div>
    </form>

    <form v-else-if="asking" @submit.prevent="confirm">
      <p class="lead">{{ asking === 'off' ? t('twoFactor.turnOffLead') : t('twoFactor.newCodesLead') }}</p>
      <el-input v-model="code" class="code" size="large" maxlength="16" autocomplete="one-time-code" :placeholder="t('twoFactor.codeLabel')" />
      <div class="row">
        <el-button @click="asking = null">{{ t('common.cancel') }}</el-button>
        <el-button :type="asking === 'off' ? 'danger' : 'primary'" native-type="submit" :loading="busy" :disabled="code.trim().length < 6">{{ t('common.confirm') }}</el-button>
      </div>
    </form>

    <template v-else-if="status">
      <p class="state"><el-tag :type="status.enabled ? 'success' : 'info'" size="large">{{ status.enabled ? t('twoFactor.on') : t('twoFactor.off') }}</el-tag></p>
      <p class="lead">{{ t('twoFactor.about') }}</p>
      <template v-if="status.enabled">
        <el-descriptions :column="1" border size="small" class="facts">
          <el-descriptions-item :label="t('twoFactor.since')">{{ formatDateTime(status.enabled_at, locale) }}</el-descriptions-item>
          <el-descriptions-item :label="t('twoFactor.codesLeft')">{{ status.recovery_codes_left }}</el-descriptions-item>
        </el-descriptions>
        <div class="row">
          <el-button @click="asking = 'codes'">{{ t('twoFactor.newCodes') }}</el-button>
          <el-button type="danger" plain @click="asking = 'off'">{{ t('twoFactor.turnOff') }}</el-button>
        </div>
      </template>
      <form v-else @submit.prevent="begin">
        <el-input v-model="password" type="password" size="large" show-password autocomplete="current-password" :placeholder="t('twoFactor.password')" />
        <div class="row">
          <el-button type="primary" native-type="submit" :loading="busy" :disabled="!password">{{ t('twoFactor.start') }}</el-button>
        </div>
      </form>
    </template>
    <div v-else v-loading="true" class="wait" />
  </el-dialog>
</template>

<style scoped>
.lead {
  margin: 0 0 14px;
  line-height: 1.6;
  color: var(--el-text-color-regular);
}
.state {
  margin: 0 0 12px;
}
.facts {
  margin-bottom: 4px;
}
.row {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}
.qr {
  display: block;
  margin: 0 auto 8px;
  padding: 8px;
  border-radius: 8px;
  background: #fff; /* a QR needs a white plate in both themes */
}
.key {
  display: block;
  margin: 0 auto 14px;
  padding: 6px 10px;
  border: 0;
  background: none;
  color: inherit;
  cursor: pointer;
  font: 600 13px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  word-break: break-all;
}
.code :deep(input) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.2em;
  text-align: center;
}
.codes {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 24px;
  margin: 0;
  padding: 6px 16px;
  list-style: none;
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
}
.codes li {
  padding: 8px 0;
  font: 600 14px/1.3 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  text-align: center;
  user-select: all;
}
.wait {
  height: 120px;
}
</style>
