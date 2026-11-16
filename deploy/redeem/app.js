/* Redemption flow: code -> account session -> confirm -> result. No framework, no storage, no
 * third-party code. The code, the tokens and the session text live only in this page's memory. */
(function () {
  'use strict'
  var T = {
    en: {
      title: 'Redeem your code', brand: 'Redeem', lang: '中文',
      codeTitle: 'Enter your code', codeLead: 'The code you received with your order. It is checked before anything else happens.',
      codeLabel: 'Redemption code', check: 'Check code',
      accountTitle: 'Connect your account', plan: 'Plan',
      how1: 'In this browser, sign in to the account that should receive the plan.',
      how2: 'Open this page in a new tab and copy everything it shows:', how3: 'Paste it below.',
      sessionLabel: 'Session text',
      sessionNote: 'This text gives access to your account. Paste it only here. It is used once for this redemption and is not stored or logged by us.',
      continue: 'Continue', otherCode: 'Use a different code',
      confirmTitle: 'Confirm', region: 'Billing region',
      recoveryNote: 'This account has a subscription that needs to be restored first. The redemption may ask you to try again later.',
      confirmLead: 'The code is used up when you confirm. This cannot be undone.', redeem: 'Redeem now', back: 'Back',
      order: 'Reference', checkAgain: 'Check again', another: 'Redeem another code',
      foot: 'Your code and your session text are sent only to complete this redemption.',
      working: 'Working…', waitTitle: 'Redeeming', waitLead: 'This usually takes under a minute. Keep this page open.',
      okTitle: 'Done', okLead: 'The plan is active on your account. You can close this page.',
      badTitle: 'Not completed', badLead: 'The redemption did not go through. Contact support with the reference below.',
      laterTitle: 'Still in progress', laterLead: 'It is taking longer than usual. Your code is safe: check again in a few minutes, and do not start over.',
      e_invalid_request: 'That does not look right. Check what you entered and try again.',
      e_code_unavailable: 'This code cannot be redeemed. Check it for typos; if it was already used, it cannot be used again.',
      e_credential_invalid: 'The session text was not accepted. Sign in again, copy a fresh one and paste all of it.',
      e_account_ineligible: 'This account cannot receive this plan (for example, it already has an active plan).',
      e_rejected: 'The redemption was refused. Contact support.',
      e_session_expired: 'This took too long and has expired. Please start again with your code.',
      e_conflict: 'This redemption was already submitted. Checking its result…',
      e_rate_limited: 'Too many attempts. Wait a minute and try again.',
      e_unavailable: 'The service is busy right now. Try again in a few minutes.',
      e_timeout: 'No answer yet. Try again: nothing is charged twice.',
      e_network: 'No connection. Check your network and try again.'
    },
    zh: {
      title: '兑换码兑换', brand: '兑换', lang: 'English',
      codeTitle: '输入兑换码', codeLead: '请输入订单中收到的兑换码。系统会先校验兑换码，再进行后续步骤。',
      codeLabel: '兑换码', check: '校验兑换码',
      accountTitle: '关联你的账号', plan: '套餐',
      how1: '在当前浏览器中登录需要开通套餐的账号。',
      how2: '在新标签页打开以下页面，并复制页面显示的全部内容：', how3: '粘贴到下方。',
      sessionLabel: '会话内容',
      sessionNote: '这段内容可以访问你的账号，请只粘贴在这里。它仅用于本次兑换，我们不会保存或记录。',
      continue: '继续', otherCode: '更换兑换码',
      confirmTitle: '确认兑换', region: '账单地区',
      recoveryNote: '该账号有一个需要先恢复的订阅，兑换时可能会提示稍后重试。',
      confirmLead: '确认后兑换码即被使用，无法撤销。', redeem: '立即兑换', back: '返回',
      order: '参考编号', checkAgain: '再次查询', another: '兑换其他兑换码',
      foot: '兑换码和会话内容仅用于完成本次兑换。',
      working: '处理中…', waitTitle: '正在兑换', waitLead: '通常不到一分钟，请不要关闭此页面。',
      okTitle: '兑换成功', okLead: '套餐已在你的账号上生效，可以关闭此页面。',
      badTitle: '兑换未完成', badLead: '兑换没有成功，请携带下方参考编号联系客服。',
      laterTitle: '仍在处理中', laterLead: '处理时间比平时长。你的兑换码是安全的：请几分钟后再次查询，不要重新开始。',
      e_invalid_request: '输入内容有误，请检查后重试。',
      e_code_unavailable: '该兑换码无法兑换。请检查是否输入有误；已使用的兑换码不能再次使用。',
      e_credential_invalid: '会话内容未通过验证。请重新登录，复制最新的完整内容后再粘贴。',
      e_account_ineligible: '该账号无法开通此套餐（例如已有生效中的套餐）。',
      e_rejected: '兑换被拒绝，请联系客服。',
      e_session_expired: '操作超时已失效，请从输入兑换码重新开始。',
      e_conflict: '该兑换已提交，正在查询结果…',
      e_rate_limited: '尝试次数过多，请一分钟后再试。',
      e_unavailable: '服务繁忙，请几分钟后再试。',
      e_timeout: '暂未收到响应，请重试：不会重复扣费。',
      e_network: '网络连接失败，请检查网络后重试。'
    }
  }
  var lang = /^zh/i.test(navigator.language || '') ? 'zh' : 'en'
  var $ = function (id) { return document.getElementById(id) }
  var t = function (k) { return T[lang][k] || T.en[k] || k }
  // flow state: memory only, gone on reload
  var st = { step: 0, token: null, plan: '', preflight: null, credential: null, requestId: null, busy: false, result: null }
  var SECTIONS = ['s-code', 's-account', 's-confirm', 's-result']

  function paint() {
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en'
    document.title = t('title')
    $('lang').textContent = t('lang')
    var nodes = document.querySelectorAll('[data-t]')
    for (var i = 0; i < nodes.length; i++) nodes[i].textContent = t(nodes[i].getAttribute('data-t'))
    if (st.result) showResult(st.result.kind, st.result.order)
  }
  function show(step) {
    st.step = step
    SECTIONS.forEach(function (id, i) { $(id).hidden = i !== step })
    var bars = $('steps').children
    for (var i = 0; i < bars.length; i++) bars[i].className = i <= step ? 'on' : ''
    var h = $(SECTIONS[step]).querySelector('h1, .state')
    if (h) { h.setAttribute('tabindex', '-1'); h.focus({ preventScroll: true }) }
    window.scrollTo(0, 0)
  }
  function alertMsg(code) {
    var el = $('alert')
    el.hidden = !code
    el.textContent = code ? t('e_' + code) === 'e_' + code ? t('e_unavailable') : t('e_' + code) : ''
  }
  function busy(btn, on) {
    st.busy = on
    btn.disabled = on
    if (on) { btn.dataset.label = btn.textContent; btn.textContent = t('working') } else if (btn.dataset.label) { btn.textContent = t(btn.getAttribute('data-t')) }
  }
  function call(path, body) {
    return fetch('/redeem/' + path, {
      method: 'POST', credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {} }).then(function (j) {
        if (r.ok && j && j.data) return j.data
        throw { code: (j && j.error && j.error.code) || (r.status === 429 ? 'rate_limited' : 'unavailable') }
      })
    }, function () { throw { code: 'network' } })
  }
  function reset() {
    st.token = st.preflight = st.credential = st.requestId = st.result = null
    st.plan = ''
    $('code').value = ''
    $('session').value = ''
    alertMsg(null)
    show(0)
  }
  function failed(e) {
    var code = (e && e.code) || 'unavailable'
    if (code === 'session_expired') { reset(); alertMsg(code); return }
    alertMsg(code)
  }

  $('f-code').addEventListener('submit', function (ev) {
    ev.preventDefault()
    var code = $('code').value.trim()
    if (st.busy) return
    if (!/^[A-Za-z0-9._:-]{8,128}$/.test(code)) { alertMsg('invalid_request'); return }
    var btn = this.querySelector('.primary')
    alertMsg(null); busy(btn, true)
    call('preview', { code: code }).then(function (d) {
      st.token = d.redemption_token
      st.plan = String(d.plan || '')
      $('plan').textContent = $('plan2').textContent = st.plan
      // the session page is ChatGPT's; other products get the generic instruction only
      $('how-link').hidden = /grok|claude/i.test(st.plan + ' ' + (d.plan_flow || ''))
      $('code').value = ''
      show(1)
    }, failed).then(function () { busy(btn, false) })
  })

  $('f-account').addEventListener('submit', function (ev) {
    ev.preventDefault()
    var session = $('session').value.trim()
    if (st.busy) return
    if (session.length < 8) { alertMsg('credential_invalid'); return }
    var btn = this.querySelector('.primary')
    var cred = { mode: 'session', session: session }
    alertMsg(null); busy(btn, true)
    call('preflight', { redemption_token: st.token, credential: cred }).then(function (d) {
      st.preflight = d.preflight_token || null
      // codes without a preflight token need the credential again at the last step; otherwise drop it now
      st.credential = st.preflight ? null : cred
      $('session').value = ''
      var region = [d.payment_country, d.payment_currency].filter(Boolean).join(' · ')
      $('region').textContent = region
      $('row-region').hidden = !region
      $('recovery-note').hidden = !d.subscription_recovery_required
      st.requestId = null
      show(2)
    }, failed).then(function () { busy(btn, false) })
  })

  function newId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID()
    var a = new Uint8Array(16); crypto.getRandomValues(a)
    return Array.prototype.map.call(a, function (b) { return ('0' + b.toString(16)).slice(-2) }).join('')
  }
  $('go').addEventListener('click', function () {
    if (st.busy) return
    var btn = this
    // the same id on every retry: the supplier treats a repeat as the same request
    st.requestId = st.requestId || newId()
    var body = { redemption_token: st.token, client_request_id: st.requestId }
    if (st.preflight) body.preflight_token = st.preflight
    else body.credential = st.credential
    alertMsg(null); busy(btn, true)
    call('redeem', body).then(function (d) {
      st.credential = null
      settle(d) || poll()
    }, function (e) {
      if (e.code === 'conflict') { st.credential = null; poll(); return }
      failed(e)
    }).then(function () { busy(btn, false) })
  })

  var OK = /^(succeeded|success|completed|done|redeemed)$/i
  var BAD = /^(failed|rejected|cancell?ed|refunded|error)$/i
  function settle(d) {
    var status = String((d && d.status) || '')
    if (OK.test(status) && d.terminal !== false) { showResult('ok', d.order_id); return true }
    if (BAD.test(status) || (d && d.terminal && !OK.test(status))) { showResult('bad', d.order_id); return true }
    return false
  }
  function showResult(kind, order) {
    st.result = { kind: kind, order: order || (st.result && st.result.order) || '' }
    $('state').className = 'state ' + (kind === 'ok' ? 'ok' : kind === 'bad' ? 'bad' : 'wait')
    $('state-text').textContent = t(kind + 'Title')
    $('state-lead').textContent = t(kind + 'Lead')
    $('order').textContent = st.result.order
    $('row-order').hidden = !st.result.order
    $('again').hidden = kind !== 'later'
    $('restart').hidden = kind === 'wait' || kind === 'later'
    if (st.step !== 3) show(3)
  }
  function poll() {
    showResult('wait')
    var started = Date.now()
    ;(function tick() {
      call('result', { redemption_token: st.token }).then(function (d) {
        if (d.order_id) st.result.order = d.order_id
        if (!settle(d)) next()
      }, function (e) {
        // "not found" is normal for a while; anything else is retried too, the redemption is already in
        if (e.code === 'session_expired') { showResult('later'); return }
        next()
      })
      function next() {
        if (Date.now() - started > 120000) showResult('later')
        else setTimeout(tick, 4000)
      }
    })()
  }
  $('again').addEventListener('click', poll)
  $('restart').addEventListener('click', reset)
  $('back-code').addEventListener('click', reset)
  $('back-account').addEventListener('click', function () { alertMsg(null); show(1) })
  $('lang').addEventListener('click', function () { lang = lang === 'zh' ? 'en' : 'zh'; paint() })
  paint()
  show(0)
})()
