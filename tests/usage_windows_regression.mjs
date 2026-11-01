// Regression for app/src/usageWindows.js: usage windows are whatever the
// provider reports, nothing hard-coded.  Run: node tests/usage_windows_regression.mjs
import { usageWindows, shownWindows, toggleWindow } from '../app/src/usageWindows.js'

let pass = 0, fail = 0
const is = (got, want, name) => {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  ok ? pass++ : fail++
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : `  got ${JSON.stringify(got)}`}`)
}
const keys = (ws) => ws.map((w) => w.key)

const claude = { session_pct: 1, session_reset: '4h 53m', week_pct: 32, week_reset: '4d 12h', model_pct: 5, model_name: 'Opus' }
const noFive = { week_pct: 32, week_reset: '4d 12h', model_pct: 5 }
const codex = { sess_pct: 10, sess_reset_ts: '1h', week_pct: 51 }

is(keys(usageWindows(claude)), ['session_pct', 'week_pct', 'model_pct'], 'claude: all three windows, fixed order')
is(usageWindows(claude).map((w) => w.label), ['5h', 'week', 'Opus'], 'claude labels; model uses its name')
is(usageWindows(claude)[0].reset, '4h 53m', 'reset text carried')
is(keys(usageWindows(noFive)), ['week_pct', 'model_pct'], 'claude without 5h: 5h is not listed')
is(keys(usageWindows(codex)), ['sess_pct', 'week_pct'], 'codex windows')
is(usageWindows({ month_pct: 7, daily_tokens_pct: 3 }).map((w) => w.label), ['month', 'daily tokens'], 'unknown provider windows discovered')
is(usageWindows({ week_pct: null, session_pct: NaN, x_pct: '3', model_name: 'a' }), [], 'non-numbers are not windows')
is(usageWindows(null), [], 'no reading, no windows')
is(usageWindows({ _pct: 4 }), [], 'bare _pct ignored')

const W = usageWindows(claude)
const W5 = usageWindows(noFive)
is(keys(shownWindows({ id: 'a', week: false }, W)), ['session_pct'], 'old pick: first window')
is(keys(shownWindows({ id: 'a', week: true }, W)), ['session_pct', 'week_pct'], 'old pick + week tick')
is(keys(shownWindows({ id: 'a', week: true }, W5)), ['week_pct'], 'old pick when 5h is gone: week once, no duplicate')
is(keys(shownWindows({ id: 'a', keys: ['session_pct', 'model_pct'] }, W)), ['session_pct', 'model_pct'], 'chosen windows drawn')
is(keys(shownWindows({ id: 'a', keys: ['session_pct'] }, W5)), ['week_pct'], 'chosen window gone: falls back, never empty')
is(keys(shownWindows({ id: 'a', keys: ['session_pct', 'week_pct'] }, W5)), ['week_pct'], 'chosen 5h gone: only what exists')
is(keys(shownWindows({ id: 'a', keys: ['session_pct', 'week_pct'] }, W)), ['session_pct', 'week_pct'], '5h back next day: shows again by itself')
is(shownWindows({ id: 'a', keys: ['week_pct'] }, []), [], 'no reading: nothing to draw')

const p1 = toggleWindow({ id: 'a', week: false }, W, 'week_pct')
is(p1.keys, ['session_pct', 'week_pct'], 'toggle on an old pick materialises its windows first')
const p2 = toggleWindow({ id: 'a', keys: ['session_pct', 'week_pct'] }, W5, 'model_pct')
is(p2.keys, ['session_pct', 'week_pct', 'model_pct'], 'toggle keeps a window absent today (5h)')
is(toggleWindow(p2, W5, 'week_pct').keys, ['session_pct', 'model_pct'], 'toggle off')

console.log(`${pass} passed, ${fail} failed, ${pass + fail} total`)
process.exit(fail ? 1 : 0)
