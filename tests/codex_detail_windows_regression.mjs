import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resetLabel } from '../app/src/resetText.js'

// Run: node tests/codex_detail_windows_regression.mjs
// Exercise real component mapping without a JSX loader or React runtime.
const source = readFileSync(new URL('../app/src/components/AiAccounts.jsx', import.meta.url), 'utf8')
const start = source.indexOf('const KNOWN_ROWS =')
const end = source.indexOf('/* ── pieces', start)
assert.ok(start >= 0 && end > start, 'component row mapper must be found')
const usageRows = new Function('resetLabel', `${source.slice(start, end)}\nreturn usageRows`)(resetLabel)
const rows = (usage, provider = 'codex') => usageRows({ provider, usage }).map(({ label, pct, reset }) => ({ label, pct, reset }))

assert.deepEqual(rows({ session_pct: 86, session_reset: '2h', week_pct: 13, week_reset: '4d' }), [
  { label: '5h', pct: 86, reset: '2h' }, { label: 'week', pct: 13, reset: '4d' },
], 'vault Codex exposes both windows and resets')
assert.deepEqual(rows({ sess_pct: 86, sess_reset_ts: '2h', week_pct: 13, week_reset_ts: '4d' }), [
  { label: '5h', pct: 86, reset: '2h' }, { label: 'week', pct: 13, reset: '4d' },
], 'legacy Codex keeps both windows and resets')
const mixed = { session_pct: 0, sess_pct: 99, session_reset: '1h', sess_reset_ts: '9h', week_pct: 100, week_reset: '3d', week_reset_ts: '9d' }
const before = { ...mixed }
assert.deepEqual(rows(mixed), [
  { label: '5h', pct: 0, reset: '1h' }, { label: 'week', pct: 100, reset: '3d' },
], 'canonical keys win without duplicate windows; zero and 100 survive')
assert.deepEqual(mixed, before, 'account usage stays unchanged')
assert.deepEqual(rows({ week_pct: 0, week_reset: '1d' }), [{ label: 'week', pct: 0, reset: '1d' }], 'absent 5h is not invented')
assert.deepEqual(rows(null), [], 'missing usage stays empty')
assert.deepEqual(rows({ session_pct: 12, session_reset: '2h', model_pct: 3, model_name: 'Opus' }, 'claude'), [
  { label: '5h', pct: 12, reset: '2h' }, { label: 'Opus', pct: 3, reset: '' },
], 'Claude mapping unchanged')
assert.deepEqual(rows({ month_pct: 7, month_reset: '2d' }, 'future'), [{ label: 'month', pct: 7, reset: '2d' }], 'generic fallback unchanged')
console.log('PASS Codex detail windows: vault, legacy, mixed, boundaries, missing, Claude, fallback')
