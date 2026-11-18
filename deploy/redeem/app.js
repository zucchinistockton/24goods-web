/* Redemption flow: code -> account session -> confirm -> result. No framework, no storage, no
 * third-party code. The code, the tokens and the session text live only in this page's memory. */
(function () {
  'use strict'
  var T = {
    en: {
      title: 'Redeem your code', maint: 'Maintenance', brand: 'Redeem', lang: '中文',
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
      progTitle: 'Progress', progAt: 'Completed',
      p_sent: 'Code accepted', p_half_check: 'Half-price check', p_waiting_supplier: 'Waiting for the supplier',
      p_queued: 'Queued', p_funding: 'Preparing payment', p_processing: 'Being applied to your account',
      p_payment_submitted: 'Payment submitted', p_completed: 'Completed', p_failed: 'Not completed',
      p_refunded: 'Refunded', p_attention: 'Needs attention', p_unknown: 'In progress',
      s_done: 'Done', s_current: 'In progress', s_todo: 'Not started', s_failed: 'Failed', s_attention: 'Needs attention',
      progAnnounce: 'Progress {p}%: {s}',
      r_declined: 'The payment was declined, so the plan could not be applied. Contact support with the reference below.',
      r_cancelled: 'The redemption was cancelled before it was applied.', r_expired: 'The redemption expired before it was applied.',
      r_not_completed: 'The plan could not be applied to your account.', r_refunded: 'This redemption was refunded.',
      r_review: 'Our team is looking at this. Do not start over.', r_account: 'The account needs attention. Contact support with the reference below.',
      e_bad_code: 'That does not look like a redemption code. Check it for typos.',
      e_invalid_request: 'That does not look right. Check what you entered and try again.',
      e_code_unavailable: 'This code cannot be redeemed. Check it for typos; if it was already used, it cannot be used again.',
      e_credential_invalid: 'The session text was not accepted. Sign in again, copy a fresh one and paste all of it.',
      e_account_ineligible: 'This account cannot receive this plan (for example, it already has an active plan).',
      e_rejected: 'The redemption was refused. Contact support.',
      e_session_expired: 'This took too long and has expired. Please start again with your code.',
      e_conflict: 'This redemption was already submitted. Checking its result…',
      e_rate_limited: 'Too many attempts. Wait a minute and try again.',
      e_maintenance: 'Redemption is temporarily unavailable. Please try again later.',
      e_unavailable: 'The service is busy right now. Try again in a few minutes.',
      e_timeout: 'No answer yet. Try again: nothing is charged twice.',
      e_network: 'No connection. Check your network and try again.'
    },
    zh: {
      title: '兑换码兑换', maint: '维护中', brand: '兑换', lang: 'English',
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
      progTitle: '进度', progAt: '完成于',
      p_sent: '兑换码已受理', p_half_check: '半价资格检查', p_waiting_supplier: '等待供应商',
      p_queued: '排队中', p_funding: '准备付款', p_processing: '正在开通到你的账号',
      p_payment_submitted: '已提交付款', p_completed: '已完成', p_failed: '未完成',
      p_refunded: '已退款', p_attention: '需要处理', p_unknown: '处理中',
      s_done: '已完成', s_current: '进行中', s_todo: '未开始', s_failed: '失败', s_attention: '需要处理',
      progAnnounce: '进度 {p}%：{s}',
      r_declined: '付款被拒绝，套餐未能开通。请携带下方参考编号联系客服。',
      r_cancelled: '兑换在开通前已被取消。', r_expired: '兑换在开通前已过期。',
      r_not_completed: '套餐未能开通到你的账号。', r_refunded: '该兑换已退款。',
      r_review: '我们的团队正在查看，请不要重新开始。', r_account: '账号需要处理，请携带下方参考编号联系客服。',
      e_bad_code: '兑换码格式不正确，请检查是否输入有误。',
      e_invalid_request: '输入内容有误，请检查后重试。',
      e_code_unavailable: '该兑换码无法兑换。请检查是否输入有误；已使用的兑换码不能再次使用。',
      e_credential_invalid: '会话内容未通过验证。请重新登录，复制最新的完整内容后再粘贴。',
      e_account_ineligible: '该账号无法开通此套餐（例如已有生效中的套餐）。',
      e_rejected: '兑换被拒绝，请联系客服。',
      e_session_expired: '操作超时已失效，请从输入兑换码重新开始。',
      e_conflict: '该兑换已提交，正在查询结果…',
      e_rate_limited: '尝试次数过多，请一分钟后再试。',
      e_maintenance: '兑换服务暂时不可用，请稍后再试。',
      e_unavailable: '服务繁忙，请几分钟后再试。',
      e_timeout: '暂未收到响应，请重试：不会重复扣费。',
      e_network: '网络连接失败，请检查网络后重试。'
    }
  }
  var lang = /^zh/i.test(navigator.language || '') ? 'zh' : 'en'
  var $ = function (id) { return document.getElementById(id) }
  var t = function (k) { return T[lang][k] || T.en[k] || k }
  // flow state: memory only, gone on reload
  var site = { maintenance: false, message: { en: '', zh: '' }, announcement: { en: '', zh: '' } }
  var st = { step: 0, token: null, plan: '', preflight: null, credential: null, requestId: null, busy: false, result: null, progress: null }
  var SECTIONS = ['s-code', 's-account', 's-confirm', 's-result']

  function paint() {
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en'
    document.title = t('title')
    $('lang').textContent = t('lang')
    var nodes = document.querySelectorAll('[data-t]')
    for (var i = 0; i < nodes.length; i++) nodes[i].textContent = t(nodes[i].getAttribute('data-t'))
    if (st.result) showResult(st.result.kind, st.result.order)
    renderProgress(st.progress)
    renderNotice()
  }
  // the operator's announcement / maintenance text: plain text only, always set with textContent
  function renderNotice() {
    var key = lang === 'zh' ? 'zh' : 'en'
    var el = $('notice')
    var text, prefix = ''
    if (site.maintenance) {
      // maintenance never falls back to the announcement; the fixed sentence is the last resort
      text = site.message[key] || site.message.en || t('e_maintenance')
      prefix = t('maint') + ': '
      el.setAttribute('role', 'alert')
    } else {
      text = site.announcement[key] || site.announcement.en || ''
      el.setAttribute('role', 'status')
    }
    el.textContent = text ? prefix + Array.from(String(text)).slice(0, 400).join('') : ''
    el.hidden = !text
    el.className = site.maintenance ? 'notice warn' : 'notice'
  }
  function loadSite() {
    fetch('/redeem/config', { credentials: 'omit', referrerPolicy: 'no-referrer' }) // the proxy sends max-age=30
      .then(function (r) { return r.ok ? r.json() : null })
      .then(function (j) {
        var d = j && j.data
        if (!d) return
        site.maintenance = d.maintenance === true
        ;['message', 'announcement'].forEach(function (k) {
          var v = d[k === 'message' ? 'maintenance_message' : 'announcement'] || {}
          site[k] = { en: typeof v.en === 'string' ? v.en : '', zh: typeof v.zh === 'string' ? v.zh : '' }
        })
        renderNotice()
      }, function () {})
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
    st.token = st.preflight = st.credential = st.requestId = st.result = st.progress = null
    st.progressSig = undefined
    $('pannounce').textContent = ''
    renderProgress(null)
    st.plan = ''
    $('code').value = ''
    $('session').value = ''
    alertMsg(null)
    show(0)
  }
  function failed(e) {
    var code = (e && e.code) || 'unavailable'
    if (code === 'maintenance') { site.maintenance = true; renderNotice(); alertMsg(null); return } // the banner already says it
    if (code === 'session_expired') { reset(); alertMsg(code); return }
    alertMsg(code)
  }

  $('f-code').addEventListener('submit', function (ev) {
    ev.preventDefault()
    var code = $('code').value.trim()
    if (st.busy) return
    if (!/^[A-Za-z0-9._:-]{8,128}$/.test(code)) { alertMsg('bad_code'); return }
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

  var KNOWN = ['sent', 'half_check', 'waiting_supplier', 'queued', 'funding', 'processing', 'payment_submitted', 'completed', 'failed', 'refunded', 'attention']
  var STATES = ['done', 'current', 'todo', 'failed', 'attention']
  var REASONS = ['declined', 'cancelled', 'expired', 'not_completed', 'refunded', 'review', 'account']
  function stamp(at) {
    var ms = typeof at === 'number' ? (at < 1e12 ? at * 1000 : at) : typeof at === 'string' ? Date.parse(at) : NaN
    if (!isFinite(ms) || ms <= 0) return ''
    try { return new Date(ms).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en', { dateStyle: 'medium', timeStyle: 'short' }) } catch (e) { return '' }
  }
  var GLYPH = { done: '\u2713', current: '\u25C9', failed: '!', attention: '!', todo: '\u25CB' }
  function isoStamp(at) {
    var ms = typeof at === 'number' ? (at < 1e12 ? at * 1000 : at) : typeof at === 'string' ? Date.parse(at) : NaN
    try { return isFinite(ms) && ms > 0 ? new Date(ms).toISOString() : '' } catch (e) { return '' }
  }
  // the same coarse step model as the buyer's order page; everything shown comes from fixed keys
  function renderProgress(p) {
    var box = $('progress')
    if (!p || !Array.isArray(p.steps)) { box.hidden = true; return }
    st.progress = p
    var pct = Math.max(0, Math.min(100, Math.round(Number(p.percent) || 0)))
    var tone = p.outcome === 'completed' ? 'ok' : p.outcome === 'failed' ? 'bad' : p.outcome === 'attention' ? 'warn' : ''
    box.className = 'progress ' + tone
    box.hidden = false
    $('pct').textContent = pct + '%'
    $('fill').style.width = pct + '%'
    $('bar').setAttribute('aria-valuenow', String(pct))
    // the step that matters now: current, else failed / attention, else the last step reached
    var focus = null
    p.steps.forEach(function (s) { if (!focus && (s.state === 'current' || s.state === 'failed' || s.state === 'attention')) focus = s })
    var key = focus ? focus.key : String(p.step || '')
    var stepName = t('p_' + (KNOWN.indexOf(key) >= 0 ? key : 'unknown'))
    var text = t('progAnnounce').replace('{p}', String(pct)).replace('{s}', stepName)
    $('bar').setAttribute('aria-valuetext', pct + '% · ' + stepName)
    // announce politely only when the step changes, never on a poll that changes nothing
    var sig = key + '|' + pct + '|' + (p.outcome || '')
    if (sig !== st.progressSig) { st.progressSig = sig; $('pannounce').textContent = text }
    var note = $('pnote')
    var rk = (p.outcome === 'failed' || p.outcome === 'attention') ? (REASONS.indexOf(p.reason) >= 0 ? 'r_' + p.reason : p.outcome === 'failed' ? 'r_not_completed' : 'r_review') : ''
    note.hidden = !rk
    note.textContent = rk ? t(rk) : ''
    var list = $('psteps')
    list.textContent = ''
    p.steps.slice(0, 12).forEach(function (s) {
      var li = document.createElement('li')
      li.className = STATES.indexOf(s.state) >= 0 ? s.state : 'todo'
      if (s.state === 'current') li.setAttribute('aria-current', 'step')
      var d = document.createElement('span'); d.className = 'd'; d.setAttribute('aria-hidden', 'true')
      var n = document.createElement('span'); n.className = 'n'; n.textContent = t('p_' + (KNOWN.indexOf(s.key) >= 0 ? s.key : 'unknown'))
      var sk = STATES.indexOf(s.state) >= 0 ? s.state : 'todo'
      d.textContent = GLYPH[sk]
      var sw = document.createElement('span'); sw.className = 'sr'; sw.textContent = ': ' + t('s_' + sk)
      n.appendChild(sw)
      li.appendChild(d); li.appendChild(n)
      var when = stamp(s.at)
      if (when) { var tm = document.createElement('time'); tm.className = 't'; tm.textContent = when; var iso = isoStamp(s.at); if (iso) tm.setAttribute('datetime', iso); li.appendChild(tm) }
      list.appendChild(li)
    })
  }
  var OK = /^(succeeded|success|completed|done|redeemed)$/i
  var BAD = /^(failed|rejected|cancell?ed|refunded|error)$/i
  function settle(d) {
    if (d && d.progress) renderProgress(d.progress)
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
    var wait = 4000
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
        else { setTimeout(tick, wait); wait = Math.min(Math.round(wait * 1.6), 15000) } // back off: buyers share one budget
      }
    })()
  }
  $('again').addEventListener('click', poll)
  $('restart').addEventListener('click', reset)
  $('back-code').addEventListener('click', reset)
  $('back-account').addEventListener('click', function () { alertMsg(null); show(1) })
  $('lang').addEventListener('click', function () { lang = lang === 'zh' ? 'en' : 'zh'; paint() })
  // leaving the page forgets what was typed (also keeps it out of the back/forward cache copy)
  window.addEventListener('pagehide', function () { $('code').value = ''; $('session').value = '' })
  paint()
  show(0)
  // the shop's "Redeem now" button passes the code in the URL FRAGMENT (#code=...): it never reaches
  // a server or a log. Fill it in, then drop it from the address bar and the history entry at once.
  ;(function prefill() {
    var m = /(?:^|[#&])code=([^&]*)/.exec(location.hash || '')
    if (!m) return
    try { history.replaceState(null, '', location.pathname + location.search) } catch (e) {}
    var code = ''
    try { code = decodeURIComponent(m[1]).trim().slice(0, 128) } catch (e) { return }
    if (code) { $('code').value = code; $('code').focus() }
  })()
  loadSite()
})()
