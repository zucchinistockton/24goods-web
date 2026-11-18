/** Debounced, coalesced reloads for live views (shared by the buyer app and the console). */

export interface Coalescer {
  trigger(): void
  cancel(): void
}

/** `trigger()` waits `delayMs` (restarting on each call), then runs `load`. While a load is in
 * flight no second one starts; if triggers arrived meanwhile, exactly one more runs after it. */
export function createCoalescer(load: () => Promise<unknown>, delayMs = 500): Coalescer {
  let timer: ReturnType<typeof setTimeout> | null = null
  let running = false
  let pending = false
  let dead = false
  async function run() {
    if (dead) return
    if (running) {
      pending = true
      return
    }
    running = true
    try {
      await load()
    } catch {
      /* the loader handles its own errors */
    } finally {
      running = false
    }
    if (pending && !dead) {
      pending = false
      void run()
    }
  }
  return {
    trigger() {
      if (dead) return
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => {
        timer = null
        void run()
      }, delayMs)
    },
    cancel() {
      dead = true
      if (timer) clearTimeout(timer)
      timer = null
    },
  }
}
