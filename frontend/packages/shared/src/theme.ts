/**
 * Follow the system colour scheme (`prefers-color-scheme`), live. Sets the given classes on <html>
 * (`dark` for our tokens and Element Plus, `van-theme-dark` for Vant) and keeps
 * `<meta name="theme-color">` in step. Call before the app mounts so the first paint is right.
 * There is no manual switch and nothing is stored: the device setting is the single source.
 */
export function followSystemTheme(classes: readonly string[], themeColor?: { light: string; dark: string }): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => undefined
  const mq = window.matchMedia('(prefers-color-scheme: dark)')
  const apply = (): void => {
    const root = document.documentElement
    for (const c of classes) root.classList.toggle(c, mq.matches)
    if (themeColor) {
      document.querySelector('meta[name="theme-color"]')?.setAttribute('content', mq.matches ? themeColor.dark : themeColor.light)
    }
  }
  apply()
  mq.addEventListener('change', apply)
  return () => mq.removeEventListener('change', apply)
}
