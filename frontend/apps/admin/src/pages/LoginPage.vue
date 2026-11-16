<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { LogoMark, TurnstileWidget } from '@24goods/shared/ui'
import { isApiError, isTwoFactorChallenge, safeRedirect, turnstileNeeded, type SiteInfo } from '@24goods/shared'
import { client } from '../api'
import { useAuthStore } from '../stores/auth'
import { setLocale } from '../locale'
import { toastError } from '../ui'

const { t, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const form = reactive({ email: '', password: '' })
const busy = ref(false)
const error = ref('')
// Public /site tells whether the Turnstile challenge protects `login` (review-v5.2 §1)
const site = ref<SiteInfo | null>(null)
const token = ref('')
const widget = ref<InstanceType<typeof TurnstileWidget> | null>(null)
const tsNeeded = computed(() => turnstileNeeded(site.value, 'login'))
onMounted(async () => {
  try {
    site.value = await client.get<SiteInfo>('/site')
  } catch {
    site.value = null
  }
})

/** Two-factor accounts: the password yields a pending token; the code below finishes the login. */
const pending = ref<string | null>(null)
const code = ref('')
const useRecovery = ref(false)

async function verify() {
  if (busy.value || !pending.value || code.value.trim().length < 6) return
  busy.value = true
  error.value = ''
  try {
    await auth.loginSecondFactor(pending.value, code.value.trim())
    await router.replace(safeRedirect(route.query.redirect, '/'))
  } catch (e) {
    if (isApiError(e) && e.code === 'not_staff') error.value = t('login.notStaff')
    else if (isApiError(e) && e.code === 'two_factor_expired') {
      pending.value = null
      error.value = t('errors.two_factor_expired')
    } else if (isApiError(e) && e.code === 'two_factor_invalid') error.value = t('errors.two_factor_invalid')
    else toastError(e)
  } finally {
    busy.value = false
    code.value = ''
  }
}

async function submit() {
  if (busy.value || !form.email || !form.password) return
  busy.value = true
  error.value = ''
  try {
    const out = await auth.login(form.email.trim(), form.password, token.value || undefined)
    if (isTwoFactorChallenge(out)) {
      pending.value = out.pending_token
      return
    }
    await router.replace(safeRedirect(route.query.redirect, '/'))
  } catch (e) {
    if (isApiError(e) && e.code === 'not_staff') error.value = t('login.notStaff')
    else if (isApiError(e) && e.code === 'unauthorized') error.value = t('login.failed')
    else toastError(e)
  } finally {
    busy.value = false
    form.password = ''
    token.value = ''  // tokens are single use
    widget.value?.reset()
  }
}
</script>

<template>
  <div class="login">
    <div class="login-card">
      <div class="brand"><LogoMark :size="36" />{{ t('app.title') }}</div>
      <h1>{{ pending ? t('twoFactor.loginTitle') : t('login.title') }}</h1>
      <p class="muted">{{ pending ? t('twoFactor.loginSubtitle') : t('login.subtitle') }}</p>
      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="err" />
      <el-form v-if="pending" label-position="top" @submit.prevent="verify">
        <el-form-item :label="useRecovery ? t('twoFactor.recoveryLabel') : t('twoFactor.codeLabel')">
          <el-input
            v-model="code"
            size="large"
            class="code"
            :maxlength="useRecovery ? 16 : 6"
            :inputmode="useRecovery ? 'text' : 'numeric'"
            :autocomplete="useRecovery ? 'off' : 'one-time-code'"
            autofocus
          />
        </el-form-item>
        <el-button type="primary" native-type="submit" size="large" :loading="busy" class="submit">{{ t('twoFactor.verify') }}</el-button>
        <div class="alt">
          <el-button link @click="((useRecovery = !useRecovery), (code = ''))">{{ useRecovery ? t('twoFactor.useApp') : t('twoFactor.useRecovery') }}</el-button>
          <el-button link @click="pending = null">{{ t('twoFactor.startOver') }}</el-button>
        </div>
      </el-form>
      <el-form v-else label-position="top" @submit.prevent="submit">
        <el-form-item :label="t('login.email')">
          <el-input v-model="form.email" type="email" autocomplete="username" size="large" />
        </el-form-item>
        <el-form-item :label="t('login.password')">
          <el-input v-model="form.password" type="password" autocomplete="current-password" show-password size="large" />
        </el-form-item>
        <TurnstileWidget
          v-if="tsNeeded"
          ref="widget"
          v-model:token="token"
          :site-key="site?.turnstile?.site_key ?? ''"
          action="login"
          :locale="locale"
          class="ts"
        />
        <el-button type="primary" native-type="submit" size="large" :loading="busy" class="submit">{{ t('login.submit') }}</el-button>
      </el-form>
      <div class="lang">
        <el-radio-group :model-value="locale" size="small" @update:model-value="(v) => setLocale(v as 'en' | 'zh-CN')">
          <el-radio-button value="en">{{ t('lang.en') }}</el-radio-button>
          <el-radio-button value="zh-CN">{{ t('lang.zh') }}</el-radio-button>
        </el-radio-group>
      </div>
    </div>
  </div>
</template>

<style scoped>
.code :deep(input) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.2em;
  text-align: center;
}
.alt {
  display: flex;
  justify-content: space-between;
  margin-top: 12px;
}
.login {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 16px;
  background: radial-gradient(circle at 10% 10%, var(--a-glow), transparent 40%), var(--a-bg);
}
.login-card {
  width: min(400px, 100%);
  background: var(--a-surface);
  border: 1px solid var(--a-line);
  border-radius: 14px;
  padding: 28px;
  box-shadow: 0 10px 30px rgba(15, 23, 42, 0.06);
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  font-weight: 700;
  margin-bottom: 18px;
}
h1 {
  font-size: 22px;
  margin: 0;
}
p {
  margin: 4px 0 18px;
}
.err {
  margin-bottom: 14px;
}
.ts {
  margin-bottom: 8px;
}
.submit {
  width: 100%;
  margin-top: 4px;
}
.lang {
  margin-top: 18px;
  display: flex;
  justify-content: center;
}
</style>
