// Regression for app/electron/fsgate.js: no flicker, alt+tab ignored.
// Run: node tests/fsgate_regression.mjs
import { createRequire } from 'node:module'
const require = createRequire(import.meta.url)
const { initialGate, stepGate, HIDE_MS, SHOW_OTHER_MS, SHOW_SAME_MS } = require('../app/electron/fsgate.js')

let pass = 0, fail = 0
const ok = (c, n) => { c ? pass++ : fail++; console.log(`${c ? 'PASS' : 'FAIL'}  ${n}`) }

/** Play a timeline of [t, msg|null] and return the actions with their times. */
function play(events, enabled = true) {
  let g = initialGate(); const out = []
  let due = null
  const tick = (t) => {
    while (due !== null && due <= t) {
      const at = due; due = null
      const r = stepGate(g, null, at, enabled); g = r.gate
      if (r.action) out.push([at, r.action])
      if (r.waitMs) due = at + r.waitMs
    }
  }
  for (const [t, msg] of events) {
    tick(t)
    const r = stepGate(g, msg, t, enabled); g = r.gate
    if (r.action) out.push([t, r.action])
    due = r.waitMs ? t + r.waitMs : null
  }
  tick(1e9)
  return { out, g }
}
const acts = (o) => o.out.map((a) => a[1]).join(',')

const TERM = 11, CHROME = 22, SWITCH = 33
const full = (hwnd) => ({ full: true, hwnd })
const norm = (hwnd) => ({ full: false, hwnd })
const sw = { full: true, hwnd: SWITCH, transient: true }

// a steady full screen hides once, after HIDE_MS
{
  const r = play([[0, full(TERM)]])
  ok(acts(r) === 'hide' && r.out[0][0] === HIDE_MS, 'steady full screen hides after HIDE_MS')
}
// the F11 transition flicker seen in the log: full, not-full for ~1s, full
{
  const r = play([[0, full(TERM)], [1000, norm(TERM)], [2000, full(TERM)], [3000, norm(TERM)], [3900, full(TERM)]])
  ok(acts(r) === 'hide', 'same window wobbling under SHOW_SAME_MS never shows (no flicker)')
}
// a blip of full screen shorter than HIDE_MS does nothing
{
  const r = play([[0, norm(TERM)], [100, full(TERM)], [400, norm(TERM)]])
  ok(!r.out.some((a) => a[1] === 'hide'), 'full screen shorter than HIDE_MS does not hide')
}
// alt+tab away from a full-screen game: the switcher is ignored, target shows fast
{
  const r = play([[0, full(TERM)], [2000, sw], [2600, sw], [3000, norm(CHROME)]])
  const show = r.out.find((a) => a[1] === 'show')
  ok(acts(r) === 'hide,show' && show[0] === 3000 + SHOW_OTHER_MS, 'alt+tab: switcher ignored, new window shows after SHOW_OTHER_MS')
}
// opening alt+tab over a normal desktop must not hide
{
  const r = play([[0, norm(CHROME)], [500, sw], [3000, sw], [4000, norm(CHROME)]])
  ok(!r.out.some((a) => a[1] === 'hide'), 'alt+tab over a normal window never hides')
}
// leaving F11 in the same window shows after SHOW_SAME_MS
{
  const r = play([[0, full(TERM)], [5000, norm(TERM)]])
  const show = r.out.find((a) => a[1] === 'show')
  ok(show && show[0] === 5000 + SHOW_SAME_MS, 'F11 off in the same window shows after SHOW_SAME_MS')
}
// focus moving between normal windows re-raises
{
  const r = play([[0, norm(CHROME)], [500, norm(TERM)], [900, norm(TERM)]])
  ok(acts(r) === 'raise,raise', 'new normal foreground raises, same one does not raise again')
}
// switcher between two normal windows does not raise by itself
{
  const r = play([[0, norm(CHROME)], [500, sw]])
  ok(acts(r) === 'raise', 'transient surface does not raise')
}
// disabled: never hides, still raises
{
  const r = play([[0, full(TERM)], [3000, norm(CHROME)]], false)
  ok(!r.out.some((a) => a[1] === 'hide'), 'disabled never hides')
}
ok(SHOW_OTHER_MS < SHOW_SAME_MS && HIDE_MS < 1000, 'delays are sane')

console.log(`${pass} passed, ${fail} failed, ${pass + fail} total`)
process.exit(fail ? 1 : 0)
