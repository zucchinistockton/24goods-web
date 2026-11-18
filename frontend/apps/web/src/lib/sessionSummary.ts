/**
 * What the buyer is shown after pasting an account session: a short confirmation instead of the
 * long text itself (rendering kilobytes of token text in a text box is what makes some browser
 * extensions freeze the page, and it keeps the token off the screen). Pure and local: nothing is
 * stored or sent from here. The server repeats every check at checkout; this only tells the buyer
 * early that the paste is complete and which account it belongs to.
 */

export type SessionProblem = 'incomplete' | 'access_token_only' | 'missing_token' | 'expired'

export interface SessionSummary {
  /** size of the pasted text in bytes (UTF-8) */
  bytes: number
  /** `u***@example.com`, never the full address */
  account: string | null
  /** the account's current plan as the session reports it (`free`, `plus`...) */
  plan: string | null
  /** when the session stops working (the earlier of the token and session expiry), ms epoch */
  validUntil: number | null
  /** set when the paste cannot be used; the order cannot continue with it */
  problem: SessionProblem | null
}

export function maskEmail(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const at = value.indexOf('@')
  if (at < 1 || at === value.length - 1) return null
  return `${value[0]}***${value.slice(at)}`.slice(0, 64)
}

function obj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function str(o: Record<string, unknown> | null, key: string): string | null {
  const v = o?.[key]
  return typeof v === 'string' && v.trim() ? v.trim() : null
}

/** The `exp` claim of a JWT in ms, or null when it cannot be read. */
function jwtExpiry(token: string): number | null {
  const part = token.split('.')[1]
  if (!part) return null
  try {
    const b64 = part.replace(/-/g, '+').replace(/_/g, '/')
    const claims = obj(JSON.parse(atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4))))
    const exp = claims?.exp
    return typeof exp === 'number' && Number.isFinite(exp) ? exp * 1000 : null
  } catch {
    return null
  }
}

/** ChatGPT (`kind === 'gpt'`): the `chatgpt.com/api/auth/session` document. Other kinds: size only. */
export function summariseSession(raw: string, kind: string | null, now: number = Date.now()): SessionSummary {
  const text = raw.trim()
  const bytes = new TextEncoder().encode(text).length
  const out: SessionSummary = { bytes, account: null, plan: null, validUntil: null, problem: null }
  if (kind !== 'gpt') return out
  const start = text.indexOf('{')
  if (start < 0) {
    out.problem = text.startsWith('eyJ') ? 'access_token_only' : 'incomplete'
    return out
  }
  let data: Record<string, unknown> | null = null
  try {
    data = obj(JSON.parse(text.slice(start, text.lastIndexOf('}') + 1)))
  } catch {
    data = null
  }
  if (!data) {
    out.problem = 'incomplete' // usually cut off while copying: copy the whole page again
    return out
  }
  out.account = maskEmail(str(obj(data.user), 'email'))
  out.plan = str(obj(data.account), 'planType')
  const access = str(data, 'accessToken')
  if (!str(data, 'sessionToken') || !access) {
    out.problem = 'missing_token'
    return out
  }
  const ends = [jwtExpiry(access), Date.parse(str(data, 'expires') ?? '')].filter((t): t is number => t !== null && Number.isFinite(t))
  out.validUntil = ends.length ? Math.min(...ends) : null
  if (out.validUntil !== null && out.validUntil <= now) out.problem = 'expired'
  return out
}
