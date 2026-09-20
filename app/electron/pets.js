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

let manifest = null

/** The species list, built on the first ask and then kept. */
function getManifest() {
  if (!manifest) {
    manifest = buildManifest()
    console.error(`[pets] ${manifest.length} species`)
  }
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
 * has only its own window to go on.
 */
function overlayBounds() {
  const all = screen.getAllDisplays()
  const x = Math.min(...all.map((d) => d.bounds.x))
  const y = Math.min(...all.map((d) => d.bounds.y))
  const right = Math.max(...all.map((d) => d.bounds.x + d.bounds.width))
  const bottom = Math.max(...all.map((d) => d.bounds.y + d.bounds.height))

  const primary = screen.getPrimaryDisplay()
  const workBottom =
    (primary.workArea.y - primary.bounds.y + primary.workArea.height) / primary.bounds.height

  return {
    x,
    y,
    width: right - x,
    height: bottom - y,
    workBottom: Math.min(1, Math.max(0.5, workBottom)),
    displays: all.map((d) => ({
      id: d.id,
      x: d.bounds.x - x,
      y: d.bounds.y - y,
      width: d.bounds.width,
      height: d.bounds.height,
      // Where a pet stands: the bottom of this screen's work area, so it rests
      // on the taskbar's top edge rather than over the clock.
      floor: d.workArea.y - y + d.workArea.height,
    })),
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
    x: b.x,
    y: b.y,
    width: b.width,
    height: b.height,
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
    // on the main display. Re-applying the same bounds once the window exists
    // is not subject to that clamp, and does stick.
    overlay.setBounds({ x: b.x, y: b.y, width: b.width, height: b.height })
  })
  overlay.webContents.on('did-finish-load', () => {
    if (pending) overlay.webContents.send('pets', pending)
  })
  // See the note on the widget's own console-message handler: Electron 37
  // changed this to a single event object, so the positional read printed
  // `undefined` for every line the overlay logged.
  overlay.webContents.on('console-message', (e, _lvl, msg) =>
    console.error('[overlay]', msg ?? e?.message ?? e))
  overlay.on('closed', () => { overlay = null; ignoring = null })
  watchDisplays()
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
  const resync = () => {
    if (!overlay || overlay.isDestroyed()) return
    const nb = overlayBounds()
    overlay.setBounds({ x: nb.x, y: nb.y, width: nb.width, height: nb.height })
    overlay.webContents.send('bounds', nb)
  }
  screen.on('display-metrics-changed', resync)
  screen.on('display-added', resync)
  screen.on('display-removed', resync)
}

function closeOverlay() {
  if (overlay && !overlay.isDestroyed()) overlay.destroy()
  overlay = null
  ignoring = null
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
  getManifest,
  overlayBounds,
  overlayWindow,
  setInteractive,
  syncOverlay,
  closeOverlay,
}
