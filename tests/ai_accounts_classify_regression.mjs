// Regression for audit D16 F4 + F2 (AiAccounts.jsx): login verdict and stale dimming.
// Run: node tests/ai_accounts_classify_regression.mjs
import { readFileSync } from 'node:fs'

let pass = 0, fail = 0
const is = (got, want, name) => {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  ok ? pass++ : fail++
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : `  got ${JSON.stringify(got)} want ${JSON.stringify(want)}`}`)
}

// AiAccounts.jsx is JSX; slice the plain functions out of it.
const src = readFileSync(new URL('../app/src/components/AiAccounts.jsx', import.meta.url), 'utf8')
const grab = (head) => {
  const i = src.indexOf(head)
  if (i < 0) throw new Error(`${head} missing`)
  return src.slice(i, src.indexOf('\n}\n', i) + 2)
}
const { classify, statusChip } = new Function(
  [grab('function classify('), grab('function statusChip('), 'return { classify, statusChip }'].join('\n'),
)()

// F4: generic auth text is not proof of a dead login.
is(classify({ error: 'unauthorized', needs_login: false }).kind, 'http', 'F4 unauthorized without needs_login is not expired')
is(classify({ error: 'HTTP 401 Unauthorized', needs_login: false }).kind, 'http', 'F4 401 unauthorized stays refresh failure')
is(classify({ error: 'not logged in' }).kind, 'http', 'F4 "not logged in" text alone is not expired')
is(classify({ error: 'no credential' }).kind, 'http', 'F4 "no credential" text alone is not expired')
is(classify({ error: 'unauthorized', needs_login: true }).kind, 'expired', 'F4 needs_login still expires')
is(classify({ error: 'LOGIN_EXPIRED:codex' }), { kind: 'expired', short: 'login expired', full: 'Login expired — click to sign in' }, 'F4 sentinel still expires, hidden')
is(classify({ error: 'offline' }).kind, 'offline', 'F4 offline unchanged')
is(classify({ error: null }), null, 'F4 no error -> null')

// F2: explicit stale flag drives the stale chip and quota dimming.
is(statusChip({ stale: true }, false, null).text, 'stale', 'F2 stale without held shows stale chip')
is(statusChip({ held: true }, false, null).text, 'stale', 'F2 held still shows stale chip')
is(statusChip({}, false, null).text, 'ok', 'F2 fresh row ok')
const dim = /<QuotaRow [^>]*dim=\{([^}]*)\}/.exec(src)?.[1]
is(new Function('account', `return ${dim}`)({ stale: true }), true, 'F2 QuotaRow dims stale rows')
is(new Function('account', `return ${dim}`)({ held: true }), true, 'F2 QuotaRow dims held rows')
is(new Function('account', `return ${dim}`)({}), false, 'F2 QuotaRow fresh not dimmed')

console.log(`${pass} passed, ${fail} failed`)
process.exit(fail ? 1 : 0)
