/** Long chat text → .txt (review-v3 §6). */
export const LONG_TEXT_ASK = 500
export const LONG_TEXT_FORCE = 2000

/** Hard cap for text sent as a file (UTF-8 bytes); mirrors the backend MAX_FILE_BYTES. */
export const LONG_TEXT_MAX_BYTES = 1024 * 1024

/** True when the text cannot be sent even as a .txt file. */
export function longTextTooLarge(text: string): boolean {
  // UTF-8 is at most 4 bytes per UTF-16 unit pair; skip encoding when clearly small.
  if (text.length * 3 <= LONG_TEXT_MAX_BYTES) return false
  return new TextEncoder().encode(text).length > LONG_TEXT_MAX_BYTES
}

export type LongTextDecision = 'text' | 'ask' | 'file'

export function longTextDecision(text: string): LongTextDecision {
  const n = [...text].length
  if (n > LONG_TEXT_FORCE) return 'file'
  if (n > LONG_TEXT_ASK) return 'ask'
  return 'text'
}

/** message-YYYYMMDD-HHmm.txt (local time). */
export function messageFilename(d: Date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `message-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}.txt`
}
