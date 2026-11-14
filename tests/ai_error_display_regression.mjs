// Regression for audit D16: AI error / stale display in useSidecar.js + App.jsx.
// Run: node tests/ai_error_display_regression.mjs
import { readFileSync } from 'node:fs'
import { reduce } from '../app/src/useSidecar.js'
import { usageWindows } from '../app/src/usageWindows.js'

let pass = 0, fail = 0
const is = (got, want, name) => {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  ok ? pass++ : fail++
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : `  got ${JSON.stringify(got)} want ${JSON.stringify(want)}`}`)
}

// App.jsx is JSX; slice the plain functions out of it, as codex_detail_windows does.
const app = readFileSync(new URL('../app/src/App.jsx', import.meta.url), 'utf8')
const grab = (head) => {
  const i = app.indexOf(head)
  if (i < 0) return `/* ${head} missing */`
  const j = app.indexOf('\n}\n', i)
  return app.slice(i, j + 2)
}
const lib = new Function('usageWindows', [
  "const LEGACY_PROVIDERS = ['claude', 'codex']",
  grab('function aiNote('), grab('function legacyLive('), grab('function miniAiReading('),
  grab('function aiFaultNote('),
  "return { miniAiReading, aiFaultNote: typeof aiFaultNote === 'function' ? aiFaultNote : () => 'MISSING' }",
].join('\n'))(usageWindows)

const S = { errors: [], ai: null }
const run = (s, ...msgs) => msgs.reduce(reduce, s)

// F3: a second failed legacy poll must not drop the last-known hold.
{
  const s0 = run(S, { t: 'ai', codex: { sess_pct: 20, err: '' } })
  const s1 = run(s0, { t: 'ai', codex: { err: 'offline' } })
  const s2 = run(s1, { t: 'ai', codex: { err: 'offline' } })
  is([s1.ai.codex.held, s2.ai.codex.held, s2.ai.codex.sess_pct], [true, true, 20], 'F3 held survives repeated errors')
  const s3 = run(s2, { t: 'ai', codex: { sess_pct: 30, err: '' } })
  is([s3.ai.codex.held, s3.ai.codex.sess_pct], [undefined, 30], 'F3 success clears held')
  const n = run(S, { t: 'ai', codex: { err: 'offline' } }, { t: 'ai', codex: { err: 'offline' } })
  is(n.ai.codex.held, false, 'F3 never-read error is not held')
}

// F1: outer where:'ai' exception must reach the AI state and mark readings stale.
{
  const row = { id: 'c1', provider: 'codex', usage: { sess_pct: 20 }, error: null }
  const s0 = run(S, { t: 'ai', stale: false, accounts: [row] })
  const s1 = run(s0, { t: 'error', where: 'ai', err: "name 'x' is not defined" },
    { t: 'ai', status: 'wait', seconds: 300, reason: 'error' })
  is(s1.ai.fault, "name 'x' is not defined", 'F1 crash text kept on ai')
  is([s1.ai.stale, s1.ai.accounts[0].stale, s1.ai.accounts[0].usage.sess_pct], [true, true, 20], 'F1 crash marks rows stale, keeps numbers')
  is(s1.errors.length, 1, 'F1 error still logged')
  is(lib.aiFaultNote(s1.ai), "AI poll failed — name 'x' is not defined", 'F1 fault note text')
  is(/login|sign in/i.test(lib.aiFaultNote(s1.ai)), false, 'F1 crash never claims login expired')
  const s2 = run(s1, { t: 'ai', stale: false, age: 0, accounts: [row] })
  is([s2.ai.fault ?? null, s2.ai.stale, lib.aiFaultNote(s2.ai)], [null, false, null], 'F1 fresh poll clears fault')
  const cold = run(S, { t: 'error', where: 'ai', err: 'boom' })
  is(lib.aiFaultNote(cold.ai), 'AI poll failed — boom', 'F1 first-start crash visible')
  const other = run(s0, { t: 'error', where: 'net', err: 'x' })
  is(other.ai, s0.ai, 'F1 non-ai error leaves ai untouched')
}

// F2: explicit `stale` drives last-known display in the mini tab.
{
  const t = (row) => ({ row, provider: 'codex', siblings: 1 })
  const a = lib.miniAiReading({}, t({ stale: true, usage: { sess_pct: 20 }, error: 'offline' }))
  is([a.windows.length, a.held], [1, true], 'F2 stale+error keeps quota, dimmed')
  const b = lib.miniAiReading({}, t({ stale: true, usage: { sess_pct: 20 }, error: null }))
  is(b.held, true, 'F2 stale without error is dimmed')
  const c = lib.miniAiReading({}, t({ usage: { sess_pct: 20 }, error: null }))
  is([c.windows.length, c.held], [1, false], 'F2 fresh row unchanged')
  const d = lib.miniAiReading({}, t({ usage: { sess_pct: 20 }, error: 'offline' }))
  is([d.windows.length, d.held], [0, false], 'F2 error without stale/held still discarded')
}

console.log(`\n${pass} passed, ${fail} failed`)
if (fail) process.exit(1)
