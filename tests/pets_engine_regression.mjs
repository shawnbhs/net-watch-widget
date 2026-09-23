/**
 * Regression harness for the pet engine's world-clamping and animation loop.
 *
 * HOW TO RUN
 * ----------
 * From the repository root, with nothing installed — no npm, no vite, no
 * Electron, no dependency of any kind beyond Node itself (>= 18):
 *
 *     node tests/pets_engine_regression.mjs
 *
 * It exits 0 when every test passes and non-zero on the first failure, and it
 * prints a `# pass` / `# fail` summary. To run it against a DIFFERENT copy of
 * the engine — which is how you prove the tests are not vacuous — point the
 * `PETS_ENGINE` environment variable at that file:
 *
 *     git show HEAD:app/src/pets/engine.js > /tmp/engine-prefix.js
 *     PETS_ENGINE=/tmp/engine-prefix.js node tests/pets_engine_regression.mjs
 *
 * Against the pre-fix engine this suite is expected to FAIL loudly; that is the
 * point of it.
 *
 * WHY IT IS SHAPED LIKE THIS
 * --------------------------
 * The rest of tests/ is Python. This one cannot be: the code under test is the
 * browser-side `app/src/pets/engine.js`. It needs no runner beyond Node's own
 * built-in `node:test` + `node:assert`, so a stranger who has cloned the repo
 * can run it immediately.
 *
 * Two details that look odd and are deliberate:
 *
 *  - The engine is COPIED to a temporary `.mjs` file before it is imported.
 *    `app/package.json` has no `"type": "module"`, so Node resolves
 *    `app/src/pets/engine.js` as CommonJS and chokes on its `export` keywords.
 *    Copying the bytes to a `.mjs` extension is the smallest fix that keeps the
 *    engine file itself untouched, and it is what makes the `PETS_ENGINE`
 *    override a one-line change.
 *  - Time and animation frames are FAKE. `performance.now()` is a counter this
 *    file advances by hand and `requestAnimationFrame` is a `Map` this file
 *    drains by hand. Nothing here waits on a real frame or on wall-clock
 *    milliseconds, because a test that did would be flaky and worse than none.
 *
 * EVERY TEST IS ISOLATED
 * ----------------------
 * Tests are registered through the local `test()` wrapper below, never through
 * `node:test` directly. The wrapper resets the whole fake environment -- clock,
 * rAF queue, rAF id counter AND the pseudo-random seed -- before each test, and
 * refuses to let a test leak a scheduled frame into the next one.
 *
 * That is not tidiness. Three of the assertions in this file are statistical
 * ("no more than N hop take-offs in 1200 frames"), and the numbers they see are
 * a function of where in the `Math.random()` sequence the test starts. With a
 * single module-level seed, inserting ANY test anywhere shifts that sequence
 * for every test after it, so a perfectly good new test could turn an unrelated
 * old one red -- the most expensive kind of failure, because the red test is
 * not the broken one. Re-seeding per test makes each test's random sequence a
 * property of the test alone.
 *
 * WHAT IT PROTECTS
 * ----------------
 * The user's bug: with the widget zoomed down, pets escaped the window and were
 * sliced by `overflow: hidden` — one was measured ~125px (nine sprite widths)
 * outside the window at 55% zoom. The cause is that every platform clamp is
 * expressed against a CARD's span, and at the instant the window shrinks those
 * cards still describe the old, wider window. The fix routes every position
 * through `Pet.clampToWorld()`, whose bounds are the measured world box and so
 * cannot go stale.
 *
 * The headline case, preserved verbatim below: a pet at `y=200` in a world
 * shrunk to `h=110` sat 90px BELOW the visible bottom edge before the fix, and
 * lands exactly on `y=110` after it.
 */

import nodeTest from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

// ── environment stubs, installed before the engine is imported ───────────────

/**
 * The least DOM the engine can be constructed against.
 *
 * `Pet.build()` creates two elements and appends them to the stage; everything
 * else the engine does to the DOM is a write to `.style` or `.classList`, which
 * a plain object absorbs. Nothing here is asserted on — the tests read the
 * pet's own numeric state, which is what the renderer would have drawn.
 */
const makeEl = () => ({
  style: {},
  dataset: {},
  children: [],
  classList: { add() {}, remove() {}, toggle() {} },
  appendChild(child) { this.children.push(child); return child },
  remove() {},
  setAttribute() {},
  removeAttribute() {},
  addEventListener() {},
  removeEventListener() {},
  getBoundingClientRect: () => ({
    x: 0, y: 0, width: 0, height: 0, left: 0, top: 0, right: 0, bottom: 0,
  }),
})

globalThis.document = { createElement: makeEl, createElementNS: makeEl }

/** Fake clock. Only ever moved by `tick()`. */
let clock = 0
globalThis.performance = { now: () => clock }

/** Fake rAF: a queue this file drains, so "a frame" is a function call. */
let nextRafId = 1
const rafQueue = new Map()
globalThis.requestAnimationFrame = (cb) => {
  const id = nextRafId++
  rafQueue.set(id, cb)
  return id
}
globalThis.cancelAnimationFrame = (id) => { rafQueue.delete(id) }

/** Run every queued callback once. Returns how many fired. */
const tick = (ms = 16) => {
  clock += ms
  const due = [...rafQueue.values()]
  rafQueue.clear()
  for (const cb of due) cb(clock)
  return due.length
}
/** How many frames are currently scheduled. 0 means the loop has parked. */
const pending = () => rafQueue.size

// Determinism: the engine sprinkles `Math.random()` through pet setup (idle
// timers, tempo, bob phase). None of it is asserted on, but a fixed sequence
// means a failure here reproduces exactly rather than one run in ten.
//
// The seed is RESET BEFORE EVERY TEST, not once at load. See `test()` below:
// seeding once module-wide makes each test's random sequence depend on how many
// `Math.random()` calls every earlier test happened to make, which silently
// couples the statistical assertions in section 8.4 to the order and the
// existence of every other test in the file.
const SEED = 0x2f6e2b1
let seed = SEED
Math.random = () => {
  seed = (seed * 1103515245 + 12345) & 0x7fffffff
  return seed / 0x80000000
}

/**
 * Put the whole fake environment back where it started.
 *
 * Every piece of mutable module state the engine can see or be seen through:
 * the clock, the frame queue, the handle counter, and the RNG.
 */
const resetEnv = () => {
  clock = 0
  rafQueue.clear()
  nextRafId = 1
  seed = SEED
}

/**
 * Register a test that cannot be influenced by, or influence, any other test.
 *
 * Before: the environment is reset.
 * After: the test must not have left a frame scheduled. A test that calls
 * `world.start()` and walks away hands the NEXT test a live rAF callback which
 * fires inside its `tick()` and moves a pet it has never heard of. Three tests
 * in this file used to defend against that by calling `rafQueue.clear()` on the
 * way IN, which only works for the tests that remember to do it. Enforcing it
 * on the way OUT names the guilty test instead of the victim.
 *
 * Opt out with `{ leaks: true }` only where a parked/armed loop IS the thing
 * under test at the point the test ends.
 */
const test = (name, opts, fn) => {
  if (typeof opts === 'function') { fn = opts; opts = {} }
  return nodeTest(name, () => {
    resetEnv()
    try {
      fn()
      if (!opts.leaks) {
        assert.equal(
          rafQueue.size, 0,
          `'${name}' finished with ${rafQueue.size} frame(s) still scheduled. `
          + 'A leaked frame runs inside the NEXT test\'s tick() and moves a pet that '
          + 'test does not own. Call world.stop() (or pass { leaks: true } if a live '
          + 'loop is genuinely the end state under test).',
        )
      }
    } finally {
      resetEnv()
    }
  })
}

// ── load the engine ──────────────────────────────────────────────────────────

const here = path.dirname(fileURLToPath(import.meta.url))
const enginePath = process.env.PETS_ENGINE
  ? path.resolve(process.env.PETS_ENGINE)
  : path.join(here, '..', 'app', 'src', 'pets', 'engine.js')

const shim = path.join(
  fs.mkdtempSync(path.join(os.tmpdir(), 'pets-engine-test-')),
  'engine.mjs',
)
fs.writeFileSync(shim, fs.readFileSync(enginePath))
process.on('exit', () => {
  try { fs.rmSync(path.dirname(shim), { recursive: true, force: true }) } catch { /* best effort */ }
})

const { World } = await import(pathToFileURL(shim).href)

// ── fixtures ─────────────────────────────────────────────────────────────────

/**
 * A sprite sheet whose character box fills a 32x32 canvas exactly.
 *
 * With `baseH: 32` and a world size of 26 the scale `k` is 26/32, so a pet is
 * 26px wide and 26px tall — small round numbers the expected values below are
 * worked out from by hand.
 */
const VARIANT = () => {
  const box = [32, 32, 0, 0, 32, 32] // cw, ch, bx, by, bw, bh
  const kinds = ['idle', 'walk', 'run', 'air', 'land', 'held', 'swipe']
  return {
    baseH: 32,
    clips: Object.fromEntries(kinds.map((k) => [k, `${k}.png`])),
    m: Object.fromEntries(kinds.map((k) => [k, box])),
  }
}

const SPEC = (id, extra = {}) => ({
  id,
  species: { id: 'test', scale: 1 },
  variant: VARIANT(),
  platformId: 'card1',
  dir: 1,
  ...extra,
})

const CARD = { id: 'card1', x1: 20, x2: 380, y: 200 }

/** A 400x300 world with one wide card at y=200, and one pet standing on it. */
const scene = ({ x = 370, y = 200 } = {}) => {
  const world = new World({
    stage: makeEl(),
    assetBase: '/assets/pets/',
    opts: { size: 26 },
    scale: 1,
  })
  world.setSize(400, 300)
  world.setPlatforms([{ ...CARD }])
  const pet = world.add(SPEC('p1'))
  pet.x = x
  pet.y = y
  return { world, pet }
}

// ── 0. the taskbar floor ──────────────────────────────────────

// The overlay covers whole DISPLAYS, not work areas, so the bottom of the
// world runs behind the taskbar. `clampToWorld` used the world's bottom as its
// lower bound, which is a row of pixels the user cannot click and the pet
// should never stand on -- it walked across the clock. `roamBounds()` already
// knew the right floor; the clamp ran after it on nine call sites and undid it.

test('a pet driven past the bottom rests on the taskbar edge, not behind it', () => {
  const world = new World({
    stage: makeEl(),
    assetBase: '/assets/pets/',
    opts: { size: 26 },
    scale: 1,
  })
  // World 400 tall; the single display's work area ends 40px above that, the
  // way a taskbar does.
  world.setSize(400, 400)
  world.setDisplays([{ id: 1, x: 0, y: 0, width: 400, height: 400, floor: 360 }])

  const pet = world.add(SPEC('p1', { mode: 'free', platformId: null }))
  pet.x = 200
  pet.y = 395
  pet.clampToWorld()

  assert.equal(
    pet.y, 360,
    'the pet must stop at the work-area floor; pre-fix it clamped to the world '
    + 'bottom (400) and stood on the taskbar',
  )
})

test('the floor clamp never pushes a pet below the world when no display is known', () => {
  const world = new World({
    stage: makeEl(),
    assetBase: '/assets/pets/',
    opts: { size: 26 },
    scale: 1,
  })
  world.setSize(400, 400)
  world.setDisplays([])   // display map not delivered yet

  const pet = world.add(SPEC('p1', { mode: 'free', platformId: null }))
  pet.x = 200
  pet.y = 900
  pet.clampToWorld()

  assert.ok(
    pet.y > 0 && pet.y <= 400,
    `a missing display map must fall back to the world box, got y=${pet.y}`,
  )
})

// ── 1. the headline case ─────────────────────────────────────────────────────

test('a pet at y=200 in a world shrunk to h=110 lands on 110, not 90px below it', () => {
  const { world, pet } = scene()

  assert.equal(pet.h, 26, 'fixture sanity: the pet is 26px tall')
  assert.equal(pet.y, 200, 'fixture sanity: the pet starts on the card at y=200')

  // Zoom out. The cards have NOT been re-measured yet, so `card1` still claims
  // to span x 20..380 at y=200 — a window that no longer exists. This stale
  // moment is the whole bug.
  world.setSize(140, 110)

  assert.equal(
    pet.y, 110,
    `pet must be pulled up onto the new bottom edge; pre-fix it stayed at 200, `
    + `which is ${(200 - 110)}px below the visible area and exactly the slicing the user saw`,
  )
  assert.ok(pet.y <= world.h, `pet.y=${pet.y} must never exceed world.h=${world.h}`)
  assert.ok(pet.y >= pet.h, `pet.y=${pet.y} must leave room for its own height ${pet.h}`)
})

// ── 2. clamping on a setSize shrink — what zooming out actually does ─────────

test('World.setSize shrink clamps the pet on both axes', () => {
  const { world, pet } = scene()

  world.setSize(140, 110)

  const halfW = pet.w * 0.5
  assert.ok(
    pet.x >= halfW - 1e-9 && pet.x <= world.w - halfW + 1e-9,
    `x=${pet.x} must sit within [${halfW}, ${world.w - halfW}]`,
  )
  assert.ok(
    pet.y >= pet.h - 1e-9 && pet.y <= world.h + 1e-9,
    `y=${pet.y} must sit within [${pet.h}, ${world.h}]`,
  )
  // Worked by hand: xf = 370/400 = 0.925 -> 0.925 * 140 = 129.5, which the card
  // clamp leaves alone (it still believes in x 20..380) and the world clamp
  // pulls back to 140 - 13 = 127.
  assert.equal(pet.x, 127, 'the right edge, not 129.5 out past it')
})

test('a series of shrinks never leaves the pet outside the world', () => {
  const { world, pet } = scene()

  // 100% down to 35%, the range the zoom control actually covers.
  for (const zoom of [1, 0.85, 0.7, 0.55, 0.45, 0.35]) {
    world.setSize(Math.round(400 * zoom), Math.round(300 * zoom))
    const halfW = pet.w * 0.5
    const lo = Math.min(halfW, world.w - halfW)
    const hi = Math.max(halfW, world.w - halfW)
    assert.ok(
      pet.x >= lo - 1e-9 && pet.x <= hi + 1e-9,
      `at zoom ${zoom}: x=${pet.x} escaped [${lo}, ${hi}]`,
    )
    assert.ok(
      pet.y <= Math.max(pet.h, world.h) + 1e-9,
      `at zoom ${zoom}: y=${pet.y} fell below the bottom edge h=${world.h}`,
    )
  }
})

// ── 3. clamp() with inverted bounds returns the midpoint ─────────────────────

test('a pet wider than the world is centred, not pinned to an edge', () => {
  const { world, pet } = scene()

  // 20px wide, with a 26px pet in it: the clamp range [13, 7] is inverted.
  // Sticking to either bound puts half the sprite outside; the midpoint is the
  // only answer that keeps it symmetric, and it is the world's own centre.
  world.setSize(20, 20)

  assert.equal(pet.x, 10, 'x must be the midpoint of the inverted range [13, 7]')
  assert.equal(pet.x, world.w / 2, 'which is the centre of the world')
  assert.equal(pet.y, (pet.h + world.h) / 2, 'y likewise: midpoint of [26, 20]')
})

// ── 4. dragTo is clamped on both axes ────────────────────────────────────────

test('dragTo clamps a cursor dragged off the edge of the world', () => {
  const { world, pet } = scene()

  pet.grab(pet.x, pet.y)          // grabDX / grabDY are both 0
  tick(0)                         // move the fake clock off the grab instant

  pet.dragTo(9999, 9999)
  assert.equal(pet.x, world.w - pet.w * 0.5, 'dragged right: stops half a sprite short of the edge')
  assert.equal(pet.y, world.h, 'dragged down: feet stop on the bottom edge')

  pet.dragTo(-9999, -9999)
  assert.equal(pet.x, pet.w * 0.5, 'dragged left: stops half a sprite in')
  assert.equal(pet.y, pet.h, 'dragged up: feet stop one body-height down, so the head stays visible')
})

// ── 5. the rAF loop stops, parks, and wakes ──────────────────────────────────

// `{ leaks: true }`: the END STATE of this test is an armed loop, which is
// exactly what it asserts. The wrapper still resets the queue afterwards.
test('start() schedules exactly one frame and is idempotent', { leaks: true }, () => {
  const { world } = scene()

  world.start()
  assert.equal(pending(), 1, 'start() schedules one frame')
  world.start()
  world.start()
  assert.equal(pending(), 1, 'repeated start() (PetLayer calls it every render) adds nothing')

  tick()
  assert.equal(pending(), 1, 'the loop re-arms while pets exist')
})

test('stop() really cancels the queued frame', () => {
  const { world } = scene()

  world.start()
  assert.equal(pending(), 1)

  world.stop()
  assert.equal(world.running, false, 'stop() clears running')
  assert.equal(
    pending(), 0,
    'stop() must cancelAnimationFrame the pending handle — flipping the flag alone '
    + 'left the overlay compositing forever behind a layer nobody was looking at',
  )
  assert.equal(tick(), 0, 'nothing runs after stop()')
})

test('the loop parks when the last pet is removed and wakes on add()', { leaks: true }, () => {
  const { world, pet } = scene()

  world.start()
  tick()
  assert.equal(pending(), 1, 'armed while a pet exists')

  world.remove(pet.id)
  assert.equal(world.pets.length, 0)

  tick()
  assert.equal(pending(), 0, 'the frame after the world empties must park, not re-arm')
  assert.equal(world.running, true, 'parked is not stopped: running stays true')
  tick()
  tick()
  assert.equal(pending(), 0, 'and it stays parked')

  world.add(SPEC('p2'))
  assert.equal(pending(), 1, 'add() must wake the parked loop — otherwise the new pet never moves')

  tick()
  assert.equal(pending(), 1, 'and the loop keeps running from there')

  // add() while already armed must not stack a second frame.
  world.add(SPEC('p3'))
  assert.equal(pending(), 1, 'add() while armed does not double-schedule')
})

test('an empty world that start()s parks after a single frame', { leaks: true }, () => {
  const world = new World({
    stage: makeEl(), assetBase: '/assets/pets/', opts: { size: 26 }, scale: 1,
  })
  world.setSize(400, 300)

  world.start()
  assert.equal(pending(), 1)
  tick()
  assert.equal(pending(), 0, 'a world with nothing to draw parks immediately')

  world.add(SPEC('p1', { platformId: null, mode: 'free' }))
  assert.equal(pending(), 1, 'and wakes when it is given something to draw')
})

test('add() while stopped does not start a loop the host never asked for', () => {
  const { world } = scene()

  world.start()
  world.stop()
  assert.equal(pending(), 0)

  world.add(SPEC('p2'))
  assert.equal(pending(), 0, 'wake() respects running === false')
})

// ── 6. setPlatforms no longer gates on list.length ───────────────────────────

test('setPlatforms([]) drops an orphaned pet into air instead of stranding it', () => {
  const { world, pet } = scene()
  assert.equal(pet.platformId, 'card1')

  // Every card gone — a mode switch, or a layout that measured nothing yet.
  world.setPlatforms([])

  assert.equal(pet.state, 'air', 'the pet falls rather than floating over ground that is gone')
  assert.equal(pet.platformId, null, 'and lets go of the card that no longer exists')
})

// ── 7. layoutClip floors the canvas so a tiny pet stays clickable ────────────

test('a very small pet still has a non-zero canvas', () => {
  const world = new World({
    stage: makeEl(), assetBase: '/assets/pets/', opts: { size: 1 }, scale: 0.35,
  })
  world.setSize(140, 110)
  world.setPlatforms([{ ...CARD }])
  const pet = world.add(SPEC('tiny', { size: 0.1 }))

  const w = parseFloat(pet.el.style.width)
  const h = parseFloat(pet.el.style.height)
  assert.ok(w >= 1, `canvas width ${pet.el.style.width} must not round to 0px — a 0px img is invisible and unclickable`)
  assert.ok(h >= 1, `canvas height ${pet.el.style.height} must not round to 0px`)
})

// ── 8. mixed-DPI, two monitors, one CSS grid ─────────────────────────────────
//
// Everything below this line is about the SECOND defect: a pet's size had no
// per-display term at all, so the same sprite was 63.7% physically bigger on
// the low-density panel than on the dense one. Position is already tested
// above; size across a seam was not tested anywhere.
//
// These tests run through the SAME engine the fourteen above do -- the
// byte-copy import at the top of this file -- so `PETS_ENGINE=/path/to/other/engine.js`
// swaps them onto any other copy of the engine exactly as it does for the
// older tests. That is the anti-vacuity property: point this suite at the
// pre-fix engine and the parity test below must go red, because `applySize()`
// there has no positional input and both reads come back 26.

/**
 * The real two-monitor desktop, reproduced exactly as `overlayBounds()` in
 * `app/electron/pets.js` emits it.
 *
 * UNITS. These are the renderer's CSS px, which is a device pixel divided by
 * the WINDOW's single scale factor. Windows gives the whole overlay window one
 * scale factor -- the densest panel it meaningfully overlaps, 1.65 here -- and
 * that one divisor applies across the 1.10 monitor too. That is the entire
 * problem in one sentence.
 *
 * These are NOT DIPs. The secondary is 1746 DIP wide and appears here as
 * 1164.24 CSS px. "Correcting" that back to 1746 reintroduces the DIP/CSS
 * confusion this fixture exists to catch, so it is asserted against below.
 */

/** The one divisor Windows applies to the whole overlay window. */
const WINDOW_SF = 1.65

/** Device px -> the renderer's CSS px. */
const css = (v) => v / WINDOW_SF

/**
 * Real, EDID-measured pixels-per-inch. Deliberately NOT put in the display
 * payload by default: the fixture's default flavour is the `sf` FALLBACK path,
 * which is the one that has to work on every machine, because EDID physical
 * size is not always readable. The EDID path gets its own test.
 */
const REAL_PPI = { secondary: 102.30, primary: 167.76 }

const MIXED_DPI = {
  windowScaleFactor: WINDOW_SF,

  /** The overlay frame in device px: x -1920..2561, y 0..1601. */
  device: { x: -1920, y: 0, width: 4481, height: 1601 },

  /** The rectangle the window ASKS for, in DIPs. Never handed to the renderer. */
  unionDip: { x: -1746, y: 0, width: 3298, height: 1137 },

  worldWidth: css(4481),    // 2715.757575757576
  worldHeight: css(1601),   //  970.3030303030303

  // Order matters. The two rects OVERLAP by 0.606 CSS px at the seam
  // (secondary right edge 1164.2424 vs primary left edge 1163.6364), because
  // the secondary's device width is 1921 -- one px wider than its 1920 native
  // panel. `displayAt()` resolves an exact hit by iteration order, so a point
  // in that sliver belongs to whichever entry comes first. Secondary first,
  // and there is a test pinning it.
  displays: [
    {
      // ── SECONDARY: physically LEFT, negative DIP x, LOW density ──────────
      id: 2121993299,
      x: css(0),            //    0                (device -1920 - -1920)
      y: css(255),          //  154.54545454545453
      width: css(1921),     // 1164.2424242424242  (DEVICE width; NOT 1746 DIP)
      height: css(1082),    //  655.7575757575758
      // No taskbar on this monitor: work area == bounds.
      floor: css(1337),     //  810.3030303030303  (255 + 1082)
      sf: 1.10,             // the OS scale the USER chose for this panel
      ppi: 0,               // 0 == unknown, per the payload contract
    },
    {
      // ── PRIMARY: physically RIGHT, origin at 0,0, HIGH density ───────────
      id: 3828506604,
      x: css(1920),         // 1163.6363636363637
      y: css(0),            //    0
      width: css(2561),     // 1552.121212121212
      height: css(1601),    //  970.3030303030303
      // A 43 DIP taskbar: the work area is 927 of 970 DIP. 927 * 1.65 =
      // 1529.55, which Electron's dipToScreenRect hands back as the integer
      // 1530 device px.
      floor: css(1530),     //  927.2727272727273
      sf: 1.65,
      ppi: 0,
    },
  ],

  // Landmarks.
  seamX: css(1920),                        // 1163.6363636363637
  overlapCss: css(1921) - css(1920),       //    0.6060606060606233
  midSecondary: css(1921) / 2,             //  582.1212121212121
  midPrimary: css(1920) + css(2561) / 2,   // 1939.6969696969697
}

/** The same two screens with EDID densities filled in, for the `ppi` path. */
const MIXED_DPI_EDID = MIXED_DPI.displays.map((d, i) => ({
  ...d,
  ppi: i === 0 ? REAL_PPI.secondary : REAL_PPI.primary,
}))

/** The same two screens as a payload that predates `sf`/`ppi` entirely. */
const MIXED_DPI_LEGACY = MIXED_DPI.displays.map(
  ({ id, x, y, width, height, floor }) => ({ id, x, y, width, height, floor }),
)

/**
 * `scene()`'s sibling: a world that IS the real mixed-DPI desktop.
 *
 * Deliberately not a variant of `scene()` and deliberately card-less. Crossing
 * a monitor seam is a `mode: 'free'` concern; a platform pet is pinned to a
 * card's span, and that clamp would mask every display effect under it.
 *
 * `y` defaults to 700 rather than something nearer the floor because 700 is
 * inside the roam band of BOTH screens (secondary 810.30 floor, primary 927.27)
 * at every size these tests use. A y outside one of those bands makes
 * `settleToFloor()` launch a hop, and a hopping pet is refused a resize by the
 * state gate -- correct behaviour, but it would make a size reading depend on
 * the order the test happened to take it in.
 */
const mixedScene = ({
  x = MIXED_DPI.midPrimary,
  y = 700,
  size = 26,
  scale = 1,
  displays = MIXED_DPI.displays,
  windowScale = WINDOW_SF,
} = {}) => {
  const world = new World({
    stage: makeEl(),
    assetBase: '/assets/pets/',
    opts: { size },
    scale,
  })
  world.setSize(MIXED_DPI.worldWidth, MIXED_DPI.worldHeight)
  world.setDisplays(displays.map((d) => ({ ...d })), windowScale)
  world.setPlatforms([])
  const pet = world.add(SPEC('p1', { mode: 'free', platformId: null, x, y }))
  return { world, pet, fx: MIXED_DPI }
}

/**
 * Put the pet at `x` and let the engine re-size it, through the one public
 * path that is allowed to change a live pet's size.
 *
 * `applyDisplaySize()` is named here on purpose: it is the entry point the
 * per-frame hook in `update()` calls, so a test that goes through it is
 * testing the same code a real frame runs, without having to tick a loop and
 * hope the pet wandered to the right place.
 *
 * THE GUARD. `applyDisplaySize()` returns immediately and silently when the
 * pet is `held`, `air` or `hop` -- correct behaviour, tested in section 8.5,
 * and a trap for every OTHER test. Crossing a seam whose floors differ makes
 * `settleToFloor()` launch a hop, so the pet can be left in `hop` by one call
 * and refuse the NEXT one. A test that then asserts "the size did not change"
 * would pass for a reason it never meant, and a test asserting the opposite
 * would fail somewhere far from the cause.
 *
 * So the size helper refuses to be used on a gated pet at all, by name. A test
 * that genuinely wants the gated path calls `pet.applyDisplaySize()` directly
 * -- which section 8.5 does -- and `settle()` below is how a test that wants a
 * resize gets a hopping pet back on its feet first.
 */
const GATED = new Set(['held', 'air', 'hop'])

const sizeAt = (pet, x) => {
  assert.ok(
    !GATED.has(pet.state),
    `sizeAt() called on a pet in state '${pet.state}', which applyDisplaySize() `
    + 'refuses outright. The reading would be the OLD size and the test would be '
    + 'asserting on a no-op. Use settle(world, pet) first, or call '
    + 'pet.applyDisplaySize() directly if the gate is what you are testing.',
  )
  pet.x = x
  pet.applyDisplaySize()
  return pet.h
}

/**
 * Land a pet that a previous seam crossing left mid-hop, and prove it landed.
 *
 * A crossing between screens with different floors is *supposed* to hop: see
 * `settleToFloor()`. This drains that hop with real frames so the following
 * assertions run against a pet that is standing, not one frozen in an arc.
 *
 * It needs the world because a hop only advances inside the engine's own frame
 * loop -- there is no "finish this arc" entry point, and inventing one in the
 * test would be testing a mechanism the product does not have.
 */
const settle = (world, pet, frames = 600) => {
  const wasRunning = world.running
  if (!wasRunning) world.start()
  for (let i = 0; i < frames && GATED.has(pet.state); i++) tick(16)
  if (!wasRunning) world.stop()
  assert.ok(
    !GATED.has(pet.state),
    `pet stuck in '${pet.state}' after ${frames} frames -- that is the permanent `
    + 'hop loop section 8.4 exists to catch, surfacing from a different direction',
  )
  return pet
}

// ── 8.1 the fixture is not lying ─────────────────────────────────────────────

test('mixed-DPI fixture: both screens install, and the numbers are CSS px not DIPs', () => {
  const { world, fx } = mixedScene()
  const [lo, hi] = world.displays

  assert.equal(
    world.displays.length, 2,
    'neither entry may be dropped by setDisplays\' Number.isFinite filter',
  )

  // pets.js:417 asserts exactly this invariant on the Electron side: the
  // rightmost display edge IS the right edge of the world.
  const span = Math.max(...world.displays.map((d) => d.x + d.width))
  assert.ok(
    Math.abs(span - world.w) <= 0.5,
    `display span ${span} must equal world width ${world.w}`,
  )

  // The desktop starts at -1746 DIP, but nothing the renderer ever sees is
  // negative: the overlay frame's own left edge is the origin.
  assert.equal(lo.x, 0, 'the world origin is the LEFT monitor\'s left edge')

  // The DIP/CSS trap, pinned. 1746 is the secondary's width in DIPs; in the
  // renderer it is 1164.24, because the window divides by 1.65 everywhere.
  assert.notEqual(
    Math.round(lo.width), 1746,
    'the secondary is 1746 DIP wide but 1164.24 CSS px wide; writing 1746 here '
    + 'is the exact DIP/CSS confusion pets.js warns about at length',
  )
  assert.ok(Math.abs(lo.width - 1164.2424) < 0.001, `secondary width ${lo.width}`)
  assert.ok(Math.abs(hi.width - 1552.1212) < 0.001, `primary width ${hi.width}`)
  assert.ok(Math.abs(hi.x - 1163.6364) < 0.001, `primary left edge ${hi.x}`)

  // Two genuinely different taskbar geometries -- something a single-display
  // fixture cannot express at all.
  assert.ok(Math.abs(lo.floor - 810.3030) < 0.001, `secondary floor ${lo.floor}`)
  assert.ok(Math.abs(hi.floor - 927.2727) < 0.001, `primary floor ${hi.floor}`)
  assert.notEqual(lo.floor, hi.floor, 'the two monitors must not share a floor')

  assert.equal(world.displayAt(fx.midSecondary, 400).id, 2121993299)
  assert.equal(world.displayAt(fx.midPrimary, 400).id, 3828506604)

  // The 0.606px overlap sliver. Both screens claim this column; iteration
  // order decides, and the secondary is first. If this ever becomes the
  // primary it must be a deliberate change, not a drift.
  assert.ok(fx.overlapCss > 0.6 && fx.overlapCss < 0.61, `overlap ${fx.overlapCss}`)
  assert.equal(
    world.displayAt(fx.seamX + 0.3, 400).id, 2121993299,
    'a point inside the 0.606px seam overlap resolves to the FIRST entry',
  )
})

test('mixed-DPI fixture: each screen keeps its own floor under clampToWorld', () => {
  const { world, pet, fx } = mixedScene()
  const [lo, hi] = world.displays

  pet.x = fx.midSecondary; pet.y = 9999; pet.clampToWorld()
  assert.ok(Math.abs(pet.y - lo.floor) < 0.5, `must rest on the secondary floor 810.30, got ${pet.y}`)

  pet.x = fx.midPrimary; pet.y = 9999; pet.clampToWorld()
  assert.ok(Math.abs(pet.y - hi.floor) < 0.5, `must rest on the primary floor 927.27, got ${pet.y}`)
})

// ── 8.2 physical parity: equal millimetres means UNEQUAL pixels ──────────────

test('a pet is the same PHYSICAL size on both monitors, which means a DIFFERENT pixel size', () => {
  const { world, pet, fx } = mixedScene()
  const [lo, hi] = world.displays

  // One world, read twice, because there is only ever one overlay window. The
  // size is supposed to be a function of POSITION, so position is the input.
  const hHi = sizeAt(pet, fx.midPrimary)     // the dense panel
  const wHi = pet.w
  const hLo = sizeAt(pet, fx.midSecondary)   // the sparse panel
  const wLo = pet.w

  // ASSERTION 1 -- the guard against a false fix. A test asserting equality
  // here would be asserting the bug: one 42.9-device-px sprite measures
  // 10.7mm on the 102 PPI panel and 6.5mm on the 167 PPI one.
  assert.notEqual(
    hLo, hHi,
    'equal pixel height on panels of different density IS the bug. Equal '
    + 'physical size requires unequal pixel size, so a fix that equalises '
    + 'pixels has restored the defect while turning this suite green.',
  )
  assert.ok(
    hLo < hHi,
    `the low-density panel needs FEWER css px because each of its px is bigger: ${hLo} vs ${hHi}`,
  )

  // ASSERTION 2 -- the load-bearing one. Invariant D, "OS-intent parity":
  // device px proportional to the scale factor the user chose per panel,
  // which is what every other app on Windows does.
  const EXPECTED = lo.sf / hi.sf     // 1.10 / 1.65 = 0.6666666666666666
  const TOL = 0.01                   // 1% absolute on the ratio
  const ratio = hLo / hHi
  assert.ok(
    Math.abs(ratio - EXPECTED) <= TOL,
    `pet height ratio ${ratio.toFixed(4)} must be ${EXPECTED.toFixed(4)} (= sf 1.10 / sf 1.65); `
    + `the pre-fix engine gives 1.0000, a pet ${(1 / EXPECTED).toFixed(2)}x too big on the low-DPI monitor`,
  )

  // ASSERTION 3 -- the same claim in the unit a ruler measures, so the intent
  // survives a refactor of the arithmetic above.
  const inches = (cssPx, ppi) => cssPx * fx.windowScaleFactor / ppi
  const inLo = inches(hLo, REAL_PPI.secondary)
  const inHi = inches(hHi, REAL_PPI.primary)
  assert.ok(
    Math.abs(inLo - inHi) / inHi <= 0.10,
    `physical heights ${(inLo * 25.4).toFixed(2)}mm vs ${(inHi * 25.4).toFixed(2)}mm must agree `
    + `within 10%; pre-fix they are 10.68mm vs 6.53mm, a 63.7% error`,
  )
  assert.ok(
    Math.abs(inHi - 0.2569) < 0.002,
    `the primary pet must be ~6.53mm tall, got ${(inHi * 25.4).toFixed(2)}mm`,
  )

  // DOCUMENTED NON-GOAL. True physical parity wants the REAL PPI ratio
  // 102.30 / 167.76 = 0.6098, not the scaleFactor ratio 0.6667. The two
  // disagree by ~9% on this machine because Windows quantises a panel's scale
  // to a user-facing percentage that has nothing to do with its dot pitch.
  // The scaleFactor fallback deliberately does not close that 9%: scaleFactor
  // is a number Electron always hands over, real PPI needs EDID that is not
  // always readable, and a test demanding 0.6098 from the fallback would be
  // unsatisfiable. Recorded here so the residual is known, not discovered.
  const PPI_PARITY = REAL_PPI.secondary / REAL_PPI.primary   // 0.6098...
  assert.ok(
    Math.abs(PPI_PARITY - 0.610) < 0.002,
    `true-PPI parity is 0.610; this fixture says ${PPI_PARITY.toFixed(4)}`,
  )
  assert.ok(
    Math.abs(ratio - PPI_PARITY) > 0.03,
    'the scaleFactor fallback is NOT true-PPI parity, and this asserts the gap '
    + 'stays visible rather than being quietly papered over',
  )

  // ASSERTION 4 -- rescaled, not stretched: w and h share one factor k.
  assert.ok(
    Math.abs((wLo / wHi) - (hLo / hHi)) < 1e-9,
    `aspect must be preserved: w ${wLo}/${wHi} vs h ${hLo}/${hHi}`,
  )
})

test('the EDID path, when ppi is known, gives TRUE physical parity', () => {
  // The preferred branch: M = ppi / (96 * S), which lands both panels on the
  // same millimetre count exactly rather than within 9%.
  const { world, pet, fx } = mixedScene({ displays: MIXED_DPI_EDID })
  assert.equal(world.displays.length, 2, 'the EDID payload must install too')

  const hHi = sizeAt(pet, fx.midPrimary)
  const hLo = sizeAt(pet, fx.midSecondary)

  assert.notEqual(hLo, hHi, 'still unequal in pixels -- that is what parity requires')

  const mm = (cssPx, ppi) => cssPx * fx.windowScaleFactor / ppi * 25.4
  const mmLo = mm(hLo, REAL_PPI.secondary)
  const mmHi = mm(hHi, REAL_PPI.primary)
  assert.ok(
    Math.abs(mmLo - mmHi) < 0.5,
    `real PPI must give the same millimetres on both panels: ${mmLo.toFixed(2)}mm vs ${mmHi.toFixed(2)}mm`,
  )
})

// ── 8.3 the seam: one clean transition, no oscillation ───────────────────────

test('walking across the seam changes size once, monotonically, and settles', () => {
  const { pet, fx } = mixedScene({ x: MIXED_DPI.midSecondary, y: 700 })

  // Left to right: sparse panel -> dense panel, so the pet must GROW, and it
  // must never shrink on the way.
  const xs = [
    fx.midSecondary,
    fx.seamX - 200, fx.seamX - 50, fx.seamX - 1,
    fx.seamX + 1, fx.seamX + 50, fx.seamX + 200,
    fx.midPrimary,
  ]
  const hs = xs.map((x) => sizeAt(pet, x))

  for (let i = 1; i < hs.length; i++) {
    assert.ok(
      hs[i] >= hs[i - 1] - 1e-9,
      `size must never shrink while walking towards the denser panel: ${hs.join(', ')}`,
    )
  }
  assert.ok(hs[hs.length - 1] > hs[0], `the pet must end up bigger: ${hs[0]} -> ${hs[hs.length - 1]}`)

  // Exactly one transition. Two or more means the pet popped, popped back and
  // popped again as it crossed -- visible as a flicker.
  const changes = hs.filter((h, i) => i > 0 && Math.abs(h - hs[i - 1]) > 1e-9).length
  assert.equal(changes, 1, `exactly one size change across the seam, got ${changes}: ${hs.join(', ')}`)

  // And it stays settled: re-reading the same position must not move it.
  const settled = sizeAt(pet, fx.midPrimary)
  assert.equal(sizeAt(pet, fx.midPrimary), settled, 'a stationary pet must not resize')
  assert.equal(sizeAt(pet, fx.midPrimary), settled, 'still not, on the third read')
})

test('the seam has hysteresis: jiggling on the boundary never oscillates', () => {
  const { pet, fx } = mixedScene({ y: 700 })

  // Arrive from the dense side, so the pet is carrying the primary's size.
  sizeAt(pet, fx.midPrimary)
  const big = pet.h
  sizeAt(pet, fx.seamX + 300)
  assert.equal(pet.h, big)

  // Now walk back and forth across the raw boundary in small steps. Without a
  // deadband, `clampToWorld` writes x using the pet's own half-width, the new
  // x lands back over the previous screen, that re-selects the previous size,
  // which relaxes the clamp -- and the pet flickers between two sizes forever,
  // writing six style properties per frame while it does.
  //
  // The steps stay inside the deadband on purpose: with a 26px pet the pad is
  // 13, so the secondary is only adopted at x <= 1151.24, i.e. 12.4px left of
  // the raw seam. Stepping further than that is a real commitment to the other
  // screen, not a jiggle, and is checked separately below.
  const jiggle = [
    fx.seamX + 20, fx.seamX + 5, fx.seamX + 0.3, fx.seamX - 0.3,
    fx.seamX - 3, fx.seamX - 8, fx.seamX - 3, fx.seamX + 5,
    fx.seamX + 20, fx.seamX - 8, fx.seamX + 0.3, fx.seamX - 0.3,
  ]
  const hs = jiggle.map((x) => sizeAt(pet, x))
  const distinct = [...new Set(hs.map((h) => h.toFixed(6)))]
  assert.equal(
    distinct.length, 1,
    `a pet jiggling on the seam must hold ONE size; got ${distinct.length} distinct: ${distinct.join(', ')}`,
  )
  assert.equal(hs[0], big, 'and it keeps the size of the screen it actually came from')

  // Going properly onto the other screen still works -- a deadband that never
  // releases is just a different bug.
  const small = sizeAt(pet, fx.midSecondary)
  assert.ok(small < big, `committing to the sparse panel must still shrink the pet: ${small} vs ${big}`)
})

// ── 8.4 growing must not produce an inverted range ───────────────────────────
//
// `clamp()` returns the MIDPOINT when lo > hi. Every bound in engine.js has the
// form `x +/- w * k` or `top + h + k`, so GROWING a pet is precisely the
// operation that inverts them, and an inverted range never throws -- it
// teleports the pet to the middle of an impossible band, every frame. Two
// confirmed infinite loops came from this: a permanent hop loop, and a
// permanent turn-around loop.

test('however big a pet gets, its roam bounds stay ordered on BOTH screens', () => {
  for (const size of [26, 60, 120, 260, 520, 1200, 4000]) {
    const { world, pet, fx } = mixedScene({ size })
    for (const x of [fx.midSecondary, fx.seamX - 1, fx.seamX + 1, fx.midPrimary]) {
      sizeAt(pet, x)
      const b = pet.roamBounds()
      assert.ok(
        b.y1 <= b.y2,
        `size ${size} at x=${x.toFixed(1)}: roam band inverted, y1=${b.y1} > y2=${b.y2}. `
        + 'clamp() would silently park the pet at the midpoint of a band it cannot fit in, '
        + 'and settleToFloor would launch a hop every single frame, forever.',
      )
      assert.ok(b.x1 <= b.x2, `size ${size} at x=${x.toFixed(1)}: x band inverted, ${b.x1} > ${b.x2}`)
      assert.ok(
        pet.h <= world.h,
        `size ${size}: a pet ${pet.h} tall in a ${world.h} world cannot stand anywhere`,
      )
    }
  }
})

test('a pet that grows onto the dense screen settles instead of hopping forever', () => {
  const { pet, fx } = mixedScene({ x: MIXED_DPI.midSecondary, size: 120, y: 700 })

  const small = pet.h
  sizeAt(pet, fx.midPrimary)
  assert.ok(pet.h > small, `fixture sanity: the pet must actually grow, ${small} -> ${pet.h}`)

  // Hold it still and ask 400 times. A settleToFloor whose band inverted
  // launches a fresh hop on every call and never reaches its early return.
  let hops = 0
  let last = pet.state
  for (let i = 0; i < 400; i++) {
    pet.applyDisplaySize()
    if (pet.state === 'hop' && last !== 'hop') hops++
    last = pet.state
  }
  assert.ok(
    hops <= 1,
    `a stationary pet must not keep launching hops: ${hops} hop take-offs in 400 calls`,
  )
  assert.equal(pet.state === 'hop', false, 'and it must not be left mid-hop forever')
  assert.ok(Number.isFinite(pet.y), `y must stay a number, got ${pet.y}`)
})

test('1200 frames on a mixed-DPI desktop reach a stable size, with no loop', () => {
  const { world, pet } = mixedScene({ x: MIXED_DPI.midPrimary, size: 120, y: 700 })
  world.start()

  const hs = []
  const states = []
  const dirs = []
  for (let i = 0; i < 1200; i++) {
    tick(16)
    hs.push(pet.h)
    states.push(pet.state)
    dirs.push(pet.dir)
  }

  // Everything stayed a number and stayed on the board.
  assert.ok(Number.isFinite(pet.x) && Number.isFinite(pet.y) && Number.isFinite(pet.h))
  assert.ok(pet.x >= 0 && pet.x <= world.w, `x=${pet.x} escaped [0, ${world.w}]`)
  assert.ok(hs.every((h) => h >= 16 && h <= 512), 'every frame\'s height stayed inside the band')

  // No frame-to-frame size flicker. Two legitimate seam crossings are far
  // apart in time; an oscillation is adjacent frames.
  const changeFrames = []
  for (let i = 1; i < hs.length; i++) if (Math.abs(hs[i] - hs[i - 1]) > 1e-9) changeFrames.push(i)
  for (let i = 1; i < changeFrames.length; i++) {
    assert.ok(
      changeFrames[i] - changeFrames[i - 1] >= 3,
      `size changed on frames ${changeFrames[i - 1]} and ${changeFrames[i]} -- that is the `
      + 'seam oscillation the deadband exists to prevent',
    )
  }
  assert.ok(
    changeFrames.length <= 80,
    `${changeFrames.length} size changes in 1200 frames is a loop, not a walk`,
  )

  // Not a permanent hop loop.
  let hopTakeoffs = 0
  for (let i = 1; i < states.length; i++) {
    if (states[i] === 'hop' && states[i - 1] !== 'hop') hopTakeoffs++
  }
  assert.ok(hopTakeoffs <= 120, `${hopTakeoffs} hop take-offs in 1200 frames is the hop loop`)

  // Not a permanent turn-around loop: the walk clamp on a span narrower than
  // 0.8 * w used to flip `dir` every single frame.
  let flips = 0
  for (let i = 1; i < dirs.length; i++) if (dirs[i] !== dirs[i - 1]) flips++
  assert.ok(flips <= 300, `${flips} direction flips in 1200 frames is the turn-around loop`)

  world.stop()
})

// ── 8.5 the state gate ───────────────────────────────────────────────────────

test('a held, airborne or hopping pet refuses to be resized', () => {
  for (const state of ['held', 'air', 'hop']) {
    const { pet, fx } = mixedScene({ x: MIXED_DPI.midSecondary, y: 700 })
    const before = sizeAt(pet, fx.midSecondary)

    // A drag is anchored to offsets captured at pointerdown; a hop and a fall
    // are arcs frozen at take-off. Resizing under any of the three moves the
    // pet out from under whatever is steering it.
    pet.state = state
    pet.x = fx.midPrimary
    pet.applyDisplaySize()

    assert.equal(
      pet.h, before,
      `state '${state}' owns the pet's geometry and must refuse a resize (was ${before}, got ${pet.h})`,
    )
  }
})

test('the same pet, back in a normal state, does accept the resize', () => {
  const { pet, fx } = mixedScene({ x: MIXED_DPI.midSecondary, y: 700 })
  const before = sizeAt(pet, fx.midSecondary)

  pet.state = 'held'
  pet.x = fx.midPrimary
  pet.applyDisplaySize()
  assert.equal(pet.h, before, 'refused while held')

  // Not a permanent refusal -- the gate is on the state, not on the pet.
  pet.state = 'idle'
  pet.applyDisplaySize()
  assert.ok(
    pet.h > before,
    `once the state releases, the pending resize must happen: ${before} -> ${pet.h}`,
  )
})

// ── 8.6 the legacy payload must still install ────────────────────────────────

test('a displays payload with no sf and no ppi still installs, with M = 1.0', () => {
  // A predicate tightened to require the new fields turns an old payload into
  // a SILENT no-op install -- pets keep the world's last known geometry and
  // nobody sees an error. That is strictly worse than rejecting it loudly, so
  // the filter must stay exactly as permissive as it was.
  const { world, pet, fx } = mixedScene({
    displays: MIXED_DPI_LEGACY,
    // 0, not undefined: the default parameter would put 1.65 back. A legacy
    // host never sent a window scale at all, so the engine must infer one.
    windowScale: 0,
  })

  assert.equal(
    world.displays.length, 2,
    'a payload predating sf/ppi must still install both screens, not be dropped',
  )
  assert.ok(
    MIXED_DPI_LEGACY.every((d) => d.sf === undefined && d.ppi === undefined),
    'fixture sanity: the legacy payload really carries neither field',
  )

  // It installed for real, not as a no-op: the per-screen floors are live.
  assert.ok(Math.abs(world.displays[0].floor - 810.3030) < 0.001)
  assert.ok(Math.abs(world.displays[1].floor - 927.2727) < 0.001)
  pet.x = fx.midSecondary; pet.y = 9999; pet.clampToWorld()
  assert.ok(Math.abs(pet.y - 810.3030) < 0.5, `legacy payload still floors the pet: ${pet.y}`)

  // And with nothing to compute M from, M is exactly 1 on both screens, which
  // is the old behaviour -- same size everywhere, imperfect but shipped.
  const hHi = sizeAt(pet, fx.midPrimary)
  const hLo = sizeAt(pet, fx.midSecondary)
  assert.equal(
    hLo, hHi,
    `without sf or ppi there is no density to correct for, so both screens must give the `
    + `same size (the pre-fix behaviour): ${hLo} vs ${hHi}`,
  )
  assert.equal(hHi, 26, 'and that size is the plain, uncorrected one')
  assert.equal(pet.dispM, 1, 'M must default to exactly 1.0, never 0 and never NaN')
})

test('a single legacy display -- the shape every older caller sends -- still works', () => {
  // The two pre-existing display tests above send exactly this shape. If the
  // filter were tightened they would be the first casualties.
  const world = new World({
    stage: makeEl(), assetBase: '/assets/pets/', opts: { size: 26 }, scale: 1,
  })
  world.setSize(400, 400)
  world.setDisplays([{ id: 1, x: 0, y: 0, width: 400, height: 400, floor: 360 }])
  assert.equal(world.displays.length, 1, 'the six-field payload must survive the filter')

  const pet = world.add(SPEC('p1', { mode: 'free', platformId: null, x: 200, y: 395 }))
  pet.clampToWorld()
  assert.equal(pet.y, 360, 'and behave exactly as it did before sf/ppi existed')
  assert.equal(pet.h, 26, 'with no density correction applied')
})

// ── 8.7 the final height is clamped, always ──────────────────────────────────

test('the per-display factor can never make a pet invisible or screen-filling', () => {
  const fx = MIXED_DPI

  // Bottom end: a tiny world size, a tiny hand-zoom and the sparse panel's
  // shrinking factor all multiplied together.
  const tiny = mixedScene({ x: fx.midSecondary, y: 700, size: 1, scale: 0.35 })
  for (const x of [fx.midSecondary, fx.midPrimary]) {
    sizeAt(tiny.pet, x)
    assert.ok(tiny.pet.h >= 16, `a pet below 16 css px is unclickable, got ${tiny.pet.h} at x=${x.toFixed(0)}`)
    assert.ok(parseFloat(tiny.pet.el.style.width) >= 1, `canvas width ${tiny.pet.el.style.width}`)
    assert.ok(parseFloat(tiny.pet.el.style.height) >= 1, `canvas height ${tiny.pet.el.style.height}`)
  }

  // Top end: an absurd size on the dense panel.
  const huge = mixedScene({ x: fx.midPrimary, y: 700, size: 4000 })
  for (const x of [fx.midSecondary, fx.midPrimary]) {
    sizeAt(huge.pet, x)
    assert.ok(huge.pet.h <= 512, `a pet above 512 css px covers the screen, got ${huge.pet.h}`)
    assert.ok(
      huge.pet.h < huge.world.h,
      `and must still fit in the ${huge.world.h} world, got ${huge.pet.h}`,
    )
  }

  // The band is [16, 512] and both ends are reachable, so neither is dead code.
  assert.equal(tiny.pet.h, 16, 'the lower clamp is the one that fired')
  assert.equal(huge.pet.h, 512, 'the upper clamp is the one that fired')
})

// ── 8.8 `M` — the density factor, branch by branch ───────────────────────────
//
// `displayScale()` is the arithmetic the whole second defect turns on, and
// until now the only thing asserted about it was the one ratio a pet standing
// on each of two screens happens to produce. That covers exactly one branch of
// five. The rest -- which input wins, what a garbage input does, and where the
// clamp band sits -- was reachable only by reading the source.
//
// These call `world.displayScale(d)` on hand-made display objects rather than
// standing a pet on them, because a pet's height also carries `petHeight()`,
// `species.scale`, `sizeFactor`, `K` and the [16, 512] clamp. Reading `M` at
// the source is the difference between "the answer is wrong" and "the answer
// is wrong HERE".

test('M prefers real PPI over the scaleFactor approximation when both are present', () => {
  const { world } = mixedScene()

  // Both fields readable, and they disagree: 102.30 / (96 * 1.65) = 0.6458 from
  // EDID, 1.10 / 1.65 = 0.6667 from the OS scale the user picked. EDID is the
  // measurement and must win; the scaleFactor branch is the fallback for the
  // machines where physical size is not readable at all.
  const both = { ppi: REAL_PPI.secondary, sf: 1.10 }
  assert.ok(
    Math.abs(world.displayScale(both) - 0.645833) < 1e-5,
    `ppi must take precedence: expected 0.645833 (ppi/(96*S)), got ${world.displayScale(both)}`,
  )
  assert.notEqual(
    world.displayScale(both), world.displayScale({ sf: 1.10 }),
    'if the two branches agreed, this test could not tell which one ran',
  )
})

test('M falls back to scaleFactor for every unusable ppi, and to 1 when both are unusable', () => {
  const { world } = mixedScene()
  const SF_ANSWER = 1.10 / WINDOW_SF          // 0.6666666666666667

  // Every shape an absent or broken EDID read actually arrives in. `0` is the
  // documented "unknown" sentinel; the rest are what a partially-populated
  // payload looks like when something upstream failed.
  for (const ppi of [0, -5, NaN, Infinity, -Infinity, undefined, null, 'nonsense', {}]) {
    assert.ok(
      Math.abs(world.displayScale({ ppi, sf: 1.10 }) - SF_ANSWER) < 1e-12,
      `ppi=${String(ppi)} must fall through to the sf branch, got ${world.displayScale({ ppi, sf: 1.10 })}`,
    )
  }

  // Numeric strings are the one non-number that IS usable: `Number(d.ppi)`
  // accepts them, and a payload that has been through JSON on some path can
  // deliver one. Pinned so a future `typeof d.ppi === 'number'` tightening is
  // a deliberate decision rather than an accident.
  assert.ok(
    Math.abs(world.displayScale({ ppi: '102.30', sf: 1.10 }) - 0.645833) < 1e-5,
    'a numeric string ppi is coerced, not rejected',
  )

  // Nothing usable at all: exactly 1, which is the pre-fix behaviour -- the
  // same size everywhere. Never 0, never NaN, both of which would propagate
  // into `k` and collapse or destroy the sprite.
  for (const d of [{}, { ppi: 0, sf: 0 }, { ppi: NaN, sf: NaN }, { sf: -1 }, null, undefined]) {
    assert.equal(
      world.displayScale(d), 1,
      `an unusable display must give M = 1 exactly, got ${world.displayScale(d)} for ${JSON.stringify(d)}`,
    )
  }
})

test('M is clamped to [0.25, 4] from both directions, and both ends are reachable', () => {
  const { world } = mixedScene()

  // A display claiming an absurd density -- a bad EDID block, a virtual
  // display, a remote-desktop adapter -- must not be able to multiply a pet's
  // height without limit. Unclamped, sf=100 against S=1.65 is 60x.
  assert.equal(world.displayScale({ sf: 100 }), 4, 'the upper clamp')
  assert.equal(world.displayScale({ ppi: 100000 }), 4, 'the upper clamp, ppi branch')
  assert.equal(world.displayScale({ sf: 0.001 }), 0.25, 'the lower clamp')
  assert.equal(world.displayScale({ ppi: 0.001 }), 0.25, 'the lower clamp, ppi branch')

  // Just inside each end, so the band is a band and not a pair of constants.
  assert.ok(Math.abs(world.displayScale({ sf: 3.9 * WINDOW_SF }) - 3.9) < 1e-9)
  assert.ok(Math.abs(world.displayScale({ sf: 0.3 * WINDOW_SF }) - 0.3) < 1e-9)
})

test('a monitor with an absurd scale factor cannot drive a pet out of [16, 512]', () => {
  // The clamp above is on M alone. This is the same claim at the end of the
  // chain, where it is actually load-bearing: M is multiplied by
  // `petHeight() * species.scale * sizeFactor` before the PET_MIN_H/PET_MAX_H
  // clamp sees it, so an M inside its own band can still land the pet outside
  // the visible band.
  const wild = MIXED_DPI.displays.map((d, i) => ({ ...d, sf: i === 0 ? 0.05 : WINDOW_SF }))
  const lo = mixedScene({ displays: wild, x: MIXED_DPI.midSecondary })
  assert.equal(lo.world.displayScale(lo.world.displays[0]), 0.25, 'M bottomed out')
  assert.equal(
    sizeAt(lo.pet, MIXED_DPI.midSecondary), 16,
    '26 * 0.25 = 6.5 css px is an unclickable smear; PET_MIN_H must catch it',
  )

  const wilder = MIXED_DPI.displays.map((d, i) => ({ ...d, sf: i === 0 ? 100 : WINDOW_SF }))
  const hi = mixedScene({ displays: wilder, x: MIXED_DPI.midSecondary, size: 200 })
  assert.equal(hi.world.displayScale(hi.world.displays[0]), 4, 'M topped out')
  assert.equal(
    sizeAt(hi.pet, MIXED_DPI.midSecondary), 512,
    '200 * 4 = 800 css px would cover most of a monitor; PET_MAX_H must catch it',
  )
})

test('one panel with EDID beside one without is the common case, and both are honoured', () => {
  // The realistic mixed payload: the laptop panel reports physical size, the
  // external over a dock does not. Neither branch may be all-or-nothing --
  // a per-display choice, not a per-payload one.
  const half = MIXED_DPI.displays.map((d, i) => (
    i === 0 ? { ...d, ppi: REAL_PPI.secondary } : { ...d, ppi: 0 }
  ))
  const { world, pet, fx } = mixedScene({ displays: half })

  assert.ok(
    Math.abs(world.displayScale(world.displays[0]) - 0.645833) < 1e-5,
    'the EDID panel takes the ppi branch',
  )
  assert.equal(world.displayScale(world.displays[1]), 1, 'the other takes the sf branch')

  const hLo = sizeAt(pet, fx.midSecondary)
  const hHi = sizeAt(pet, fx.midPrimary)
  assert.ok(Math.abs(hLo - 16.7917) < 0.001, `sparse panel, ppi branch: ${hLo}`)
  assert.equal(hHi, 26, 'dense panel, sf branch, M = 1 because it IS the window\'s screen')
  assert.ok(hLo < hHi, 'and the relationship between the two panels is unchanged')
})

// ── 8.9 `K` — the per-display taste multiplier ───────────────────────────────
//
// `displayTaste()` had no test at all. It is user-authored (the store persists
// a map of it per display key), which makes it the one input here that is
// hostile by construction: whatever a slider, a hand-edited settings file or a
// corrupt blob can contain, it can contain.

test('K sanitises every hostile input to a usable multiplier', () => {
  const { world } = mixedScene()

  // Absent is 1 -- the overwhelmingly common case, since a display the user has
  // never touched has no entry.
  assert.equal(world.displayTaste({}), 1, 'no k at all')
  assert.equal(world.displayTaste(null), 1, 'no display at all')

  for (const k of [0, -2, NaN, Infinity, -Infinity, undefined, null, 'nonsense', {}, []]) {
    assert.equal(
      world.displayTaste({ k }), 1,
      `k=${String(k)} must sanitise to exactly 1, got ${world.displayTaste({ k })}`,
    )
  }

  // Sane values pass through untouched, and the band is [0.5, 2].
  assert.equal(world.displayTaste({ k: 1.5 }), 1.5, 'a legitimate value is not mangled')
  assert.equal(world.displayTaste({ k: '1.5' }), 1.5, 'a numeric string is coerced, like ppi/sf')
  assert.equal(world.displayTaste({ k: 99 }), 2, 'the upper clamp')
  assert.equal(world.displayTaste({ k: 0.01 }), 0.5, 'the lower clamp')
  assert.equal(world.displayTaste({ k: 2 }), 2, 'the band is inclusive at the top')
  assert.equal(world.displayTaste({ k: 0.5 }), 0.5, 'and at the bottom')
})

test('K multiplies the pet without contaminating M, and is per-display', () => {
  // The engine keeps M and K as two stored numbers on purpose -- one is a
  // measurement, one is a preference -- and folding them into a product would
  // make a taste setting indistinguishable from a density bug the next time
  // either has to be debugged. This asserts the separation survives.
  const tasted = MIXED_DPI.displays.map((d, i) => (i === 0 ? { ...d, k: 2 } : { ...d }))
  const { pet, fx } = mixedScene({ displays: tasted })

  const hHi = sizeAt(pet, fx.midPrimary)
  assert.equal(hHi, 26, 'the untasted screen is unaffected')
  assert.equal(pet.dispK, 1, 'and reports K = 1')
  assert.equal(pet.dispM, 1, 'with M = 1, being the screen the window was scaled for')

  const hLo = sizeAt(pet, fx.midSecondary)
  assert.ok(
    Math.abs(pet.dispM - (1.10 / WINDOW_SF)) < 1e-12,
    `M must still be the pure density term ${1.10 / WINDOW_SF}, got ${pet.dispM}`,
  )
  assert.equal(pet.dispK, 2, 'K is stored separately, not folded into M')
  assert.ok(
    Math.abs(hLo - 26 * (1.10 / WINDOW_SF) * 2) < 1e-9,
    `the height is the product of both: expected ${26 * (1.10 / WINDOW_SF) * 2}, got ${hLo}`,
  )

  // The striking part, and the reason a user reaches for this at all: taste
  // can INVERT the density relationship. The sparse panel is now the one with
  // the bigger pet, which a test asserting "low density means smaller" would
  // wrongly call a regression.
  assert.ok(hLo > hHi, `K=2 overrides the density shrink: ${hLo} vs ${hHi}`)
})

test('a K outside the band cannot make a pet vanish or fill the screen', () => {
  const crushed = MIXED_DPI.displays.map((d) => ({ ...d, k: 0.0001 }))
  const a = mixedScene({ displays: crushed, x: MIXED_DPI.midPrimary })
  assert.equal(a.world.displayTaste(a.world.displays[1]), 0.5, 'K clamped up to 0.5')
  assert.equal(sizeAt(a.pet, MIXED_DPI.midPrimary), 16, 'and PET_MIN_H catches the rest')

  const bloated = MIXED_DPI.displays.map((d) => ({ ...d, k: 1e9 }))
  const b = mixedScene({ displays: bloated, x: MIXED_DPI.midPrimary, size: 400 })
  assert.equal(b.world.displayTaste(b.world.displays[1]), 2, 'K clamped down to 2')
  assert.equal(sizeAt(b.pet, MIXED_DPI.midPrimary), 512, 'and PET_MAX_H catches the rest')
})

// ── 8.10 `S` — the window scale, told and inferred ───────────────────────────
//
// `M` is a RATIO, and `S` is its denominator. Every assertion in 8.2 holds `S`
// fixed at the value the fixture hands over, so the entire inference path --
// the one that runs whenever the host has not said, which includes every
// legacy caller -- was unexercised.

test('S is inferred from the densest panel when the host never says', () => {
  const { world, pet, fx } = mixedScene({ windowScale: 0 })

  // Windows gives a window the scale factor of whichever monitor it covers
  // most in device px; for an overlay spanning the desktop that is the densest
  // panel it meaningfully overlaps. The largest `sf` in the map is therefore
  // the right guess, and here it recovers 1.65 exactly.
  assert.equal(world.windowScaleOf(), WINDOW_SF, 'the largest sf in the map is the inference')

  // And the inference is good enough that the headline ratio still holds --
  // which is the whole point of having one.
  const hLo = sizeAt(pet, fx.midSecondary)
  const hHi = sizeAt(pet, fx.midPrimary)
  assert.ok(
    Math.abs((hLo / hHi) - (1.10 / WINDOW_SF)) < 0.01,
    `an inferred S must still give the sf ratio: ${hLo / hHi}`,
  )
})

test('setWindowScale re-sizes every live pet, and a bad value means "not told"', () => {
  const { world, pet, fx } = mixedScene()
  const before = sizeAt(pet, fx.midSecondary)
  assert.ok(Math.abs(before - 17.3333) < 0.001, `fixture sanity: ${before}`)

  // The host reporting a NEW scale -- which is what happens when the overlay
  // window is moved onto a different monitor and Windows re-scales it -- must
  // move every pet immediately. Note there is no applyDisplaySize() call here:
  // setWindowScale is required to do it itself, because a stationary pet's
  // position has not changed and nothing else would notice.
  world.setWindowScale(1.10)
  assert.equal(world.windowScaleOf(), 1.10, 'the told value wins over the inference')
  assert.equal(
    pet.h, 26,
    'with S = 1.10 the sparse panel IS the window\'s screen, so M = 1 and the pet '
    + `is drawn plain; got ${pet.h}`,
  )

  // Same pet, same panel, different S: the dense screen is now the one being
  // corrected upward. 1.65 / 1.10 = 1.5, so 26 -> 39.
  assert.ok(Math.abs(sizeAt(pet, fx.midPrimary) - 39) < 1e-9, `dense panel under S=1.10: ${pet.h}`)

  // Anything unusable does NOT keep the last good value: it returns the world
  // to "the host has not said", which re-enables the inference. Silently
  // keeping 1.10 would leave the window wearing a scale nobody claimed.
  for (const bad of [-1, 0, NaN, 'x', undefined, null]) {
    world.setWindowScale(1.10)
    world.setWindowScale(bad)
    assert.equal(
      world.windowScaleOf(), WINDOW_SF,
      `setWindowScale(${String(bad)}) must fall back to the inference, not keep 1.10`,
    )
  }
})

test('setDisplays only overwrites S when it is given a usable one', () => {
  const { world } = mixedScene()
  assert.equal(world.windowScaleOf(), WINDOW_SF)

  // A host that re-sends its display map without a scale -- every pre-`sf`
  // caller -- must not be able to wipe a scale a newer call already
  // established.
  for (const bad of [0, -1, NaN, undefined, null, 'x']) {
    world.setDisplays(MIXED_DPI.displays.map((d) => ({ ...d })), bad)
    assert.equal(
      world.windowScaleOf(), WINDOW_SF,
      `setDisplays(list, ${String(bad)}) must leave S alone`,
    )
  }

  world.setDisplays(MIXED_DPI.displays.map((d) => ({ ...d })), 1.25)
  assert.equal(world.windowScaleOf(), 1.25, 'but a usable one does land')
})

// ── 8.11 the dead rectangle beside a short monitor ───────────────────────────
//
// `displayAt()` resolves a point by COLUMN first and only falls back to
// nearest-rectangle, and the comment above it is emphatic about why. Nothing
// tested it: the mixed-DPI fixture's two screens both reach the bottom of the
// world, so every point in it is inside some display and the column branch and
// the nearest branch agree everywhere.
//
// This fixture is deliberately staggered so they DISAGREE. A short monitor
// beside a tall one leaves a rectangle that is part of the desktop's extent
// and part of no display at all -- and it is precisely where a pet walking out
// of the tall screen at its own floor height arrives.

/** Tall 1000px-high screen on the left, short 260px-high screen on the right. */
const STAGGERED = [
  { id: 10, x: 0, y: 0, width: 1200, height: 1000, floor: 960, sf: 1.0, ppi: 0 },
  { id: 20, x: 1200, y: 0, width: 800, height: 260, floor: 240, sf: 2.0, ppi: 0 },
]

const staggeredScene = ({ size = 60, x = 600, y = 900 } = {}) => {
  const world = new World({
    stage: makeEl(), assetBase: '/assets/pets/', opts: { size }, scale: 1,
  })
  world.setSize(2000, 1000)
  world.setDisplays(STAGGERED.map((d) => ({ ...d })), 2.0)
  world.setPlatforms([])
  const pet = world.add(SPEC('p1', { mode: 'free', platformId: null, x, y }))
  return { world, pet }
}

test('a point in the dead rectangle belongs to the column, not to the nearest rectangle', () => {
  const { world } = staggeredScene()

  // (1260, 950): 60px right of the seam, 690px below the short screen's bottom
  // edge. Inside no display.
  const probe = { x: 1260, y: 950 }
  assert.ok(
    !STAGGERED.some((d) => probe.x >= d.x && probe.x < d.x + d.width
      && probe.y >= d.y && probe.y < d.y + d.height),
    'fixture sanity: the probe point really is inside no display',
  )

  // Worked by hand, and the whole reason the column rule exists: the tall
  // screen is 60px away, the short screen 690px. Nearest-rectangle matching
  // says TALL and is wrong -- a pet 60px past the seam has walked onto the
  // short monitor and should be standing on ITS floor, not hanging in the air
  // over a screen it has left.
  const dTall = (1260 - 1200) ** 2                 //   3600
  const dShort = (950 - 260) ** 2                  // 476100
  assert.ok(dTall < dShort, `fixture sanity: nearest-rect prefers the tall screen (${dTall} < ${dShort})`)

  assert.equal(
    world.displayAt(probe.x, probe.y).id, 20,
    'the column rule must win: x decides which monitor a walking pet is on, and '
    + 'nearest-rect answers that question the wrong way round and strands the pet in the gap',
  )
  assert.deepEqual(
    world.screenSpan(probe.x, probe.y), { top: 0, floor: 240 },
    'so the ground under that point is the SHORT screen\'s floor',
  )

  // The column rule is not "always the right-hand screen": on the tall side it
  // still answers tall, at the same y.
  assert.equal(world.displayAt(600, 950).id, 10, 'and the tall column is still the tall screen')
})

test('a pet stepping off a tall screen into the dead rectangle is not lost in it', () => {
  const { world, pet } = staggeredScene()

  assert.equal(pet.display.id, 10, 'starts on the tall screen')
  assert.equal(sizeAt(pet, 600), 30, 'at half size: sf 1.0 against S 2.0')
  assert.equal(pet.y, 900, 'standing well down the tall screen')

  // One step across the seam, onto a monitor that is BOTH denser and 740px
  // shorter. Both things change at once, which is the case a single-display
  // fixture cannot express at all.
  pet.x = 1600
  pet.applyDisplaySize()

  assert.equal(pet.display.id, 20, 'it adopts the short screen')
  assert.equal(pet.h, 60, 'and doubles, sf 2.0 against S 2.0')
  assert.equal(
    pet.state, 'hop',
    'the ground moved 720px up under a pet that did not move vertically, so it hops '
    + '-- teleporting is not something a pet does',
  )
  assert.ok(
    pet.y <= 240 + 1e-9,
    `and it must be heading for the SHORT screen's floor 240, not left at 900 in the `
    + `dead rectangle where nothing is drawn; got y=${pet.y}`,
  )

  // Land it with real frames and check it stayed landed.
  settle(world, pet)
  pet.clampToWorld()
  // 234, not 240: `settleToFloor` aims at `roamBounds().y2`, which is the floor
  // less a 6px standing margin. The floor itself is the hard `clampToWorld`
  // bound; the roam band is where a pet chooses to be, and the two are 6px
  // apart by design. Asserting the band's own number rather than the floor is
  // what keeps this test from drifting into re-implementing `roamBounds()`.
  const b = pet.roamBounds()
  assert.ok(
    Math.abs(pet.y - b.y2) < 1e-9,
    `after landing it rests on the short screen's roam floor ${b.y2} (floor 240 less `
    + `the 6px margin), got ${pet.y}`,
  )
  assert.ok(pet.y <= 240 + 1e-9, `and never below the hard floor 240: ${pet.y}`)
  assert.ok(b.y1 <= b.y2, `and its roam band on the short screen is ordered: ${b.y1} > ${b.y2}`)
  assert.ok(pet.y >= b.y1 - 1e-9 && pet.y <= b.y2 + 1e-9, `standing inside it: ${pet.y}`)
})

test('a pet that starts life in the dead rectangle is pulled out of it', () => {
  // The other way in: a saved position from a session when the second monitor
  // was taller, restored against today's layout.
  const { pet } = staggeredScene({ x: 1600, y: 900 })

  assert.equal(pet.display.id, 20, 'the column rule places it on the short screen')
  pet.clampToWorld()
  assert.ok(
    Math.abs(pet.y - 240) < 1e-9,
    `clampToWorld must lift it onto the short screen's floor 240, not leave it at 900 `
    + `over a world that is 1000 tall but blank below 260 there; got ${pet.y}`,
  )
})

// ── 8.12 a monitor unplugged ─────────────────────────────────────────────────

test('unplugging a monitor drops the stale cached screen instead of sizing from it', () => {
  const { world, pet, fx } = mixedScene({ x: MIXED_DPI.midPrimary })

  const hHi = sizeAt(pet, fx.midPrimary)
  const gone = pet.display
  assert.equal(gone.id, 3828506604, 'sized against the dense screen')
  assert.equal(hHi, 26)

  // The dense monitor is unplugged; the sparse one is now the whole desktop.
  // The pet has not moved, so nothing about its POSITION signals a change --
  // if the cached `display` survived, it would keep drawing itself at the
  // density of a panel that is no longer attached, forever.
  world.setDisplays(
    [{ ...MIXED_DPI.displays[0], width: MIXED_DPI.worldWidth }],
    WINDOW_SF,
  )

  assert.equal(world.displays.length, 1, 'one screen left')
  assert.ok(
    !world.displays.some((d) => d.id === gone.id),
    'fixture sanity: the unplugged screen really is out of the map',
  )
  assert.equal(
    pet.display.id, 2121993299,
    'the pet must re-resolve onto a screen that still exists',
  )
  assert.notEqual(pet.display, gone, 'and hold the live entry, not the detached object')
  assert.ok(
    Math.abs(pet.h - 17.3333) < 0.001,
    `and be re-sized for it: expected 17.33 css px, got ${pet.h}`,
  )
  // It stays at 700 -- it never needed to move. 700 was chosen for `mixedScene`
  // precisely because it is inside the roam band of BOTH screens, so the pet
  // that loses a monitor out from under it is still standing somewhere legal
  // on the one that remains. A re-clamp that moved it anyway would be a
  // visible jump for no reason.
  assert.equal(pet.y, 700, `an already-legal position must not be disturbed, got ${pet.y}`)
  const b = pet.roamBounds()
  assert.ok(
    pet.y >= b.y1 - 1e-9 && pet.y <= b.y2 + 1e-9,
    `and it is inside the surviving screen's band [${b.y1}, ${b.y2}]: ${pet.y}`,
  )
  assert.ok(pet.y <= 810.3030 + 1e-9, 'never below the surviving screen\'s floor')
})

test('with every monitor gone the pet keeps its last size rather than collapsing', () => {
  const { world, pet, fx } = mixedScene({ x: MIXED_DPI.midPrimary })
  const hHi = sizeAt(pet, fx.midPrimary)

  // The transient every multi-monitor machine goes through: the map is
  // replaced with an empty one for a frame or two during a display change.
  // `displayAt` returns null and `governingDisplay` has nothing to answer
  // with. The pet must hold still, not fall back to some default that makes
  // it pop to a different size and pop back.
  world.setDisplays([], WINDOW_SF)

  assert.equal(world.displays.length, 0)
  assert.equal(pet.display, null, 'the cache is cleared with the map')
  assert.equal(pet.h, hHi, 'but the drawn size is untouched')

  pet.x = fx.midSecondary
  pet.applyDisplaySize()
  assert.equal(
    pet.h, hHi,
    'and a move while blind changes nothing either -- with no display map there is '
    + 'no density to read, and guessing would be a visible pop for no information',
  )
  assert.ok(Number.isFinite(pet.y) && Number.isFinite(pet.x), 'position stays a number')
})

// ── 8.13 a world resize can cross a seam without the pet moving ──────────────

test('setSize carrying a pet across a seam re-densities it', () => {
  const { world, pet, fx } = mixedScene({ x: MIXED_DPI.midPrimary })

  assert.equal(sizeAt(pet, fx.midPrimary), 26, 'on the dense screen')
  assert.equal(pet.display.id, 3828506604)

  // `setSize` moves every pet by a FRACTION of the new width, so the pet ends
  // up somewhere it never walked to. Nothing else in the engine would notice:
  // this is not a crossing, not a display change and not a size change, and
  // without the explicit re-density in `setSize` the pet keeps the dense
  // screen's factor while standing over the sparse one indefinitely.
  world.setSize(MIXED_DPI.worldWidth * 0.3, MIXED_DPI.worldHeight)

  assert.ok(
    Math.abs(pet.x - 581.909) < 0.01,
    `fixture sanity: 0.7143 of the old width becomes ${pet.x} of the new`,
  )
  assert.ok(pet.x < fx.seamX, 'which is over the SECONDARY, without the pet having walked')
  assert.equal(
    pet.display.id, 2121993299,
    'setSize must re-resolve the screen it landed the pet on',
  )
  assert.ok(
    Math.abs(pet.h - 17.3333) < 0.001,
    `and re-size for it: expected 17.33, got ${pet.h} (26 means it kept the dense `
    + 'screen\'s density while standing on the sparse one)',
  )
})

// ── 8.14 the destination screen's floor, after the size changed ──────────────
//
// `docs/multi-monitor-sizing.md` §5 names this as a required supporting
// assertion and nothing implemented it: "a pet whose height changed at the
// seam must rest on the floor of the screen it arrived on, not the one it
// left." Both directions matter and they are not symmetric -- one is a step
// up onto higher ground, the other is a step out over lower ground.

test('crossing onto a screen with a HIGHER floor lands the pet on that floor', () => {
  const { world, pet, fx } = mixedScene({ x: MIXED_DPI.midPrimary })
  const [lo, hi] = world.displays

  // Standing on the primary's floor, which is 117px BELOW the secondary's.
  sizeAt(pet, fx.midPrimary)
  pet.y = hi.floor
  assert.ok(Math.abs(pet.y - 927.2727) < 0.001, 'starts on the primary floor')

  pet.x = fx.midSecondary
  pet.applyDisplaySize()

  assert.equal(pet.state, 'hop', 'stepping up onto higher ground is a hop, not a teleport')
  assert.ok(
    pet.y <= lo.floor + 1e-9,
    `it must be aiming at the secondary's floor ${lo.floor}, not still at the primary's `
    + `${hi.floor} -- which on the secondary is 117px below the glass; got ${pet.y}`,
  )

  settle(world, pet)
  pet.clampToWorld()
  // `lo.floor - 6`: the roam band's own bottom, which is where `settleToFloor`
  // aims. The floor itself is the hard clamp. See the note in 8.11.
  assert.ok(
    Math.abs(pet.y - (lo.floor - 6)) < 1e-9,
    `and it comes to rest on the arrival screen's roam floor ${lo.floor - 6}, got ${pet.y}`,
  )
  assert.ok(
    pet.y <= lo.floor + 1e-9,
    `never below the arrival screen's hard floor ${lo.floor}, and specifically not still `
    + `on the primary's ${hi.floor}; got ${pet.y}`,
  )
  assert.ok(Math.abs(pet.h - 17.3333) < 0.001, `wearing the arrival screen's size: ${pet.h}`)
})

test('crossing onto a screen with a LOWER floor does not drop the pet through the old one', () => {
  const { world, pet, fx } = mixedScene({ x: MIXED_DPI.midSecondary, size: 60 })
  const [lo, hi] = world.displays

  sizeAt(pet, fx.midSecondary)
  pet.y = lo.floor
  assert.ok(Math.abs(pet.y - 810.3030) < 0.001, 'starts on the secondary floor')

  // The primary's floor is LOWER, so the pet arrives standing in mid-air over
  // it -- legal, since 810 is inside the primary's roam band, and it should
  // simply keep standing there rather than snapping down. What must not
  // happen is a resize that leaves it below the new floor.
  pet.x = fx.midPrimary
  pet.applyDisplaySize()
  settle(world, pet)

  assert.equal(pet.h, 60, 'it grew for the dense screen')
  assert.ok(
    pet.y <= hi.floor + 1e-9,
    `and stayed at or above the arrival floor ${hi.floor}, got ${pet.y}`,
  )
  const b = pet.roamBounds()
  assert.ok(b.y1 <= b.y2, `roam band ordered on arrival: ${b.y1} > ${b.y2}`)
  assert.ok(
    pet.y >= b.y1 - 1e-9 && pet.y <= b.y2 + 1e-9,
    `and it is standing somewhere it is actually allowed to be: ${pet.y} in [${b.y1}, ${b.y2}]`,
  )
})

// ── 8.15 the deadband's own escape hatch ─────────────────────────────────────
//
// The hysteresis test in 8.3 proves the deadband HOLDS. Its failure mode in
// the other direction -- a deadband that never releases -- has one specific
// trigger the engine carries an explicit branch for: a screen narrower than
// twice the pad can never satisfy "centre properly inside it", so without that
// branch a pet would wear the wrong size on that monitor forever.

test('a screen narrower than the deadband is still adoptable', () => {
  const world = new World({
    stage: makeEl(), assetBase: '/assets/pets/', opts: { size: 60 }, scale: 1,
  })
  world.setSize(1020, 800)
  world.setDisplays([
    { id: 1, x: 0, y: 0, width: 1000, height: 800, floor: 700, sf: 1.0, ppi: 0 },
    { id: 2, x: 1000, y: 0, width: 20, height: 800, floor: 700, sf: 2.0, ppi: 0 },
  ], 2.0)
  world.setPlatforms([])
  const pet = world.add(SPEC('p1', { mode: 'free', platformId: null, x: 500, y: 500 }))

  const wide = sizeAt(pet, 500)
  assert.equal(wide, 30, 'half size on the sf 1.0 screen under S 2.0')

  // The arithmetic that makes this a special case. pad = max(8, w/2) = 15, and
  // the ordinary rule wants x within [1000 + 15, 1020 - 15] = [1015, 1005] --
  // an INVERTED range that no x satisfies. A 20px-wide monitor is not
  // realistic as a monitor; it is exactly what a 1-2px sliver of a rounded
  // display rect looks like, and the fixtures in 8.1 already carry one.
  const pad = Math.max(8, pet.w * 0.5)
  assert.equal(pad, 15, `fixture sanity: pad is ${pad}`)
  assert.ok(1000 + pad > 1020 - pad, 'fixture sanity: the deadband window really is inverted')

  const narrow = sizeAt(pet, 1010)
  assert.equal(
    narrow, 60,
    'the pet must still adopt the narrow screen; a deadband that never releases is '
    + 'just a different bug, and it would wear the wrong size there permanently',
  )
  assert.equal(pet.display.id, 2, 'and hold it')

  // Held, not flickering: repeat reads at the same place must not oscillate.
  const hs = []
  for (let i = 0; i < 8; i++) { pet.x = 1010; pet.applyDisplaySize(); hs.push(pet.h) }
  assert.equal(new Set(hs).size, 1, `a stationary pet on the sliver must hold one size: ${hs.join(', ')}`)

  // And it can leave again.
  assert.equal(sizeAt(pet, 500), 30, 'walking back onto the wide screen still works')
  assert.equal(pet.display.id, 1)
})

test('a screen wide enough to enter properly still demands commitment', () => {
  // The complement of the test above: where the escape hatch does NOT apply,
  // the ordinary deadband rule must still be the one in force. Otherwise the
  // narrow-screen branch has quietly disabled hysteresis everywhere.
  const world = new World({
    stage: makeEl(), assetBase: '/assets/pets/', opts: { size: 60 }, scale: 1,
  })
  world.setSize(1200, 800)
  world.setDisplays([
    { id: 1, x: 0, y: 0, width: 1000, height: 800, floor: 700, sf: 1.0, ppi: 0 },
    { id: 2, x: 1000, y: 0, width: 200, height: 800, floor: 700, sf: 2.0, ppi: 0 },
  ], 2.0)
  world.setPlatforms([])
  const pet = world.add(SPEC('p1', { mode: 'free', platformId: null, x: 500, y: 500 }))

  assert.equal(sizeAt(pet, 500), 30, 'on the sparse screen')
  const pad = Math.max(8, pet.w * 0.5)              // 15
  assert.ok(200 > pad * 2, 'fixture sanity: this screen IS wide enough for the ordinary rule')

  // Inside the raw rectangle but inside the pad: not yet.
  assert.equal(sizeAt(pet, 1005), 30, 'one step past the seam is a jiggle, not a move')
  assert.equal(pet.display.id, 1)
  assert.equal(sizeAt(pet, 1014), 30, 'still inside the 15px pad')
  assert.equal(pet.display.id, 1)

  // Past the pad: committed.
  assert.equal(sizeAt(pet, 1016), 60, 'past x = 1000 + pad the pet has really moved')
  assert.equal(pet.display.id, 2)
})

// ── 8.16 the isolation machinery itself ──────────────────────────────────────
//
// `test()` at the top of this file resets the clock, the frame queue, the rAF
// handle counter and the RNG seed before every test, and fails a test that
// leaves a frame scheduled. That machinery is load-bearing for the statistical
// assertions in 8.4, so it gets tested like anything else -- an isolation
// harness that silently stopped isolating would make a future failure land on
// an innocent test.

test('the harness hands every test the same starting environment', () => {
  // Burn the environment thoroughly. The NEXT test asserts it came back.
  clock += 123456
  requestAnimationFrame(() => { throw new Error('this must never run') })
  for (let i = 0; i < 977; i++) Math.random()

  assert.ok(clock > 0 && rafQueue.size > 0, 'fixture sanity: the environment really is dirty')
  rafQueue.clear()   // or the leak guard would (correctly) fail this test
})

test('...and the environment really was restored, including the RNG stream', () => {
  assert.equal(clock, 0, 'the clock was reset after the previous test')
  assert.equal(pending(), 0, 'and the frame queue is empty')

  // The load-bearing half: the first three draws must be the first three draws
  // of the seed, not wherever the previous test's 977 calls left the stream.
  // Worked out from the seed constant at the top of this file.
  const first = [Math.random(), Math.random(), Math.random()]
  seed = SEED
  const again = [Math.random(), Math.random(), Math.random()]
  assert.deepEqual(
    first, again,
    'each test must start at the same point in the random stream, or inserting a '
    + 'test anywhere in this file silently re-rolls the dice for every test after it',
  )
})
