import './pets.css'
import { ASSET_BASE, World, bindPointer } from './engine.js'

/**
 * The desktop overlay: the pets that were set loose.
 *
 * A whole-screen transparent window with no platforms in it, so every pet in
 * here is in free-roam mode -- it walks where it likes and hops between heights.
 * There is no UI: the roster arrives from the widget and this draws it.
 *
 * The one thing worth understanding is the click-through. The window covers the
 * entire display and sits above everything, so left as a solid window it would
 * make the desktop unusable. Instead it is click-through by default, and on
 * every pointer move the page hit-tests its own pets and asks the main process
 * to make the window solid only while the cursor is actually over one. Electron
 * keeps forwarding mousemove either way, which is what makes the test possible
 * from inside a window that is not receiving clicks.
 */

const stage = document.getElementById('stage')

const world = new World({
  stage,
  assetBase: ASSET_BASE,
  opts: { size: 44 },
})

let bounds = null

function fit(b) {
  bounds = b
  // `ground` keeps a resting pet above the taskbar. The window itself covers
  // the whole display, so a pet can still walk in front of it mid-hop -- which
  // is where a desktop pet belongs -- it simply does not choose to stand there.
  world.ground = b?.workBottom ?? 1
  world.setSize(window.innerWidth, window.innerHeight)
}

// Sized from the window first and corrected from the display a tick later. The
// two differ only in where the taskbar starts, and waiting on IPC for that
// would mean a frame with no world at all to put an arriving pet into.
fit(null)
world.start()
window.nwPets.bounds().then(fit).catch(() => { /* keep the window's own size */ })

window.addEventListener('resize', () => fit(bounds))
window.nwPets.onBounds(fit)

// ── click-through ─────────────────────────────────────────────────────────────

let interactive = null

function sync(want) {
  if (want === interactive) return
  interactive = want
  window.nwPets.setInteractive(want)
}

let held = false

window.addEventListener('mousemove', (e) => {
  world.setCursor(e.clientX, e.clientY, true)
  // A drag has to keep the window solid even when the pointer runs ahead of the
  // sprite, which during a fast throw it always does.
  sync(held || Boolean(world.petAt(e.clientX, e.clientY)))
})

window.addEventListener('mouseleave', () => {
  world.cursor.inside = false
  sync(false)
})

// ── gestures ──────────────────────────────────────────────────────────────────

bindPointer(world, {
  onGesture: (kind, pet) => {
    held = false
    // Double-click is the way home. The overlay cannot edit the roster -- the
    // widget owns it -- so it reports the pet and the widget moves it.
    if (kind === 'swap') window.nwPets.sendHome(pet.id)
  },
})

stage.addEventListener('pointerdown', () => { held = true })
window.addEventListener('pointerup', () => { held = false })

// ── the roster ────────────────────────────────────────────────────────────────

window.nwPets.onState((state) => {
  if (!state) return
  if (state.opts) {
    // The widget's size slider is tuned for a 26px pet standing on a card. Out
    // here there is a whole screen and nothing to be in scale with, so it is
    // taken as a proportion of a size that reads properly on a desktop rather
    // than as an absolute.
    world.setOpts({ ...state.opts, size: Math.round(state.opts.size * 1.7) })
  }

  const keep = new Set()
  for (const row of state.pets ?? []) {
    keep.add(row.id)
    if (world.byId(row.id)) continue
    // A new arrival walks on from the edge it is nearest, rather than fading in
    // over the middle of somebody's screen.
    const fromLeft = Math.random() < 0.5
    world.add({
      id: row.id,
      species: row.species,
      variant: row.variant,
      mode: 'free',
      x: fromLeft ? 24 : world.w - 24,
      y: world.h * world.ground - 8,
      dir: fromLeft ? 1 : -1,
    })
  }
  for (const live of [...world.pets]) if (!keep.has(live.id)) world.remove(live.id)
})
