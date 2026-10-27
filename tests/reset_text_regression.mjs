// Regression for app/src/resetText.js: the reset countdown in usage tooltips.
// Run: node tests/reset_text_regression.mjs
import { resetLabel, resetOf, resetPhrase, usageTip } from '../app/src/resetText.js'

let pass = 0, fail = 0
const eq = (got, want, name) => {
  const ok = got === want; ok ? pass++ : fail++
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : `  got ${JSON.stringify(got)} want ${JSON.stringify(want)}`}`)
}
const NOW = Date.parse('2026-10-26T12:00:00Z')

eq(resetLabel('2h 27m', NOW), '2h 27m', 'sidecar text passes through')
eq(resetLabel('in 42m', NOW), '42m', 'leading "in" dropped')
eq(resetLabel(NOW / 1000 + 9000, NOW), '2h 30m', 'epoch seconds -> 2h 30m')
eq(resetLabel(String(NOW / 1000 + 9000), NOW), '2h 30m', 'epoch string')
eq(resetLabel('2026-10-28T15:00:00Z', NOW), '2d 3h', 'ISO -> days')
eq(resetLabel('2026-10-26T12:05:00Z', NOW), '5m', 'ISO -> minutes')
eq(resetLabel(NOW / 1000 - 5, NOW), 'now', 'past -> now')
eq(resetLabel('', NOW), '', 'empty')
eq(resetLabel(null, NOW), '', 'null')
eq(resetLabel('tomorrow', NOW), 'tomorrow', 'phrase left alone')

eq(resetOf({ session_pct: 18, session_reset: '2h 14m' }, 'session_pct'), '2h 14m', 'claude 5h')
eq(resetOf({ sess_pct: 3, sess_reset_ts: '1h 2m' }, 'sess_pct'), '1h 2m', 'codex 5h via _ts')
eq(resetOf({ week_pct: 32, week_reset_ts: '4d 6h' }, 'week_pct'), '4d 6h', 'week via _ts')
eq(resetOf({ week_pct: 32 }, 'week_pct'), '', 'no reset field')
eq(resetOf(null, 'week_pct'), '', 'no object')

eq(resetPhrase('2h 30m'), 'resets in 2h 30m', 'phrase')
eq(resetPhrase('now'), 'resetting now', 'phrase now')
eq(usageTip('Claude Code · week', 32, '2h 30m'), 'Claude Code · week · 32% · resets in 2h 30m', 'full tip')
eq(usageTip('Claude Code · week', 32.6, ''), 'Claude Code · week · 33%', 'tip without reset')
eq(usageTip('Codex · 5h', null, '1h'), 'Codex · 5h · resets in 1h', 'tip without pct')

console.log(`${pass} passed, ${fail} failed, ${pass + fail} total`)
process.exit(fail ? 1 : 0)
