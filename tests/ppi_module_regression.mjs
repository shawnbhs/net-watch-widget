/**
 * Regression harness for `app/electron/ppi.js` — real physical panel density.
 *
 * HOW TO RUN
 * ----------
 * From the repository root, with nothing installed — no npm, no vite, no
 * Electron, no dependency of any kind beyond Node itself (>= 18):
 *
 *     node tests/ppi_module_regression.mjs
 *
 * TAP 13 to stdout, exit 0 when every test passes. To run it against a
 * DIFFERENT copy of the module — which is how you prove these tests are not
 * vacuous — point `NW_PPI_MODULE` at that file:
 *
 *     NW_PPI_MODULE=/tmp/ppi-prefix.js node tests/ppi_module_regression.mjs
 *
 * Against the pre-fix module the two `DEFECT` tests below are expected to FAIL
 * loudly; that is the point of them.
 *
 * WHY IT IS SHAPED LIKE THIS
 * --------------------------
 * `ppi.js` spawns a PowerShell child, reads EDID out of WMI and talks to Win32
 * through P/Invoke. None of that is testable directly, and a test that needed a
 * particular monitor plugged in would be untrustworthy everywhere else. So the
 * seam used here is the one real boundary the module has: `child_process.spawn`.
 * Everything on the far side of it is a fixture, and everything on this side is
 * the code under test, unmodified.
 *
 * Three consequences a reader should know about up front:
 *
 *  - The module is COPIED to a temp `.cjs` before it is loaded, once per test.
 *    `ppi.js` keeps its cache in module-level `let`s and probes at require
 *    time, so two tests sharing one instance would share a cache and a
 *    `complainOnce` latch. A fresh copy per test is the only way each test gets
 *    a module in a known state. The `.cjs` extension is what lets a CommonJS
 *    file be required from this ESM harness.
 *  - `spawn` is replaced on the `node:child_process` module object BEFORE the
 *    copy is loaded, and the fake child is an EventEmitter the test drives by
 *    hand. No PowerShell ever runs, nothing is timing-dependent, and a test can
 *    make a probe succeed, fail, hang or return garbage on demand.
 *  - Real measured numbers are used as fixtures — 167.761 PPI at origin (0,0)
 *    and 102.299 PPI at (-1920, 255), taken from an actual run of this module
 *    on the two-monitor desktop `docs/multi-monitor-sizing.md` describes. Using
 *    the real pair keeps the ratio (1.64x) in front of the reader, because that
 *    ratio is the size of the bug the whole module exists to prevent.
 *
 * WHAT IT PROTECTS
 * ----------------
 * Two defects this suite was written to close, both reproduced before they were
 * fixed:
 *
 *  1. `refresh()` was never called after startup. The module documented itself
 *     as "call on display-added/removed/metrics-changed" and exported the
 *     function, and nothing anywhere called it. A replugged monitor fell back to
 *     the scaleFactor approximation for the rest of the process's life.
 *     (`docs/CHANGES-this-cycle.md`, open item 12.)
 *
 *  2. A probe that SUCCEEDED and honestly reported "no panel here has usable
 *     EDID" was routed through the same `resolve(null)` as a probe that FAILED,
 *     so the stale cache survived it. Unplug a 167 PPI laptop, put an EDID-less
 *     panel at the same device-pixel origin, and `ppiFor()` returned 167 for a
 *     panel that is really ~102 — a confidently wrong number where the module's
 *     own contract promises 0, and 1.64x wrong in the direction that scales
 *     every sprite on that monitor.
 *
 * Plus the behaviours that were already correct and must stay that way:
 * probe coalescing, cache retention across a genuine failure, timeout recovery,
 * late output from a killed child being ignored, two identical monitors staying
 * deliberately ambiguous, and `ppiFor()` never throwing whatever it is handed.
 */

import nodeTest from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { EventEmitter } from 'node:events'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const require_ = createRequire(import.meta.url)
const HERE = path.dirname(fileURLToPath(import.meta.url))

const MODULE_UNDER_TEST = process.env.NW_PPI_MODULE
  ? path.resolve(process.env.NW_PPI_MODULE)
  : path.join(HERE, '..', 'app', 'electron', 'ppi.js')

if (!fs.existsSync(MODULE_UNDER_TEST)) {
  console.error('cannot find the module under test: ' + MODULE_UNDER_TEST)
  process.exit(2)
}

// ── the measured desktop, used as the fixture everywhere ─────────────────────
//
// Straight out of a real run of this module. Keep the real numbers: the whole
// point of the module is that these two panels differ by 1.64x while Windows
// reports both as "150%".
const LAPTOP = { x: 0, y: 0, w: 2560, h: 1600, ppi: 167.761, mmW: 388, mmH: 242, name: 'NE180QDM-NZC' }
const EXTERNAL = { x: -1920, y: 255, w: 1920, h: 1080, ppi: 102.299, mmW: 477, mmH: 268, name: 'T22C350' }
const DESKTOP = [LAPTOP, EXTERNAL]

/** A minimal stand-in for an Electron Display. */
function display(x, y, width, height, scaleFactor = 1) {
  return { bounds: { x, y, width, height }, scaleFactor }
}

// ── the spawn seam ───────────────────────────────────────────────────────────

const cp = require_('node:child_process')
const REAL_SPAWN = cp.spawn

/**
 * One loaded, isolated copy of the module with its `spawn` under our control.
 *
 * `script(call)` is invoked for every spawn and decides what that probe does.
 * It is settable mid-test, so one instance can see a good probe, then a failing
 * one, then a good one again — which is exactly the sequence a user produces by
 * unplugging a monitor.
 */
function load({ script, platform = 'win32', electron = null } = {}) {
  const calls = []
  let current = script || ((call) => complete(call, DESKTOP))

  cp.spawn = function fakeSpawn(exe, args, opts) {
    const child = new EventEmitter()
    const stream = () => {
      const s = new EventEmitter()
      s.setEncoding = () => {}
      return s
    }
    child.stdout = stream()
    child.stderr = stream()
    child.killed = false
    child.kill = () => { child.killed = true; return true }
    const call = { exe, args, opts, child, index: calls.length }
    calls.push(call)
    // Asynchronous, like a real spawn: nothing may resolve before the caller
    // has returned, or the coalescing logic would be tested in a world where
    // probes finish instantly and the queue could never be exercised.
    setImmediate(() => { try { current(call) } catch { /* the test will notice */ } })
    return child
  }

  // `electron` is not installed for this harness, and the module requires it
  // lazily inside try/catch. Injecting a fake through the require cache is how
  // the display-subscription and dipToScreenRect paths get exercised at all.
  const electronPath = path.join(HERE, '__fake_electron__.cjs')
  if (electron) {
    require_.cache[electronPath] = { id: electronPath, filename: electronPath, loaded: true, exports: electron }
    const Module = require_('node:module')
    if (!Module._nw_ppi_patched) {
      const realResolve = Module._resolveFilename
      Module._resolveFilename = function (request, ...rest) {
        if (request === 'electron' && require_.cache[electronPath]) return electronPath
        return realResolve.call(this, request, ...rest)
      }
      Module._nw_ppi_patched = true
    }
  } else {
    delete require_.cache[electronPath]
  }

  // The platform override is held for the whole test, not just across the
  // require. `refresh()` chains its trailing re-probe through a `.finally()`,
  // so a probe started under the fake platform can still reach `spawn` a
  // microtask later — by which time a flip-back would have lied to it.
  const realPlatform = Object.getOwnPropertyDescriptor(process, 'platform')
  Object.defineProperty(process, 'platform', { value: platform, configurable: true })

  // A fresh temp copy per load: the module's cache, its `probing`/`queued`
  // flags and its one-warning-per-process latch are all module-level state.
  const copy = path.join(
    fs.mkdtempSync(path.join(os.tmpdir(), 'nw-ppi-')),
    'ppi-under-test.cjs',
  )
  fs.copyFileSync(MODULE_UNDER_TEST, copy)

  const mod = require_(copy)      // require-time probe fires here

  return {
    mod,
    calls,
    setScript: (fn) => { current = fn },
    cleanup: () => {
      Object.defineProperty(process, 'platform', realPlatform)
      cp.spawn = REAL_SPAWN
      delete require_.cache[copy]
      delete require_.cache[electronPath]
      try { fs.rmSync(path.dirname(copy), { recursive: true, force: true }) } catch { /* temp */ }
    },
  }
}

/** Drive a fake probe to a successful completion with these rows. */
function complete(call, rows) {
  call.child.stdout.emit('data', JSON.stringify(rows))
  call.child.emit('close', 0)
}

/** Drive a fake probe to a non-zero exit — a genuine failure to find out. */
function fail(call, message = 'WMI is busy') {
  call.child.stderr.emit('data', message + '\n')
  call.child.emit('close', 1)
}

/** Let every pending microtask and immediate drain. */
const settle = async (rounds = 6) => {
  for (let i = 0; i < rounds; i++) await new Promise((r) => setImmediate(r))
}

// ── test wrapper: every test gets a clean module and restores global state ───

let passed = 0
let failed = 0

function test(name, fn) {
  nodeTest(name, async (t) => {
    const instances = []
    const make = (opts) => {
      const inst = load(opts)
      instances.push(inst)
      return inst
    }
    try {
      await fn({ load: make, t })
      passed++
    } catch (err) {
      failed++
      throw err
    } finally {
      for (const inst of instances) inst.cleanup()
      cp.spawn = REAL_SPAWN
    }
  })
}

// ── 1. the probe itself ──────────────────────────────────────────────────────

test('the probe runs at require time and its results are readable', async ({ load }) => {
  const { mod, calls } = load()
  await settle()

  assert.equal(calls.length, 1, 'requiring the module probes exactly once')
  assert.equal(mod.snapshot().length, 2, 'both measured panels are cached')
  assert.equal(mod.ppiFor(display(0, 0, 2560, 1600)), 167.761)
  assert.equal(mod.ppiFor(display(-1920, 255, 1920, 1080)), 102.299)
})

test('PowerShell is invoked safely: no profile, no prompt, hidden, base64 script', async ({ load }) => {
  const { calls } = load()
  await settle()

  const { args, opts } = calls[0]
  for (const flag of ['-NoProfile', '-NonInteractive', '-EncodedCommand']) {
    assert.ok(args.includes(flag), 'the probe passes ' + flag)
  }
  assert.equal(opts.windowsHide, true, 'no console window flashes on the user')
  assert.equal(opts.stdio[0], 'ignore', 'the child can never block waiting on stdin')

  // -EncodedCommand is UTF-16LE base64. If this ever silently becomes a
  // -Command string, the script's quotes/braces/here-string get one more layer
  // of mangling on the way to PowerShell.
  const script = Buffer.from(args[args.indexOf('-EncodedCommand') + 1], 'base64').toString('utf16le')
  assert.match(script, /SetProcessDpiAwareness\(2\)/,
    'the probe must declare per-monitor DPI awareness BEFORE it measures anything, '
    + 'or Windows virtualises the coordinates and the probe invents a scale difference '
    + 'that does not exist (trap 4.2 in docs/multi-monitor-sizing.md)')
  assert.match(script, /WmiMonitorListedSupportedSourceModes/,
    'millimetres must come from the EDID preferred source mode, not the rounded '
    + 'centimetre field (trap 4.6)')
  assert.match(script, /\[NullString\]::Value/,
    'EnumDisplayDevices needs [NullString]::Value; PowerShell marshals $null as an '
    + 'empty string and the call then returns no devices at all')
})

test('a single monitor arrives as a bare object, not a one-element array', async ({ load }) => {
  // Some PowerShell versions do this and it is not configurable.
  const { mod } = load({ script: (call) => complete(call, LAPTOP) })
  await settle()

  assert.equal(mod.snapshot().length, 1)
  assert.equal(mod.ppiFor(display(0, 0, 2560, 1600)), 167.761)
})

test('nothing is probed, ever, off Windows', async ({ load }) => {
  const inst = load({ platform: 'linux' })
  await settle()
  inst.mod.refresh()
  inst.mod.refresh()
  await settle()

  assert.equal(inst.calls.length, 0, 'no PowerShell is spawned on a platform that has none')
  assert.equal(inst.mod.ppiFor(display(0, 0, 2560, 1600)), 0,
    'and the answer is "unknown", not an error')
})

// ── 2. DEFECT 1: the cache must follow the hardware ──────────────────────────

test('DEFECT 1: the module subscribes to display changes instead of probing once and going stale',
  async ({ load }) => {
    const handlers = new Map()
    const screen = {
      on: (event, fn) => {
        if (!handlers.has(event)) handlers.set(event, [])
        handlers.get(event).push(fn)
      },
      dipToScreenRect: () => null,
    }
    const { calls } = load({ electron: { screen, app: { isReady: () => true, once: () => {} } } })
    await settle()

    assert.equal(calls.length, 1, 'the require-time probe still happens')

    // This is the whole defect: before the fix, NOTHING was ever registered, so
    // a replugged monitor produced no cache match and the display fell back to
    // the scaleFactor approximation for the rest of the process's life.
    for (const event of ['display-added', 'display-removed', 'display-metrics-changed']) {
      assert.ok(
        (handlers.get(event) || []).length > 0,
        'the module must react to ' + event + ' — its own header documents refresh() as '
        + '"call on display-added/removed/metrics-changed", and a contract that depends on '
        + 'some other file remembering to honour it is a contract that silently rots '
        + '(it already had: nothing called it)',
      )
    }
  })

test('DEFECT 1: a display event actually re-reads the hardware', async ({ load }) => {
  const handlers = new Map()
  const screen = {
    on: (e, fn) => { handlers.set(e, fn) },
    dipToScreenRect: () => null,
  }
  let rows = [LAPTOP]
  const inst = load({
    electron: { screen, app: { isReady: () => true, once: () => {} } },
    script: (call) => complete(call, rows),
  })
  await settle()
  assert.equal(inst.mod.ppiFor(display(0, 0, 2560, 1600)), 167.761)

  // The user docks the machine: the panel at (0,0) is now the 102 PPI monitor.
  rows = [{ ...EXTERNAL, x: 0, y: 0 }]
  handlers.get('display-metrics-changed')()

  // The re-probe is deliberately debounced (Windows fires this event several
  // times for one settings change), so drive the timer rather than waiting.
  await new Promise((r) => setTimeout(r, 2200))
  await settle()

  assert.ok(inst.calls.length >= 2, 'a display change triggers a fresh probe')
  assert.equal(
    inst.mod.ppiFor(display(0, 0, 1920, 1080)), 102.299,
    'and the new panel reports ITS density, not the departed one\'s',
  )
})

test('DEFECT 1: an event storm is debounced into one re-probe, not one probe per event',
  async ({ load }) => {
    const handlers = new Map()
    const screen = { on: (e, fn) => { handlers.set(e, fn) }, dipToScreenRect: () => null }
    const { calls } = load({ electron: { screen, app: { isReady: () => true, once: () => {} } } })
    await settle()
    const before = calls.length

    // Windows fires this repeatedly while the display settings dialog is open.
    // One PowerShell per event would be a visible stall on the main process.
    for (let i = 0; i < 20; i++) handlers.get('display-metrics-changed')()
    await new Promise((r) => setTimeout(r, 2200))
    await settle()

    assert.equal(calls.length - before, 1, '20 events cost exactly one probe')
  })

test('DEFECT 1: subscribing before app.ready retries on ready rather than giving up',
  async ({ load }) => {
    // Electron throws if the screen module is touched before the app is ready,
    // and a main-process module being required early is the normal case.
    let readyHandler = null
    const handlers = new Map()
    let ready = false
    const electron = {
      app: {
        isReady: () => ready,
        once: (event, fn) => { if (event === 'ready') readyHandler = fn },
      },
      get screen() {
        if (!ready) throw new Error('cannot use screen module before app is ready')
        return { on: (e, fn) => { handlers.set(e, fn) }, dipToScreenRect: () => null }
      },
    }
    const { mod } = load({ electron })
    await settle()

    assert.equal(handlers.size, 0, 'nothing could be registered yet')
    assert.ok(readyHandler, 'but a retry was armed on app.ready instead of failing silently')

    ready = true
    readyHandler()
    await settle()

    assert.equal(handlers.size, 3, 'and on ready, all three display events are subscribed')
    assert.equal(typeof mod.watchDisplayChanges, 'function', 'the hook stays callable by a host too')
  })

test('outside Electron the module still loads and simply does not watch', async ({ load }) => {
  // This file is runnable under plain node for diagnostics; requiring
  // `electron` there throws, and that must not take the module down.
  const { mod } = load({ electron: null })
  await settle()

  assert.equal(mod.watchDisplayChanges(), false, 'nothing to subscribe to, and it says so')
  assert.equal(mod.snapshot().length, 2, 'the probe itself still worked')
})

// ── 3. DEFECT 2: a successful "nothing here" must not be read as "do not know" ─

test('DEFECT 2: a monitor swap to an EDID-less panel reports unknown, not the dead panel\'s PPI',
  async ({ load }) => {
    let rows = [LAPTOP]
    const { mod } = load({ script: (call) => complete(call, rows) })
    await settle()
    assert.equal(mod.ppiFor(display(0, 0, 2560, 1600)), 167.761, 'the laptop is known')

    // The laptop is unplugged and a cheap EDID-less monitor takes its place at
    // the same device-pixel origin. The probe SUCCEEDS — it enumerates the
    // display fine — and honestly reports that this panel has no EDID.
    rows = [{ x: 0, y: 0, w: 1920, h: 1080, ppi: null, name: 'no-EDID panel' }]
    mod.refresh()
    await settle()

    const answer = mod.ppiFor(display(0, 0, 1920, 1080))
    assert.equal(
      answer, 0,
      'a probe that ran and found nothing usable is a CURRENT answer ("unknown"), not a '
      + 'failure to find out. Routing it through the same null as a crashed probe kept the '
      + 'departed laptop in the cache, and this call returned ' + answer + ' for a panel that '
      + 'is really ~102 PPI — 1.64x wrong, in the direction that scales every sprite on that '
      + 'monitor, and wrong CONFIDENTLY: returning a number means the scaleFactor fallback is '
      + 'never reached to correct it',
    )
    assert.equal(mod.snapshot().length, 0, 'and the stale entry is gone, not merely unmatched')
  })

test('DEFECT 2: the distinction is exactly failure-vs-empty, so a real failure still keeps the cache',
  async ({ load }) => {
    // The other half of the fix. It would be easy to "fix" defect 2 by always
    // replacing the cache, which would throw away good data every time WMI was
    // momentarily busy — a regression dressed as a fix.
    let mode = 'ok'
    const { mod } = load({
      script: (call) => (mode === 'ok' ? complete(call, DESKTOP) : fail(call, 'WMI is busy')),
    })
    await settle()

    mode = 'fail'
    mod.refresh()
    await settle()

    assert.equal(mod.snapshot().length, 2, 'the panels did not stop existing because WMI was busy')
    assert.equal(mod.ppiFor(display(0, 0, 2560, 1600)), 167.761)
  })

test('DEFECT 2: unparseable output is a failure, not an empty answer', async ({ load }) => {
  let junk = false
  const { mod } = load({
    script: (call) => {
      if (!junk) return complete(call, DESKTOP)
      call.child.stdout.emit('data', 'At line:1 char:1 <<< not json at all')
      call.child.emit('close', 0)
    },
  })
  await settle()

  junk = true
  mod.refresh()
  await settle()

  assert.equal(mod.snapshot().length, 2,
    'garbage on stdout means the probe did not tell us anything — it does not mean the '
    + 'monitors vanished')
})

// ── 4. probe lifecycle: coalescing, timeout, late output ─────────────────────

test('overlapping refreshes collapse into one trailing re-probe', async ({ load }) => {
  // Windows fires display-metrics-changed several times for one settings
  // change; a probe per event would be a pile of PowerShell processes.
  const pending = []
  const { mod, calls } = load({ script: (call) => pending.push(call) })
  await settle()
  assert.equal(calls.length, 1, 'the load-time probe is in flight')

  for (let i = 0; i < 8; i++) mod.refresh()
  await settle()
  assert.equal(calls.length, 1, 'while one is in flight, no second process is started')

  complete(pending.shift(), DESKTOP)
  await settle()
  assert.equal(calls.length, 2, 'the eight queued calls became exactly one trailing re-probe')

  complete(pending.shift(), DESKTOP)
  await settle()
  assert.equal(calls.length, 2, 'and it does not then loop forever')
})

test('a hung probe is killed, does not wedge the module, and a later refresh recovers',
  async ({ load }) => {
    // The module must never block: it is loaded by the main process and read
    // while building the overlay payload.
    const realSetTimeout = global.setTimeout
    global.setTimeout = function (fn, ms, ...rest) {
      // Compress only the probe's own 20s watchdog; everything else is untouched.
      return realSetTimeout(fn, ms === 20000 ? 15 : ms, ...rest)
    }
    try {
      let hang = true
      const { mod, calls } = load({
        script: (call) => { if (!hang) complete(call, DESKTOP) },
      })
      await new Promise((r) => realSetTimeout(r, 60))
      await settle()

      assert.equal(calls[0].child.killed, true, 'the timed-out child is killed, not leaked')
      assert.equal(mod.ppiFor(display(0, 0, 2560, 1600)), 0, 'and the answer is unknown, not a throw')

      hang = false
      mod.refresh()
      await settle()
      assert.ok(calls.length >= 2, 'the module is not wedged: a later probe starts')
      assert.equal(mod.ppiFor(display(0, 0, 2560, 1600)), 167.761, 'and it recovers fully')
    } finally {
      global.setTimeout = realSetTimeout
    }
  })

test('late output from an already-abandoned probe cannot overwrite a good cache',
  async ({ load }) => {
    const realSetTimeout = global.setTimeout
    global.setTimeout = function (fn, ms, ...rest) {
      return realSetTimeout(fn, ms === 20000 ? 15 : ms, ...rest)
    }
    try {
      let hang = true
      const { mod, calls } = load({ script: (call) => { if (!hang) complete(call, DESKTOP) } })
      await new Promise((r) => realSetTimeout(r, 60))
      await settle()

      hang = false
      mod.refresh()
      await settle()
      assert.equal(mod.ppiFor(display(0, 0, 2560, 1600)), 167.761)

      // The killed child finally gets round to writing. A resolved promise
      // cannot resolve twice, but the `done` latch is what makes that true of
      // the surrounding handlers too.
      complete(calls[0], [{ x: 0, y: 0, w: 9, h: 9, ppi: 500, name: 'ghost' }])
      await settle()

      assert.equal(mod.ppiFor(display(0, 0, 2560, 1600)), 167.761,
        'a zombie probe does not get to install a cache that is two minutes out of date')
      assert.ok(!mod.snapshot().some((e) => e.name === 'ghost'))
    } finally {
      global.setTimeout = realSetTimeout
    }
  })

test('a spawn that fails outright is survivable and silent after the first line',
  async ({ load }) => {
    const warnings = []
    const realWarn = console.warn
    console.warn = (...a) => warnings.push(a.join(' '))
    try {
      const { mod } = load({ script: (call) => fail(call, 'first failure') })
      await settle()
      for (let i = 0; i < 5; i++) { mod.refresh(); await settle() }

      assert.equal(mod.ppiFor(display(0, 0, 2560, 1600)), 0, 'unknown, never a throw')
      assert.equal(warnings.length, 1,
        'one line of noise per process however often the probe fails — this runs on a '
        + 'main process that may be up for weeks')
      assert.match(warnings[0], /falling back to scaleFactor/)
    } finally {
      console.warn = realWarn
    }
  })

// ── 5. matching a display to a panel ─────────────────────────────────────────

test('origins are matched with a small tolerance, because the two sides round independently',
  async ({ load }) => {
    // Electron's DIP bounds times scaleFactor is a float; Windows' dmPosition
    // is an exact integer. A couple of pixels of slack costs nothing, and
    // origins cannot collide because monitors may not overlap.
    const { mod } = load()
    await settle()

    assert.equal(mod.ppiFor(display(0, 0, 2560, 1600)), 167.761, 'exact')
    assert.equal(mod.ppiFor(display(-1918, 256, 1920, 1080)), 102.299, 'within tolerance')
    assert.equal(mod.ppiFor(display(-1900, 255, 700, 400)), 0,
      '18px out with no size match is a different monitor, not a rounding error')
  })

test('two identical monitors stay ambiguous rather than guessing', async ({ load }) => {
  // A unique device-pixel size is the last-resort match. Two panels of the same
  // size must NOT resolve: guessing wrong here is a 1.64x error on a sprite,
  // and unknown (0) routes the caller to the scaleFactor fallback instead.
  const twins = [
    { x: 0, y: 0, w: 1920, h: 1080, ppi: 102.3, name: 'left' },
    { x: 1920, y: 0, w: 1920, h: 1080, ppi: 157.4, name: 'right' },
  ]
  const { mod } = load({ script: (call) => complete(call, twins) })
  await settle()

  assert.equal(mod.ppiFor(display(0, 0, 1920, 1080)), 102.3, 'an exact origin still resolves')
  assert.equal(mod.ppiFor(display(1920, 0, 1920, 1080)), 157.4)
  assert.equal(
    mod.ppiFor(display(600, 3000, 1920, 1080)), 0,
    'but an unknown origin whose SIZE matches two panels must report unknown — the '
    + 'two candidates differ by 1.54x, so a guess is worse than no answer',
  )
})

test('a unique size match is accepted when the origin does not resolve', async ({ load }) => {
  const { mod } = load()
  await settle()
  assert.equal(
    mod.ppiFor(display(9999, 9999, 2560, 1600)), 167.761,
    'only one panel is 2560x1600, so the match is unambiguous',
  )
})

test('EDID outside the believable range is rejected as data, not scaled into a pet',
  async ({ load }) => {
    // A projector reporting 0mm, a VM inventing a 1mm panel, a TV claiming
    // 3 metres. Accepting these would scale a sprite by a factor of forty.
    const rows = [
      { x: 0, y: 0, w: 1920, h: 1080, ppi: 2.5, name: 'TV claiming 3 metres' },
      { x: 1920, y: 0, w: 1920, h: 1080, ppi: 4000, name: 'VM with a 1mm panel' },
      { x: 3840, y: 0, w: 1920, h: 1080, ppi: 0, name: 'projector reporting 0mm' },
      { x: 5760, y: 0, w: 2560, h: 1600, ppi: 167.761, name: 'an honest panel' },
    ]
    const { mod } = load({ script: (call) => complete(call, rows) })
    await settle()

    assert.deepEqual(mod.snapshot().map((e) => e.name), ['an honest panel'],
      'only the believable panel is cached')
    assert.equal(mod.ppiFor(display(0, 0, 1920, 1080)), 0)
    assert.equal(mod.ppiFor(display(1920, 0, 1920, 1080)), 0)
  })

test('rows missing the join key are dropped without taking the good ones with them',
  async ({ load }) => {
    const rows = [
      null,
      { ppi: 120 },                                        // no origin at all
      { x: '0', y: 0, w: 10, h: 10, ppi: 120 },            // origin as a string
      { x: 0, y: 0, w: 2560, h: 1600, ppi: 'lots' },       // ppi not a number
      LAPTOP,
    ]
    const { mod } = load({ script: (call) => complete(call, rows) })
    await settle()

    assert.equal(mod.snapshot().length, 1, 'one usable row out of five')
    assert.equal(mod.ppiFor(display(0, 0, 2560, 1600)), 167.761)
  })

test('the exact device-pixel rect from Electron is preferred over bounds * scaleFactor',
  async ({ load }) => {
    // Chromium accumulates a secondary display's DIP offset through its
    // NEIGHBOURS' scale factors, so multiplying by the display's own is wrong:
    // measured, a panel really at device (-1920, 255) computes to (-1921, 169)
    // — the y 86px out, far past any tolerance, silently matching nothing (or
    // worse, the wrong monitor). screen.dipToScreenRect does the full walk.
    const screen = {
      on: () => {},
      dipToScreenRect: (_win, rect) => {
        assert.equal(rect.x, -1746, 'the module passes the DIP rect through unchanged')
        return { x: -1920, y: 255, width: 1920, height: 1080 }
      },
    }
    const { mod } = load({ electron: { screen, app: { isReady: () => true, once: () => {} } } })
    await settle()

    // DIP (-1746, 154) at scaleFactor 1.10 multiplies out to (-1921, 169),
    // which matches nothing. Via dipToScreenRect it is exactly (-1920, 255).
    assert.equal(
      mod.ppiFor(display(-1746, 154, 1745, 981, 1.1)), 102.299,
      'the external monitor resolves only because the exact rect was used',
    )
  })

test('without Electron the multiply fallback still matches when it is good enough',
  async ({ load }) => {
    const { mod } = load({ electron: null })
    await settle()
    assert.equal(
      mod.ppiFor(display(0, 0, 2560, 1600, 1)), 167.761,
      'the fallback is fine for the display containing the DIP origin, which is why '
      + 'it is kept for the plain-node diagnostics path',
    )
  })

// ── 6. the contract: never throw, never block, never leak ────────────────────

test('ppiFor returns 0 for anything malformed and never throws', async ({ load }) => {
  // It is called while assembling the overlay payload, once per display per
  // update. An exception there takes the whole overlay down.
  const { mod } = load()
  await settle()

  const hostile = [
    undefined, null, 0, '', false, NaN, [], {},
    { bounds: null },
    { bounds: {} },
    { bounds: { x: NaN, y: 0, width: 1, height: 1 } },
    { bounds: { x: '0', y: '0', width: 1, height: 1 } },
    { bounds: { x: 0, y: 0, width: 1, height: 1 }, scaleFactor: 0 },
    { bounds: { x: 0, y: 0, width: 1, height: 1 }, scaleFactor: -3 },
    { bounds: { x: Infinity, y: 0, width: 1, height: 1 } },
    { get bounds() { throw new Error('hostile getter') } },
  ]
  for (const input of hostile) {
    let result
    assert.doesNotThrow(() => { result = mod.ppiFor(input) },
      'ppiFor must never throw — the overlay payload is built on this path')
    assert.equal(typeof result, 'number', 'and must always answer with a number')
    assert.ok(Number.isFinite(result), 'never NaN: NaN propagates into layout and yields '
      + 'an INVISIBLE sprite, the one outcome that must never happen')
  }
})

test('ppiFor does no I/O, so it can be called once per display per frame', async ({ load }) => {
  const { mod, calls } = load()
  await settle()
  const before = calls.length

  for (let i = 0; i < 500; i++) mod.ppiFor(display(0, 0, 2560, 1600))
  assert.equal(calls.length, before, 'a pure cache read never spawns anything')
})

test('snapshot hands out copies, so a diagnostics caller cannot corrupt the cache',
  async ({ load }) => {
    const { mod } = load()
    await settle()

    const first = mod.snapshot()
    first[0].ppi = 999
    first[0].name = 'vandalised'

    assert.equal(mod.snapshot()[0].ppi, 167.761, 'the cache is unaffected')
    assert.equal(mod.ppiFor(display(0, 0, 2560, 1600)), 167.761)
  })

test('requiring the module cannot fail, even when spawn throws synchronously',
  async ({ load }) => {
    const realSpawn = cp.spawn
    cp.spawn = () => { throw new Error('EPERM: locked-down box') }
    try {
      let mod
      assert.doesNotThrow(() => {
        const copy = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'nw-ppi-')), 'p.cjs')
        fs.copyFileSync(MODULE_UNDER_TEST, copy)
        const realPlatform = Object.getOwnPropertyDescriptor(process, 'platform')
        Object.defineProperty(process, 'platform', { value: 'win32', configurable: true })
        try { mod = require_(copy) } finally {
          Object.defineProperty(process, 'platform', realPlatform)
          delete require_.cache[copy]
        }
      }, 'requiring this module must never be able to fail')
      assert.equal(mod.ppiFor(display(0, 0, 2560, 1600)), 0)
    } finally {
      cp.spawn = realSpawn
    }
  })

test('the probe watchdog does not hold the process open', async ({ load }) => {
  // A 20s timer that is not unref'd keeps an Electron main process — or this
  // test run — alive after everything else is done.
  const timers = []
  const realSetTimeout = global.setTimeout
  global.setTimeout = function (fn, ms, ...rest) {
    const t = realSetTimeout(fn, ms, ...rest)
    timers.push({ ms, unrefed: false, handle: t })
    const realUnref = t.unref.bind(t)
    t.unref = () => { timers.find((x) => x.handle === t).unrefed = true; return realUnref() }
    return t
  }
  try {
    load({ script: () => {} })
    await settle()
    const watchdog = timers.find((t) => t.ms === 20000)
    assert.ok(watchdog, 'the probe arms a watchdog')
    assert.equal(watchdog.unrefed, true, 'and unrefs it, so it can never hold the app open')
  } finally {
    global.setTimeout = realSetTimeout
    for (const t of timers) { try { clearTimeout(t.handle) } catch { /* done */ } }
  }
})

process.on('exit', () => {
  console.log('# ppi module: ' + passed + ' passed, ' + failed + ' failed')
})
