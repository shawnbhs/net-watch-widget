// Sprites and the desktop overlay, for the widget's pets.
//
// Two jobs that have little to do with each other beyond both being the main
// process's half of one feature.
//
// 1. The sprite manifest. The vscode-pets set names every clip
//    `<colour>_<action>_8fps.gif`, so the folder is scanned once at boot rather
//    than four hundred filenames being written down. Dropping a new species
//    folder into assets/pets is enough to add it.
//
// 2. The overlay window. A pet set loose on the desktop cannot live in the
//    widget: that window is sized to its own content and travels with it when
//    it is dragged. Loose pets get a full-screen, transparent, click-through
//    window of their own -- created only once something is actually roaming in
//    it, and destroyed again when the last one comes home.

const { BrowserWindow, screen } = require('electron')
const fs = require('node:fs')
const path = require('node:path')

const ASSET_ROOT = path.join(__dirname, '..', 'assets', 'pets')

/**
 * Real panel PPI, from the display's EDID, or 0 when it cannot be known.
 *
 * Optional by design, and permanently guarded. The overlay's sizing already
 * has a working fallback -- each display's own `scaleFactor` -- so a PPI
 * source that is absent, not yet installed, or throws on this platform must
 * degrade to that fallback rather than take the whole overlay down: no pet is
 * worth a main-process crash, and a slightly-wrong pet size is recoverable
 * where a dead window is not.
 */
let ppiResolver
try {
  ({ ppiFor: ppiResolver } = require('./ppi'))
} catch {
  ppiResolver = null
}

function ppiOf(display) {
  if (typeof ppiResolver !== 'function') return 0
  try {
    const v = ppiResolver(display)
    return Number.isFinite(v) && v > 0 ? v : 0
  } catch {
    return 0
  }
}

// ── the sprite manifest ───────────────────────────────────────────────────────

// Longest first, so `walk_fast` is matched before `walk`.
const ACTIONS = [
  'fall_from_grab', 'wallclimb', 'wallgrab', 'walk_fast', 'with_ball',
  'stand', 'swipe', 'idle', 'walk', 'run', 'lie', 'jump', 'land',
]

/**
 * Per-species size, relative to every other species.
 *
 * The source art is drawn at wildly different resolutions -- 68x64 for the
 * skeleton, 250x250 for totoro -- so normalising every clip to a common height
 * makes a snail the size of a horse. These multipliers are applied after that
 * normalisation to put each species back on a believable scale beside the rest.
 */
const SPECIES_SCALE = {
  crab: 0.72, snail: 0.66, rat: 0.74, 'rubber-duck': 0.78, zappy: 0.8,
  chicken: 0.85, snake: 0.85, monkey: 0.9, cockatiel: 0.85, turtle: 0.8,
  fox: 1.0, dog: 1.0, panda: 1.05, raccoon: 0.95, clippy: 0.95,
  skeleton: 1.0, mod: 0.95, deno: 0.95, rocky: 0.7, morph: 0.9,
  totoro: 1.25, horse: 1.3,
}

const SPECIES_LABEL = {
  'rubber-duck': 'Rubber Duck', mod: 'Mod the Bot', totoro: 'Totoro',
}

function titleCase(s) {
  return s.split(/[-_]/).map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
}

function splitClipName(base) {
  for (const action of ACTIONS) {
    if (base === action) return { color: 'default', action }
    if (base.endsWith(`_${action}`)) {
      return { color: base.slice(0, -(action.length + 1)), action }
    }
  }
  return null
}

/**
 * Per-clip sprite metrics: `[canvasW, canvasH, boxX, boxY, boxW, boxH]`, where
 * the box is the opaque area of the drawn character, unioned over every frame.
 *
 * Without these a pet visibly resizes whenever it changes animation and its feet
 * drift off whatever it is standing on: the source frames have inconsistent
 * canvas padding, so the same dog sits 6px from the bottom of one clip and 20px
 * from the bottom of the next. Regenerate with `python app/tools/gen-metrics.py`
 * after adding sprites.
 */
function loadMetrics() {
  try {
    return JSON.parse(fs.readFileSync(path.join(ASSET_ROOT, 'metrics.json'), 'utf8'))
  } catch {
    console.error('[pets] no metrics.json — falling back to raw sprite sizes')
    return {}
  }
}

/**
 * The clip a species' scale is measured against.
 *
 * Everything else keeps its own proportions relative to this one, so a lying pet
 * stays low and totoro's catbus stays big, instead of every animation being
 * squashed to a single height.
 */
const REFERENCE_ACTIONS = ['idle', 'stand', 'walk', 'lie', 'run']

function buildManifest() {
  const metrics = loadMetrics()
  let dirs = []
  try {
    dirs = fs.readdirSync(ASSET_ROOT, { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => d.name)
      .sort()
  } catch (err) {
    console.error('[pets] cannot read', ASSET_ROOT, err.message)
    return []
  }

  const species = []

  for (const id of dirs) {
    const files = fs.readdirSync(path.join(ASSET_ROOT, id))
    const variants = new Map()
    const icons = new Map()

    for (const file of files) {
      if (file.endsWith('.png')) {
        const m = /^icon(?:_(.+))?\.png$/.exec(file)
        if (m) icons.set(m[1] || '__default__', `${id}/${file}`)
        continue
      }
      if (!file.endsWith('.gif')) continue

      const base = file.replace(/_8fps\.gif$/, '').replace(/\.gif$/, '')
      const parsed = splitClipName(base)
      if (!parsed) continue

      if (!variants.has(parsed.color)) variants.set(parsed.color, {})
      variants.get(parsed.color)[parsed.action] = `${id}/${file}`
    }

    if (variants.size === 0) continue

    species.push({
      id,
      label: SPECIES_LABEL[id] || titleCase(id),
      scale: SPECIES_SCALE[id] === undefined ? 1 : SPECIES_SCALE[id],
      variants: [...variants.entries()]
        .sort((a, b) => a[0].localeCompare(b[0]))
        .map(([color, clips]) => {
          const m = {}
          for (const [action, rel] of Object.entries(clips)) {
            if (metrics[rel]) m[action] = metrics[rel]
          }

          let baseH = 0
          for (const action of REFERENCE_ACTIONS) {
            if (m[action]) { baseH = m[action][5]; break }
          }
          if (!baseH) {
            const any = Object.values(m)[0]
            if (any) baseH = any[5]
          }

          return {
            color,
            label: titleCase(color),
            clips,
            m,
            baseH,
            icon: icons.get(color) || icons.get('__default__') || clips.idle,
          }
        }),
    })
  }

  return species
}

/**
 * The built species list, kept only once it is worth keeping.
 *
 * Caching an empty result was a latch. An asset root that read back empty once
 * -- a folder momentarily unreadable, a pack still being unpacked, a disk that
 * answered late -- was remembered for the life of the process, so every retry
 * the renderer scheduled and every press of its Retry button was answered from
 * that same empty array. The symptom is the feature silently absent with no
 * explanation anywhere. Only a manifest with species in it is worth keeping;
 * anything else is rebuilt on a later ask.
 */
let manifest = null

/**
 * The floor between two failed builds, in ms.
 *
 * Not caching failure invites the opposite fault: a desktop with no sprite pack
 * installed is legitimately empty for good, and the renderer's retries, the
 * overlay sync and any future caller would between them walk the asset tree
 * again on every single ask. This bounds that to one directory scan per
 * interval, while staying short enough that a real retry still reaches the
 * disk -- the renderer's backoff is 400/1200/3000/8000 ms, so every retry past
 * the first one clears the floor, as does any manually pressed Retry.
 *
 * A wall-clock step backwards only makes the next build happen sooner, which
 * is the harmless direction.
 */
const BUILD_RETRY_FLOOR_MS = 1500
let nextBuildAt = 0

/**
 * The species list: built on the first ask, kept once it has something in it.
 *
 * Returns `[]` for both "could not read" and "read, and there is nothing
 * there", because the caller cannot act on the difference and both are
 * retryable. No separate invalidation entry point is exported: the only way in
 * is the `pet-manifest` IPC handler, and a failed build already re-reads on the
 * next call past the floor above, so a Retry button needs nothing more than to
 * ask again.
 */
function getManifest() {
  if (manifest) return manifest

  const now = Date.now()
  if (now < nextBuildAt) return []
  nextBuildAt = now + BUILD_RETRY_FLOOR_MS

  let built
  try {
    built = buildManifest()
  } catch (err) {
    // A species folder can disappear between the parent listing and its own
    // read. Reported as a retryable miss rather than thrown across IPC, so the
    // caller sees the same "nothing yet" an empty asset root gives it.
    console.error('[pets] manifest build failed:', err.message)
    return []
  }

  console.error(`[pets] ${built.length} species`)
  if (built.length === 0) return []

  manifest = built
  return manifest
}

// ── the desktop overlay ───────────────────────────────────────────────────────

let overlay = null
/**
 * What the overlay is told the moment it finishes loading.
 *
 * The roster arrives from the widget in the same tick that asks for the window,
 * which is long before the page exists to receive it. Holding the last state
 * here and replaying it on `did-finish-load` is what stops the first pet let
 * loose from needing a second click to appear.
 */
let pending = null
let ignoring = null

/**
 * The whole desktop -- every display, not just the primary one.
 *
 * The overlay used to be the primary display's rectangle, which made "let a pet
 * roam the desktop" quietly mean "roam *this* desktop": a pet could not be
 * dragged onto a second monitor because there was no window over it to drag
 * into. So the window is the bounding box of every display, and a pet walks
 * between screens the same way it walks anywhere else.
 *
 * Full display bounds rather than work areas, because a pet wandering the
 * desktop should be able to pass in front of the taskbar -- that is where a
 * desktop pet belongs. Where it comes to *rest* is a different question, and
 * one each screen answers for itself: monitors do not share a taskbar, or a
 * height, or even a top edge. Hence `displays`, each with its own floor,
 * already converted into the overlay's own coordinates so the renderer never
 * has to know where on the desktop it happens to be.
 *
 * `workBottom` stays for the single frame before this arrives, when the page
 * has only its own window to go on. It is the primary display's work area
 * applied desktop-wide -- deliberately, as a one-frame stand-in -- and is
 * superseded by each display's own `floor` the moment `displays` lands.
 *
 * Units are the whole difficulty. Electron measures displays in DIPs; the
 * renderer lays out in CSS px. On a mixed-DPI desktop those are different
 * units and no single constant relates them: every monitor divides its own
 * device pixels by its own `scaleFactor`, so the DIP desktop is a *piecewise*
 * scaled copy of the physical one, while the overlay -- one window -- has
 * exactly one CSS px, namely the device pixel divided by the scale of the
 * monitor the OS associated the window with. Handing DIP rectangles to a page
 * measuring in CSS px is what left `displays` hundreds of pixels outside the
 * viewport, and pets invisible, once a second monitor at a different scale was
 * plugged in. Everything below therefore routes through device pixels -- the
 * one space both ends agree on -- and divides by the window's own scale only
 * at the very end, so the payload is entirely in the renderer's units.
 */

/**
 * A DIP rectangle in device pixels, or null where the platform has no separate
 * notion of one (macOS and Linux hand Chromium a single DIP space that is
 * already the renderer's CSS px, so there is nothing to convert).
 */
function physOf(rect) {
  if (typeof screen.dipToScreenRect !== 'function') return null
  try {
    return screen.dipToScreenRect(null, rect)
  } catch {
    return null
  }
}

/**
 * The persistence key for one display's TASTE multiplier, `K(d)`.
 *
 * This is a deliberate, documented duplicate of `displayTasteKey()` in
 * `app/src/pets/store.jsx`, and the two MUST produce the same string for the
 * same panel. The key is the join between two processes: the widget writes
 * `taste[key]` from the renderer's copy of the display list, and this is the
 * only place that can name the same panel on the way back out. They cannot
 * share one module -- `store.jsx` is an ESM React file bundled into the widget,
 * this is CommonJS in the main process, and importing it here would drag React
 * into the main process.
 *
 * The duplication is paid for on the ONE input where the two sides cannot
 * drift: the Electron `Display` object itself. `store.jsx` documents that shape
 * as an accepted input (DIP `bounds` + `scaleFactor`) and derives the native
 * pixel count as `bounds x scaleFactor` with exactly the rounding repeated
 * below. Deriving the key from a *payload* entry instead would not be safe: by
 * the time an entry reaches a renderer its rectangle has been cut to the
 * overlay frame and divided by the WINDOW's scale, so the same monitor would
 * answer with a different key depending on where the window happened to be --
 * which is precisely the silent, unexplainable reset of an authored setting
 * that the key format exists to prevent.
 *
 * Returns null when the display carries nothing durable. A null key means "no
 * stored opinion, use the 1.0 default"; it is never stored and never guessed.
 *
 * If `store.jsx`'s derivation changes, this changes in the same commit.
 */
function displayTasteKey(d) {
  if (!d || typeof d !== 'object') return null

  // Electron's `Display` carries no EDID string today, but the list is kept in
  // the same order as `store.jsx` so that a platform -- or a future Electron --
  // that does supply one is picked up by both sides at once rather than by one
  // of them, which would split every key in half.
  const ident = [d.edid, d.monitorId, d.deviceId]
    .find((s) => typeof s === 'string' && s.trim() !== '')
  if (ident) return `edid:${ident.trim().replace(/\s+/g, ' ').slice(0, 96)}`

  const rawSf = Number(d.scaleFactor)
  const sf = Number.isFinite(rawSf) && rawSf > 0 ? rawSf : 1

  const box = (d.bounds && typeof d.bounds === 'object') ? d.bounds : d
  const w = Number(box.width) * sf
  const h = Number(box.height) * sf
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return null

  return `panel:${Math.round(w)}x${Math.round(h)}@${Math.round(sf * 100) / 100}`
}

/** Each display with its DIP rect, its device-pixel rect, and the scale between. */
function panelsOf(all) {
  return all.map((d) => {
    const phys = physOf(d.bounds)
    return {
      id: d.id,
      dip: d.bounds,
      phys: phys || d.bounds,
      work: physOf(d.workArea) || d.workArea,
      // 1 when there is no device space of our own to map into: DIP is then
      // already the answer, and pretending otherwise would scale it twice.
      sf: phys ? (d.scaleFactor || 1) : 1,
      // Carried from the panel rather than looked up later, because the
      // Electron `Display` the PPI is derived from is only in scope here.
      ppi: ppiOf(d),
      // Same reason, and it is the stronger case of the two: the taste key can
      // ONLY be derived from an untouched `Display`, and this is the last point
      // at which one is in scope. Null for a panel with no durable identity.
      tasteKey: displayTasteKey(d),
      // The raw identity the key was built from, carried beside it so that a
      // consumer can re-derive and check the join instead of having to trust
      // it, and so a key mismatch is diagnosable from one payload dump.
      dipBounds: d.bounds,
      scaleFactor: Number(d.scaleFactor) > 0 ? Number(d.scaleFactor) : 1,
    }
  })
}

function clamp(v, lo, hi) {
  return v < lo ? lo : v > hi ? hi : v
}

/**
 * A DIP rectangle in device pixels, mapped per monitor.
 *
 * A window spanning two differently-scaled monitors has no single scale, so it
 * is cut against each display and each piece converted with that display's own
 * mapping before being unioned back together. Clamping to the panel's own
 * device rect keeps the shared edges exact: 1707 DIP x 1.5 is 2560.5, and half
 * a pixel of slop at the seam is how a pet ends up falling between screens.
 */
function dipRectToPhysical(rect, panels) {
  let x0 = Infinity
  let y0 = Infinity
  let x1 = -Infinity
  let y1 = -Infinity

  for (const p of panels) {
    const ix0 = Math.max(rect.x, p.dip.x)
    const iy0 = Math.max(rect.y, p.dip.y)
    const ix1 = Math.min(rect.x + rect.width, p.dip.x + p.dip.width)
    const iy1 = Math.min(rect.y + rect.height, p.dip.y + p.dip.height)
    if (ix1 <= ix0 || iy1 <= iy0) continue

    const px = (v) => clamp(p.phys.x + (v - p.dip.x) * p.sf, p.phys.x, p.phys.x + p.phys.width)
    const py = (v) => clamp(p.phys.y + (v - p.dip.y) * p.sf, p.phys.y, p.phys.y + p.phys.height)

    x0 = Math.min(x0, px(ix0))
    y0 = Math.min(y0, py(iy0))
    x1 = Math.max(x1, px(ix1))
    y1 = Math.max(y1, py(iy1))
  }

  if (!Number.isFinite(x0)) {
    // Off every display -- a real state during a hot-unplug, before the
    // `display-removed` resync lands. Returning `rect` untouched would hand
    // back DIPs from a function whose entire contract is device pixels, and
    // every caller divides the result by the window scale a second time, so
    // the payload would be silently double-divided rather than merely stale.
    // Convert with the nearest panel's mapping instead: approximate, but in
    // the unit the callers are owed.
    const p = nearestPanel(rect, panels)
    if (!p) return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
    return {
      x: p.phys.x + (rect.x - p.dip.x) * p.sf,
      y: p.phys.y + (rect.y - p.dip.y) * p.sf,
      width: rect.width * p.sf,
      height: rect.height * p.sf,
    }
  }
  return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 }
}

/** The panel whose DIP rect is nearest a DIP rectangle's centre, or null. */
function nearestPanel(rect, panels) {
  let best = null
  let bestD = Infinity
  const cx = rect.x + rect.width / 2
  const cy = rect.y + rect.height / 2
  for (const p of panels) {
    const dx = cx - (p.dip.x + p.dip.width / 2)
    const dy = cy - (p.dip.y + p.dip.height / 2)
    const d = dx * dx + dy * dy
    if (d < bestD) { bestD = d; best = p }
  }
  return best
}

/**
 * The scale factor the window's CSS px is built from.
 *
 * Windows gives a window the scale of the monitor it overlaps most *in device
 * pixels*, and applies it to the whole window -- including the parts hanging
 * over a monitor of a different scale. That one divisor, not each display's
 * own, is what the renderer's layout is denominated in.
 */
function windowScale(phys, panels) {
  let best = 1
  let bestArea = 0
  for (const p of panels) {
    const w = Math.min(phys.x + phys.width, p.phys.x + p.phys.width) - Math.max(phys.x, p.phys.x)
    const h = Math.min(phys.y + phys.height, p.phys.y + p.phys.height) - Math.max(phys.y, p.phys.y)
    if (w <= 0 || h <= 0) continue
    if (w * h > bestArea) { bestArea = w * h; best = p.sf }
  }
  return best
}

/**
 * The scale factor `setBounds` speaks for a window that spans the desktop.
 *
 * Deliberately not `windowScale`. That one answers "what is this window's CSS
 * pixel", which is the largest-overlap monitor's factor and is right for the
 * payload. This answers a different question: what unit does Windows read a
 * bounds *request* in. For a rectangle touching several monitors, per-monitor-v2
 * awareness gives the window the highest DPI present, so a union expressed in
 * any other scale asks for a rectangle that does not exist. Measured on a
 * 1.10 + 1.65 desktop: devUnion/1.65 covers 100% of it, devUnion/1.10 covers
 * 94.6%, and the piecewise DIP union covers 63%.
 */
function spanScale(all) {
  return Math.max(1, ...all.map((d) => d.scaleFactor || 1))
}

/** The whole desktop in device px -- the one space every monitor shares. */
function deviceUnion(panels) {
  const x = Math.min(...panels.map((p) => p.phys.x))
  const y = Math.min(...panels.map((p) => p.phys.y))
  return {
    x,
    y,
    width: Math.max(...panels.map((p) => p.phys.x + p.phys.width)) - x,
    height: Math.max(...panels.map((p) => p.phys.y + p.phys.height)) - y,
  }
}

/**
 * The rectangle to hand `setBounds` so the window covers the whole desktop.
 *
 * The DIP union is a piecewise quantity -- each monitor divided by its own
 * factor -- and no single window has that shape. Going out through device px
 * and back in through one scale is what makes the request describe a rectangle
 * that can actually exist.
 */
function spanRequest(all, panels) {
  const s = spanScale(all)
  const d = deviceUnion(panels)
  return {
    x: Math.round(d.x / s),
    y: Math.round(d.y / s),
    width: Math.round(d.width / s),
    height: Math.round(d.height / s),
  }
}

/**
 * How close to the desktop counts as covering it, in device px.
 *
 * Windows grants integer DIPs, so a request of devUnion/1.65 comes back
 * rounded: 4481 device px asks for 2716 DIP and is granted 2718, i.e. three
 * device px of overshoot. Demanding exactness here would re-ask forever.
 */
const SPAN_SLOP_PX = 8

/** Does the window's device rect cover the desktop's, within the slop? */
function spansDesktop(win, all, panels) {
  const s = spanScale(all)
  const b = win.getBounds()
  const got = { x: b.x * s, y: b.y * s, width: b.width * s, height: b.height * s }
  const want = deviceUnion(panels)
  return got.x <= want.x + SPAN_SLOP_PX
    && got.y <= want.y + SPAN_SLOP_PX
    && got.x + got.width >= want.x + want.width - SPAN_SLOP_PX
    && got.y + got.height >= want.y + want.height - SPAN_SLOP_PX
}

/**
 * @param {{x:number,y:number,width:number,height:number}} [frameDip]
 *   The rectangle the renderer actually occupies, in DIPs. Defaults to the
 *   live window's own bounds, and -- before the window exists -- to the
 *   rectangle that is about to be asked for, never to a measured one.
 */
function overlayBounds(frameDip) {
  const all = screen.getAllDisplays()
  const panels = panelsOf(all)

  const x = Math.min(...all.map((d) => d.bounds.x))
  const y = Math.min(...all.map((d) => d.bounds.y))
  const right = Math.max(...all.map((d) => d.bounds.x + d.bounds.width))
  const bottom = Math.max(...all.map((d) => d.bounds.y + d.bounds.height))
  const union = { x, y, width: right - x, height: bottom - y }

  // Before the window exists there is no measured frame, so the stand-in is
  // the rectangle `setBounds` is about to be handed. Not the DIP union: that
  // is a piecewise quantity, and the block below reads the frame as a
  // single-scale one, so substituting it describes a desktop 1.5x too wide and
  // reports a drift that belongs to the stand-in rather than to the window.
  const live = overlay && !overlay.isDestroyed() ? overlay.getBounds() : null
  const frame = frameDip || live || spanRequest(all, panels)

  // Which monitor's scale this window's CSS pixel is denominated in is decided
  // by device-pixel overlap, so the piecewise walk is still how *that* question
  // is answered -- but only that question. Here it is a probe, not the frame.
  const sf = windowScale(dipRectToPhysical(frame, panels), panels)

  // The frame itself is not a piecewise quantity. A window has one DIP rect and
  // one scale; converting it monitor by monitor describes a shape no window
  // has. Checked against the page's own `innerWidth` in four different window
  // states: single-scale agreed every time, the piecewise walk in none. On the
  // 94.6%-clamped frame the piecewise result reported a world of 2327.6 CSS px
  // for a page that really measured 3299 -- a 971 px lie, inside which the
  // renderer then refused to place pets that were perfectly paintable.
  const px = {
    x: frame.x * sf,
    y: frame.y * sf,
    width: frame.width * sf,
    height: frame.height * sf,
  }
  const css = (v) => v / sf

  const primaryId = screen.getPrimaryDisplay().id

  // Each display as the renderer sees it: cut to the frame first, because the
  // renderer can only draw on, and can only receive a click on, the part of a
  // screen the window actually covers. Emitting a panel's full rectangle made
  // the payload describe a desktop larger than the window -- pets walked to
  // coordinates outside the viewport, where they are neither painted nor
  // hit-testable, and the span invariant below fired on every clamped frame.
  // A panel the window does not reach at all is dropped rather than reported
  // empty: a zero-area screen is one the engine would still try to stand a pet
  // on.
  const displays = []
  for (const p of panels) {
    const x0 = Math.max(p.phys.x, px.x)
    const y0 = Math.max(p.phys.y, px.y)
    const x1 = Math.min(p.phys.x + p.phys.width, px.x + px.width)
    const y1 = Math.min(p.phys.y + p.phys.height, px.y + px.height)
    if (x1 <= x0 || y1 <= y0) continue

    displays.push({
      id: p.id,
      x: css(x0 - px.x),
      y: css(y0 - px.y),
      width: css(x1 - x0),
      height: css(y1 - y0),
      // Where a pet stands: the bottom of this screen's own work area, so it
      // rests on the taskbar's top edge rather than over the clock. Clamped
      // into the visible strip for the same reason the rect is: a floor the
      // window does not cover would settle a pet somewhere it cannot be seen.
      floor: css(clamp(p.work.y + p.work.height, y0, y1) - px.y),
      // The two per-display terms the renderer needs to size a pet in
      // *physical* units. One window has one CSS pixel, so a sprite of a fixed
      // CSS height is a different real-world size on each panel; `ppi` lets
      // the renderer undo that exactly, and `sf` is the fallback that recovers
      // most of it when no EDID is available. 0 means unknown, never 'no
      // correction' -- the renderer decides which term to use.
      sf: p.sf,
      ppi: p.ppi,
      // The join for the per-display TASTE multiplier `K(d)`. The main process
      // cannot resolve `K` itself -- the value lives in the widget's own
      // persisted store, which this process never reads -- so what it emits is
      // the *identity* of the panel, and the overlay looks the number up in the
      // `taste` map the widget already sends it.
      //
      // Emitting the key rather than the value is the only ordering that works.
      // The two payloads are independent: `bounds` is pushed when the desktop
      // changes shape and `pets` when the roster or the tuning changes, in
      // either order and with no handshake between them. A value baked in here
      // would be a snapshot of a store this process learns about only by
      // accident, and would go stale the moment the user moved the slider
      // without touching the desktop. A key is immutable for the life of the
      // panel, so a stale one cannot exist.
      //
      // Null when the panel has no durable identity; the overlay reads that as
      // the 1.0 default and never as a lookup miss to be reported.
      tasteKey: p.tasteKey,
      // The untouched DIP geometry this panel's key was derived from. NOT for
      // layout -- every other field here is in the renderer's CSS px and these
      // two are not -- but so that the key can be re-derived and verified on
      // the far side, and so a future consumer that needs the panel's real
      // identity is not tempted to rebuild one from the cut-down rectangle
      // above, which describes the window rather than the monitor.
      dipBounds: p.dipBounds,
      scaleFactor: p.scaleFactor,
    })
  }

  const worldWidth = css(px.width)
  const worldHeight = css(px.height)

  // The one-frame stand-in floor, before `displays` arrives and each screen
  // answers for itself. It is a fraction of the *world*, so it has to be
  // derived in the world's own rectangle: the old form took a fraction of the
  // primary display's height and the renderer applied it to the union's, which
  // on a desktop whose screens differ in height put the stand-in floor well
  // below the shorter monitor entirely. Fall back to the primary's own
  // proportion only when the window does not cover the primary at all, where
  // there is no world-relative answer to give.
  const primaryEntry = displays.find((d) => d.id === primaryId)
  const primary = screen.getPrimaryDisplay()
  const workBottom = primaryEntry && worldHeight > 0
    ? primaryEntry.floor / worldHeight
    : (primary.workArea.y - primary.bounds.y + primary.workArea.height) / primary.bounds.height

  // The invariant that would have caught the DIP/CSS mix-up on its first
  // mixed-DPI launch: the rightmost display edge *is* the right edge of the
  // world, because they are now the same rectangle in the same unit. A drift
  // here means the window is clamped to less than the desktop, or that a
  // display was converted with the wrong scale -- either way the renderer is
  // about to hit-test pets against screens that are not where it thinks.
  // The tolerance is one grant's worth of DIP rounding, not zero. Windows
  // answers a request of `deviceUnion / spanScale` with an integer DIP
  // rectangle, so the frame lands a few device px wider than the desktop and
  // the displays -- clipped to the real screens -- cannot reach the end of it.
  // Measured: 1.9 CSS px of benign overshoot at full span. A genuine
  // conversion error is hundreds of px (582.9 measured), so widening the band
  // to the same slop the span check uses, expressed in the renderer's unit,
  // costs no sensitivity and stops the line firing on a healthy window.
  const span = displays.length ? Math.max(...displays.map((d) => d.x + d.width)) : 0
  if (Math.abs(span - worldWidth) > css(SPAN_SLOP_PX)) {
    console.error(
      `[pets] display span ${span.toFixed(1)} != world width ${worldWidth.toFixed(1)} `
      + `(scale ${sf}, frame ${frame.width}x${frame.height} DIP)`,
    )
  }

  // The check above can no longer see a clamped window, and its own comment
  // used to claim it could. Since the display entries are cut to the frame,
  // `span` and `worldWidth` are both frame-derived and agree by construction:
  // measured 0.0 drift at 94.6%, 63.3% and 63.5% desktop coverage alike. The
  // question that actually matters -- does the window reach every screen -- has
  // to be asked against the desktop, and in device px, because that is the one
  // space where monitors tile exactly. The slop absorbs DIP rounding, which
  // makes a granted rectangle a few device px larger than the one asked for.
  // Each term is "how far this edge falls inside the desktop", so every one of
  // them is positive only when the window genuinely fails to reach a screen.
  // The near edges therefore subtract the desktop from the window and the far
  // edges do the opposite: getting that pair the same way round reports a
  // window *larger* than the desktop as being short by the size of its own
  // overhang, which is the exact opposite of the defect being watched for.
  const devWanted = deviceUnion(panels)
  const shortfall = Math.max(
    px.x - devWanted.x,
    px.y - devWanted.y,
    (devWanted.x + devWanted.width) - (px.x + px.width),
    (devWanted.y + devWanted.height) - (px.y + px.height),
  )
  if (shortfall > SPAN_SLOP_PX) {
    console.error(
      `[pets] overlay covers ${px.width.toFixed(0)}x${px.height.toFixed(0)} of a `
      + `${devWanted.width.toFixed(0)}x${devWanted.height.toFixed(0)} device desktop `
      + `(short by ${shortfall.toFixed(0)} px, scale ${sf}, `
      + `frame ${frame.width}x${frame.height} DIP) `
      + '-- pets outside it are invisible and unclickable',
    )
  }

  return {
    // The frame, in DIPs, which is what BrowserWindow.setBounds speaks.
    x: frame.x,
    y: frame.y,
    width: frame.width,
    height: frame.height,
    // The whole desktop, also in DIPs: the rectangle to *ask* for, kept apart
    // from the one the window turned out to have.
    union,
    // The window's own scale, and its size in *device* pixels -- not CSS px.
    // Named apart from the block below because the two differ by exactly that
    // scale (1.65x here), which is a trap worth one extra line of comment: a
    // reader who takes `deviceWidth` for a CSS measurement is out by two
    // thirds of a screen and nothing complains.
    scaleFactor: sf,
    deviceWidth: px.width,
    deviceHeight: px.height,
    // Everything below is in the renderer's own CSS px.
    worldWidth,
    worldHeight,
    world: { width: worldWidth, height: worldHeight },
    workBottom: Math.min(1, Math.max(0.5, workBottom)),
    displays,
  }
}

/**
 * Turn the overlay solid, or let the mouse straight through it.
 *
 * `forward: true` is the whole trick: mousemove keeps flowing to the renderer
 * while clicks fall through to the desktop, which is what lets the page
 * hit-test its own pets and ask for the window to be made solid only once the
 * cursor is genuinely over one. Without it, a full-screen always-on-top window
 * would swallow every click on everything behind it.
 */
function setInteractive(value) {
  if (!overlay || overlay.isDestroyed()) return
  const ignore = !value
  if (ignore === ignoring) return
  ignoring = ignore
  overlay.setIgnoreMouseEvents(ignore, { forward: true })
}

function createOverlay() {
  const b = overlayBounds()

  overlay = new BrowserWindow({
    x: b.union.x,
    y: b.union.y,
    width: b.union.width,
    height: b.union.height,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    hasShadow: false,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    acceptFirstMouse: true,
    title: 'Net Watch pets',
    webPreferences: {
      preload: path.join(__dirname, 'overlay-preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // A pet that freezes the moment another window takes focus is not a pet.
      backgroundThrottling: false,
    },
  })

  // `resizable:false` does not merely forbid a drag-resize: Electron installs
  // the size Windows granted *at creation* as both the minimum and the maximum,
  // and Windows grants a clamped, one-monitor size to a window asking to span
  // the desktop. Measured: min == max == 1555x929 for a 3298x1137 request. The
  // ceiling is the clamp itself, so every later `setBounds` -- including the
  // `ready-to-show` re-apply that exists to defeat exactly this -- is bounded by
  // the thing it is trying to undo. Nothing here makes the window
  // user-resizable: it has no frame and no resize handles.
  overlay.setResizable(true)
  overlay.setMinimumSize(1, 1)
  overlay.setMaximumSize(0, 0)

  // The same level the widget uses: above a maximised window, which for a
  // desktop pet is the difference between being there and being a rumour.
  overlay.setAlwaysOnTop(true, 'screen-saver')
  overlay.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: false })
  ignoring = null
  setInteractive(false)

  overlay.loadFile(path.join(__dirname, '..', 'dist', 'overlay.html'))
  // `showInactive`, not `show`: letting the pets steal focus would pull it off
  // whatever the person is actually typing into.
  overlay.once('ready-to-show', () => {
    overlay.showInactive()
    // Windows clamps a window to one monitor's work area *as it is created*,
    // so the rectangle asked for above never fully arrives: measured here, a
    // request for the full 1440-tall display came back 1392 -- the taskbar
    // strip shaved off -- and on a multi-monitor desktop every screen past the
    // primary one would go the same way, which is the whole of what kept pets
    // on the main display. Re-applying bounds once the window exists is not
    // subject to that clamp -- but only now that the creation clamp is no
    // longer frozen into a max size above, and only if the rectangle is
    // expressed in the unit `setBounds` reads. The DIP union is not that unit;
    // re-applying it here is what made this workaround "not stick".
    const all = screen.getAllDisplays()
    overlay.setBounds(spanRequest(all, panelsOf(all)))
  })
  overlay.webContents.on('did-finish-load', () => {
    if (pending) overlay.webContents.send('pets', pending)
  })
  // See the note on the widget's own console-message handler: Electron 37
  // changed this to a single event object, so the positional read printed
  // `undefined` for every line the overlay logged.
  overlay.webContents.on('console-message', (e, _lvl, msg) =>
    console.error('[overlay]', msg ?? e?.message ?? e))
  overlay.on('move', onOverlayMove)
  overlay.on('closed', () => {
    clearTimeout(moveIdle)
    moveIdle = null
    overlay = null
    ignoring = null
    spanTries = 0
  })
  watchDisplays()
}

/**
 * Re-fit the overlay after it has moved.
 *
 * This window is never dragged -- it is the whole desktop and `movable` is
 * false -- but it still moves: a display layout change shifts it, and Windows
 * can re-associate it with a monitor of a different scale factor, which
 * changes every CSS px the renderer measures in. Nothing announces that. None
 * of the `display-*` events fire for it, which is why crossing monitors left
 * the page drawing against stale geometry.
 *
 * Debounced because one layout change arrives as a burst of moves, and a full
 * re-push per move is a storm of IPC into a window that never throttles.
 */
let moveIdle = null

function onOverlayMove() {
  clearTimeout(moveIdle)
  moveIdle = setTimeout(() => {
    moveIdle = null
    resyncOverlay()
  }, 140)
}

/**
 * Consecutive failed attempts to span the desktop, and the ceiling on them.
 *
 * The old guard compared the next request against the last request, so once
 * the OS had answered a union with a clamped rectangle the condition was false
 * forever and the clamp was latched permanently. That was the deliberate cure
 * for a re-ask storm -- this function is on the receiving end of the `move`
 * that its own `setBounds` emits, measured at ~6.6 `setBounds` and ~6.4
 * `bounds` pushes per second with nobody touching anything -- but it cured it
 * by giving up, and it gave up on the one state that matters.
 *
 * Worse, the re-ask it did issue was destructive: it re-asked with the
 * piecewise DIP union, a rectangle no window can have, so a window that had
 * just been granted the whole desktop was shrunk back by this very function
 * one debounce interval later. Measured: 100% coverage at t=120ms, 63% at
 * t=160ms.
 *
 * Counting attempts instead keeps the storm bounded while never accepting a
 * clamped window in silence: the window is asked only while it is not already
 * spanning, a genuine refusal is reported once and loudly, and the counter is
 * re-armed the moment the display topology changes.
 */
let spanTries = 0
const SPAN_MAX_TRIES = 4

/**
 * Put the window back over the whole desktop and tell the page what it got.
 *
 * The rectangle asked for and the rectangle granted are not the same thing --
 * Windows clamps -- so the payload is rebuilt from `getBounds()` after the
 * fact. Sending the request instead would tell the renderer a size its window
 * does not have, and every pet position derived from it would be wrong by the
 * difference.
 */
function resyncOverlay() {
  if (!overlay || overlay.isDestroyed()) return

  const all = screen.getAllDisplays()
  const panels = panelsOf(all)

  // Asked only while it is not already true. Re-issuing a rectangle the window
  // already has is what produced the storm, and re-issuing one in the wrong
  // unit is what took the desktop away again.
  if (!spansDesktop(overlay, all, panels)) {
    if (spanTries < SPAN_MAX_TRIES) {
      spanTries += 1
      overlay.setBounds(spanRequest(all, panels))
      if (!spansDesktop(overlay, all, panels) && spanTries === SPAN_MAX_TRIES) {
        // Reported, never silently accepted. A clamped overlay means pets on
        // the uncovered monitors are painted off-window and cannot be clicked
        // at all, which is entirely invisible from inside the page.
        const b = overlay.getBounds()
        const d = deviceUnion(panels)
        console.error(
          `[pets] overlay could not span the desktop after ${spanTries} attempts: `
          + `have ${b.width}x${b.height} at ${b.x},${b.y} DIP, want `
          + `${d.width}x${d.height} at ${d.x},${d.y} device px `
          + '-- pets outside it are invisible and unclickable',
        )
      }
    }
  } else {
    spanTries = 0
  }

  overlay.webContents.send('bounds', overlayBounds(overlay.getBounds()))
}

/**
 * Re-fit the overlay when the displays change.
 *
 * Attached once for the lifetime of the process, not once per window. The
 * overlay is created and destroyed as pets come and go, and registering these
 * inside `createOverlay` left a dead listener behind on every close -- each one
 * still firing, each one reaching for whatever the module-level `overlay` had
 * become by then.
 */
let watchingDisplays = false

function watchDisplays() {
  if (watchingDisplays) return
  watchingDisplays = true
  // A new desktop shape is a new question, so the refusal counter starts over:
  // a refusal on the previous layout says nothing about this one.
  const rearm = () => { spanTries = 0; resyncOverlay() }
  screen.on('display-metrics-changed', rearm)
  screen.on('display-added', rearm)
  screen.on('display-removed', rearm)
}

function closeOverlay() {
  if (overlay && !overlay.isDestroyed()) overlay.destroy()
  overlay = null
  ignoring = null
  // A new window gets its own answer from the OS; carrying the old refusal
  // count over would spend the next overlay's attempts before it has made any.
  spanTries = 0
}

/**
 * Hand the overlay a new roster, opening or closing the window to match.
 *
 * The window's existence follows the roster rather than a setting. An empty
 * desktop costs nothing, and a full-screen always-on-top window that exists for
 * no reason is exactly the sort of thing that turns up in a bug report about
 * something else entirely.
 */
function syncOverlay(state) {
  pending = state
  const wanted = Array.isArray(state?.pets) && state.pets.length > 0

  if (!wanted) {
    if (overlay) console.error('[pets] overlay closed')
    closeOverlay()
    return
  }
  if (!overlay || overlay.isDestroyed()) {
    console.error(`[pets] overlay opened for ${state.pets.length} pet(s)`)
    createOverlay()
    return                       // did-finish-load replays `pending`
  }
  overlay.webContents.send('pets', state)
}

function overlayWindow() {
  return overlay && !overlay.isDestroyed() ? overlay : null
}

module.exports = {
  ASSET_ROOT,
  // Exported for the parity test against `store.jsx`'s copy. The two
  // derivations are the join between the widget's store and this process, and
  // an untested duplicate is one edit away from silently splitting in half.
  displayTasteKey,
  getManifest,
  overlayBounds,
  overlayWindow,
  setInteractive,
  syncOverlay,
  closeOverlay,
}
