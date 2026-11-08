// Regression for app/electron/main.js: the docked tab's TAB_BLEED overhang
// must never land on a neighbouring monitor. It hangs off an edge only where
// nothing else is beyond that edge (a bare screen edge, or behind the taskbar).
// Run: node tests/tab_bleed_regression.mjs
//
// main.js needs Electron and runs on load, so the geometry functions are cut
// out of its text and evaluated against a stub `screen`. MAIN_JS=<path>
// points the harness at another copy (used to prove it fails on the old code).
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const MAIN = process.env.MAIN_JS || path.join(here, '..', 'app', 'electron', 'main.js')
const src = fs.readFileSync(MAIN, 'utf8')

let pass = 0, fail = 0
const ok = (c, n) => { c ? pass++ : fail++; console.log(`${c ? 'PASS' : 'FAIL'}  ${n}`) }

/** One top-level `const NAME = ...` line, or null. */
function constLine(name) {
  const m = src.match(new RegExp(`^const ${name} = .*$`, 'm'))
  return m ? m[0] : null
}
/** One top-level `function NAME(...) { ... }`, brace-matched, or null. */
function fn(name) {
  const start = src.search(new RegExp(`^function ${name}\\(`, 'm'))
  if (start < 0) return null
  let depth = 0
  for (let i = src.indexOf('{', start); i < src.length; i++) {
    if (src[i] === '{') depth++
    else if (src[i] === '}' && --depth === 0) return src.slice(start, i + 1)
  }
  return null
}

const parts = [
  constLine('TAB_BLEED'), constLine('TAB_INSET'), constLine('HORIZONTAL_EDGES'),
  fn('alongStop'), fn('workAreaAt'), fn('tabBounds'),
]
if (parts.some((p) => !p)) { console.log('FAIL  could not extract tab geometry from main.js'); process.exit(1) }
const helper = fn('bleedFor')

const ctx = { screen: null, tabEdge: 'top', tabAlong: 'start', anchor: () => null, Math }
vm.createContext(ctx)
const api = vm.runInContext(
  [...parts, helper ?? '', ';({ tabBounds, TAB_BLEED, bleedFor: typeof bleedFor === "function" ? bleedFor : null })'].join('\n'),
  ctx,
)
const B = api.TAB_BLEED

// ── displays (DIPs, as screen.getAllDisplays() reports them)
const mk = (id, x, y, w, h, taskbar = 0) => ({
  id, bounds: { x, y, width: w, height: h }, workArea: { x, y, width: w, height: h - taskbar },
})
const PRIMARY = mk(1, 0, 0, 2560, 1600, 48)        // taskbar along the bottom
const LEFT = mk(5, -1920, 0, 1920, 1080, 48)       // DISPLAY5, left of primary
const RIGHT = mk(6, 2560, 0, 1920, 1080, 48)
const LEFT_LOW = mk(7, -1920, 1600, 1920, 1080, 48) // left, but entirely below primary
const ABOVE = mk(8, 0, -1080, 1920, 1080, 48)

function stubScreen(displays) {
  const inside = (d, p) => p.x >= d.bounds.x && p.x < d.bounds.x + d.bounds.width
    && p.y >= d.bounds.y && p.y < d.bounds.y + d.bounds.height
  const dist = (d, p) => {
    const b = d.bounds
    const dx = Math.max(b.x - p.x, 0, p.x - (b.x + b.width))
    const dy = Math.max(b.y - p.y, 0, p.y - (b.y + b.height))
    return dx * dx + dy * dy
  }
  return {
    getAllDisplays: () => displays,
    getPrimaryDisplay: () => displays[0],
    getDisplayNearestPoint: (p) => displays.find((d) => inside(d, p))
      ?? displays.reduce((a, d) => (dist(d, p) < dist(a, p) ? d : a)),
  }
}

/** Dock a 66x300 column / 330x62 row on `edge`/`along`, measured at `at`. */
function dock(displays, edge, along, at) {
  ctx.screen = stubScreen(displays)
  ctx.tabEdge = edge
  ctx.tabAlong = along
  const [w, h] = edge === 'top' || edge === 'bottom' ? [330, 62] : [66, 300]
  return api.tabBounds(w, h, at)
}
const onPrimary = { x: 1280, y: 800 }
const onLeft = { x: -960, y: 500 }

// left edge
{
  const r = dock([PRIMARY, LEFT], 'left', 'start', onPrimary)
  ok(r.x === PRIMARY.workArea.x, `left edge, neighbour on the left: flush, no bleed (x=${r.x}, want 0)`)
}
{
  const r = dock([PRIMARY], 'left', 'start', onPrimary)
  ok(r.x === PRIMARY.workArea.x - B, `left edge, nothing to the left: bleeds (x=${r.x}, want ${-B})`)
}
{
  const r = dock([PRIMARY, LEFT], 'left', 'start', onLeft)
  ok(r.x === LEFT.workArea.x - B, `left edge of the leftmost display: bleeds (x=${r.x})`)
}
{
  // tab runs y 1236..1536 on the primary; DISPLAY5 only spans y 0..1080
  const r = dock([PRIMARY, LEFT], 'left', 'end', onPrimary)
  ok(r.x === -B, `left edge, neighbour vertically disjoint from the tab: bleeds (x=${r.x}, y=${r.y})`)
}
{
  const r = dock([PRIMARY, LEFT_LOW], 'left', 'start', onPrimary)
  ok(r.x === -B, `left edge, neighbour entirely below the primary: bleeds (x=${r.x})`)
}

// right edge
{
  const r = dock([PRIMARY, LEFT], 'right', 'start', onLeft)
  const wa = LEFT.workArea
  ok(r.x + r.width === wa.x + wa.width, `right edge of DISPLAY5, primary on its right: flush (right=${r.x + r.width}, want 0)`)
}
{
  const r = dock([PRIMARY, RIGHT], 'right', 'start', onPrimary)
  ok(r.x + r.width === 2560, `right edge, neighbour on the right: flush (right=${r.x + r.width})`)
}
{
  const r = dock([PRIMARY, LEFT], 'right', 'start', onPrimary)
  ok(r.x + r.width === 2560 + B, `right edge, nothing to the right: bleeds (right=${r.x + r.width})`)
}

// bottom edge -- bleed behind the taskbar is intended and must remain
{
  const r = dock([PRIMARY], 'bottom', 'end', onPrimary)
  ok(r.y + r.height === 1552 + B, `bottom edge, single display: bleeds behind the taskbar (bottom=${r.y + r.height})`)
}
{
  const r = dock([PRIMARY, LEFT], 'bottom', 'start', onPrimary)
  ok(r.y + r.height === 1552 + B, `bottom edge, neighbour beside (not below): still bleeds (bottom=${r.y + r.height})`)
}
{
  // DISPLAY5's bottom is 1080, its taskbar to 1080; LEFT_LOW is not below it here
  const r = dock([PRIMARY, LEFT], 'bottom', 'start', onLeft)
  ok(r.y + r.height === 1032 + B, `bottom edge of DISPLAY5: bleeds behind its taskbar (bottom=${r.y + r.height})`)
}

// top edge
{
  const r = dock([PRIMARY, LEFT], 'top', 'end', onPrimary)
  ok(r.y === -B, `top edge, nothing above: bleeds (y=${r.y})`)
}
{
  const r = dock([PRIMARY, ABOVE], 'top', 'start', onPrimary)
  ok(r.y === 0, `top edge, neighbour above the tab's span: flush (y=${r.y})`)
}
{
  // tab at the end of the top edge runs x 2214..2544; ABOVE only spans x 0..1920
  const r = dock([PRIMARY, ABOVE], 'top', 'end', onPrimary)
  ok(r.y === -B, `top edge, neighbour above but horizontally disjoint: bleeds (y=${r.y})`)
}

// the helper itself, pure: (edge, rect flush at the work-area edge, displays)
{
  const f = api.bleedFor
  ok(typeof f === 'function', 'bleedFor(edge, rect, displays) exists')
  if (f) {
    const all = [PRIMARY, LEFT].map((d) => d.bounds)
    ok(f('left', { x: 0, y: 16, width: 66, height: 300 }, all) === 0, 'bleedFor: left seam with DISPLAY5 -> 0')
    ok(f('left', { x: 0, y: 1236, width: 66, height: 300 }, all) === B, 'bleedFor: left, below DISPLAY5 -> TAB_BLEED')
    ok(f('bottom', { x: 16, y: 1490, width: 330, height: 62 }, all) === B, 'bleedFor: bottom behind own taskbar -> TAB_BLEED')
  }
}

console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail ? 1 : 0)
