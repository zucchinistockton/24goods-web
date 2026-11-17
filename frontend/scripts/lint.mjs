#!/usr/bin/env node
// Lightweight policy lint (ESLint skipped to keep installs small on 500 MB RAM hosts):
//  - no v-html / innerHTML (XSS) in app/shared sources
//  - no runtime third-party CDNs / Google fonts / analytics / recaptcha
//  - no console.log left in sources
//  - audit exceptions carry a review date that has not passed; the picomatch exception holds
//    only while no vue-router build-plugin entry is imported
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, dirname, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const dirs = ['apps/web/src', 'apps/web/index.html', 'apps/admin/src', 'apps/admin/index.html', 'packages/shared/src']
const rules = [
  { re: /\bv-html\b/, msg: 'v-html is forbidden (XSS); render text with {{ }}' },
  { re: /\.innerHTML\s*=|insertAdjacentHTML|document\.write\(/, msg: 'raw HTML injection is forbidden' },
  { re: /fonts\.(googleapis|gstatic)\.com|googletagmanager|google-analytics|recaptcha|gstatic\.com/i, msg: 'Google assets are forbidden' },
  { re: /(src|href)\s*=\s*["']https?:\/\/(?!www\.w3\.org)/i, msg: 'no runtime third-party asset URLs (self-host everything)' },
  { re: /cdn\.jsdelivr|unpkg\.com|cdnjs|bootcdn|staticfile\.org|yzcdn/i, msg: 'third-party CDN is forbidden' },
  { re: /\bconsole\.log\(/, msg: 'remove console.log' },
  { re: /\beval\(|new Function\(/, msg: 'eval/new Function is forbidden' },
]
const exts = /\.(vue|ts|html|css|mjs)$/
let problems = 0
function check(path) {
  const st = statSync(path)
  if (st.isDirectory()) {
    for (const f of readdirSync(path)) if (f !== 'node_modules' && f !== 'dist') check(join(path, f))
    return
  }
  if (!exts.test(path) || path.endsWith('.d.ts')) return
  const lines = readFileSync(path, 'utf8').split('\n')
  // SafeHtml.vue is the single, audited renderer of server-sanitised *_html fields
  const allowHtml = path.endsWith('packages/shared/src/ui/SafeHtml.vue') || path.endsWith('packages/shared/src/sanitize.ts')
  lines.forEach((line, i) => {
    for (const r of rules) {
      if (allowHtml && /v-html|innerHTML/.test(r.msg + r.re.source)) continue
      if (r.re.test(line)) {
        problems++
        console.log(`${relative(root, path)}:${i + 1}  ${r.msg}\n    ${line.trim()}`)
      }
    }
  })
}
for (const d of dirs) {
  try {
    check(join(root, d))
  } catch (e) {
    if (e.code !== 'ENOENT') throw e
  }
}
// Audit exceptions (pnpm-workspace.yaml `auditConfig.ignoreGhsas`, docs/security-exceptions.md):
// every ignored advisory needs a "MANDATORY REVIEW: YYYY-MM-DD" line in its comment block, and
// lint fails once that date has passed, so an exception cannot silently outlive its review.
const ws = readFileSync(join(root, 'pnpm-workspace.yaml'), 'utf8').split('\n')
const today = new Date().toISOString().slice(0, 10)
let inIgnore = false
let note = []
for (const line of ws) {
  if (/^\s*ignoreGhsas:\s*$/.test(line)) { inIgnore = true; note = []; continue }
  if (!inIgnore) continue
  if (/^\s*#/.test(line)) { note.push(line); continue }
  const m = line.match(/^\s*-\s*(GHSA-[\w-]+)\s*$/)
  if (!m) { if (line.trim() && !/^\s/.test(line)) inIgnore = false; continue }
  const due = note.join(' ').match(/MANDATORY REVIEW:\s*(\d{4}-\d{2}-\d{2})/)
  if (!due) { problems++; console.log(`pnpm-workspace.yaml  ${m[1]}: audit exception without "MANDATORY REVIEW: YYYY-MM-DD"`) }
  else if (due[1] < today) { problems++; console.log(`pnpm-workspace.yaml  ${m[1]}: audit exception review was due ${due[1]}; re-assess (docs/security-exceptions.md)`) }
  note = []
}
// GHSA-fc5q-39xc-vjpf (picomatch) is ignored ONLY because vue-router's build plugin is never
// loaded: importing any of its plugin entries voids that exception, so it is refused here.
const pluginEntry = /from\s+['"]vue-router\/(vite|unplugin|rollup|rolldown|webpack|esbuild)\b|require\(\s*['"]vue-router\/(vite|unplugin|rollup|rolldown|webpack|esbuild)\b/
const configs = ['apps/web', 'apps/admin', 'packages/shared'].flatMap((d) => {
  try { return readdirSync(join(root, d)).filter((f) => /^vite(st)?\.config\./.test(f)).map((f) => join(root, d, f)) } catch { return [] }
})
function scanPlugin(path) {
  const st = statSync(path)
  if (st.isDirectory()) { for (const f of readdirSync(path)) if (f !== 'node_modules' && f !== 'dist') scanPlugin(join(path, f)); return }
  if (!/\.(vue|ts|mts|mjs|js|cjs)$/.test(path)) return
  readFileSync(path, 'utf8').split('\n').forEach((line, i) => {
    if (pluginEntry.test(line)) {
      problems++
      console.log(`${relative(root, path)}:${i + 1}  vue-router build plugin entries void the picomatch audit exception (GHSA-fc5q-39xc-vjpf); see docs/security-exceptions.md\n    ${line.trim()}`)
    }
  })
}
for (const p of [...dirs.map((d) => join(root, d)), ...configs]) {
  try { scanPlugin(p) } catch (e) { if (e.code !== 'ENOENT') throw e }
}

if (problems) {
  console.error(`\nlint: ${problems} problem(s)`)
  process.exit(1)
}
console.log('lint: ok')
