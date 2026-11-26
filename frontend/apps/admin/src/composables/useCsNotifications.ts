import { onBeforeUnmount, onMounted, watch } from 'vue'
import { useRoute, useRouter, type RouteLocationRaw } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { formatMoney, messagePreview } from '@24goods/shared'
import { beep, desktopNotify, registerNotifyWorker, unlockAudioOnFirstGesture } from '../notify'
import { DIRECT_VERIFY_URL, createSeenSet, createThrottle, isExpediteMessage, isRelevant, isUrgentPaid, orderAlertKind, titleWithCount, verifySecondsLeft, type NotifyEvent } from '../lib/notifyPolicy'
import { useDeskStore } from '../stores/desk'
import { usePrefsStore } from '../stores/prefs'
import { useRealtimeStore } from '../stores/realtime'

/**
 * CS notifications (review-v3 §6, v8b §4): chime + browser Notification on a new buyer message, order
 * created / paid (any delivery mode) / cancelled / expired (per-agent toggles) and a new after-sales ticket. Only when the tab is hidden or the agent is
 * looking at something else; throttled to one alert per 4 s; tab title shows the unread count.
 * Mounted once, by AdminLayout.
 */
export function useCsNotifications() {
  const { t, locale } = useI18n()
  const route = useRoute()
  const router = useRouter()
  const rt = useRealtimeStore()
  const prefs = usePrefsStore()
  const desk = useDeskStore()
  const throttle = createThrottle()
  const orderSeen = createSeenSet()
  const ticketSeen = createSeenSet()
  const expediteSeen = createSeenSet()
  const verifySeen = createSeenSet()

  // v11: notifications through a service worker keep working while the browser is minimised
  void registerNotifyWorker((url) => {
    const path = url.startsWith('/admin') ? url.slice('/admin'.length) || '/' : url
    void router.push(path)
  })
  unlockAudioOnFirstGesture()

  const isHidden = () => document.visibilityState === 'hidden' || !document.hasFocus()
  const openConversationId = () => (route.name === 'inbox' && typeof route.query.c === 'string' ? route.query.c : null)
  const openOrderId = () => (typeof route.query.open === 'string' && route.name === 'orders' ? route.query.open : null)

  function fire(ev: NotifyEvent, title: string, body: string, to: RouteLocationRaw, urgent = false) {
    const hidden = isHidden()
    if (!isRelevant(ev, { hidden, openConversationId: openConversationId(), openOrderId: openOrderId() })) return
    if (hidden) desk.unread++
    // an urgent alert (manual order to ship, buyer reminder) is never swallowed by the throttle
    if (!urgent && !throttle.take()) return
    if (prefs.sound) beep()
    const go = () => void router.push(to)
    if (hidden) {
      if (prefs.notify) desktopNotify(title, body, ev.kind, go, { url: router.resolve(to).href, urgent })
    } else {
      // Visible but elsewhere in the console: an in-app toast instead of an OS popup.
      const n = ElNotification({
        title,
        message: body.slice(0, 120),
        type: urgent ? 'warning' : 'info',
        duration: urgent ? 0 : 5000,
        position: 'bottom-right',
        onClick: () => {
          n.close()
          go()
        },
      })
    }
  }

  // A direct order waits for the Stripe check (180 s): always loud, never throttled or toggled
  // away, shown even while this tab is focused; the click opens ZovoCard (external, no opener).
  function directVerify(e: { order_no: string | null; vendor_order_id: string; expires_at: string }) {
    if (!verifySeen.first(`${e.vendor_order_id}:${e.expires_at}`)) return
    const title = t('notify.directVerifyTitle')
    const body = t('notify.directVerifyBody', {
      order: e.order_no ? `#${e.order_no}` : `ZovoCard #${e.vendor_order_id}`,
      n: verifySecondsLeft(e.expires_at),
    })
    const open = () => void window.open(DIRECT_VERIFY_URL, '_blank', 'noopener,noreferrer')
    if (isHidden()) desk.unread++
    beep()
    desktopNotify(title, body, `direct_verify:${e.vendor_order_id}`, open, { url: DIRECT_VERIFY_URL, urgent: true })
    if (!isHidden()) {
      const n = ElNotification({ title, message: body, type: 'error', duration: 0, position: 'bottom-right', onClick: () => { n.close(); open() } })
    }
  }

  const offs = [
    rt.on('attention', (e) => {
      if (e.kind === 'direct_verify') directVerify(e)
    }),
    rt.on('message', (e) => {
      if (isExpediteMessage(e.message) && expediteSeen.first(String(e.message.id))) {
        fire(
          { kind: 'order', orderId: (e.message.payload as { order_id?: string }).order_id ?? null },
          t('notify.urgentExpedite', { no: (e.message.payload as { order_no?: string }).order_no ?? '' }),
          e.message.text ?? '',
          { name: 'orders', query: { open: (e.message.payload as { order_id?: string }).order_id ?? '' } },
          true,
        )
        return
      }
      if (e.message.sender.role !== 'buyer') return
      fire(
        { kind: 'message', conversationId: e.conversation_id },
        t('notify.newMessage', { name: e.message.sender.display_name }),
        messagePreview(e.message, t),
        { name: 'inbox', query: { c: e.conversation_id } },
      )
    }),
    rt.on('order', (o) => {
      // one alert per (order, event); per-agent toggles decide which events are shown
      const kind = orderAlertKind(o, orderSeen)
      if (!kind) return
      const urgent = isUrgentPaid(kind, o)
      if (!urgent && !prefs.orderAlerts[kind]) return // urgent manual-order alerts ignore the toggle
      const amount = formatMoney(o.amount, o.currency, locale.value)
      fire(
        { kind: 'order', orderId: o.id },
        t(urgent ? 'notify.urgentPaid' : `notify.order.${kind}`, { no: o.order_no, title: o.title, amount }),
        kind === 'created' ? amount : o.title,
        { name: 'orders', query: { open: o.id } },
        urgent,
      )
    }),
    rt.on('after_sales', (tk) => {
      desk.refreshAfterSalesSoon()
      if (tk.status !== 'open' || !ticketSeen.first(tk.id)) return
      fire(
        { kind: 'after_sales', orderId: tk.order_id },
        t('notify.newTicket', { no: tk.order_no ?? tk.order?.order_no ?? '' }),
        t(`afterSales.reason.${tk.reason}`),
        { name: 'after-sales', query: { open: tk.id } },
      )
    }),
    rt.on('sync', () => desk.refreshAfterSalesSoon()),
  ]

  function onVisible() {
    if (!isHidden()) desk.unread = 0
  }
  const stopTitle = watch(
    [() => desk.unread, locale],
    () => {
      document.title = titleWithCount(t('app.title'), desk.unread)
    },
    { immediate: true },
  )

  onMounted(() => {
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onVisible)
    void desk.refreshAfterSales()
  })
  onBeforeUnmount(() => {
    offs.forEach((o) => o())
    stopTitle()
    document.removeEventListener('visibilitychange', onVisible)
    window.removeEventListener('focus', onVisible)
    document.title = t('app.title')
  })
}
