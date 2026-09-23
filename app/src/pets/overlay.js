import './pets.css'
import { ASSET_BASE, OVERLAY_SIZE_FACTOR, World, bindPointer } from './engine.js'

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

/**
 * The per-display TASTE multipliers, `K(d)`, as last sent by the widget.
 *
 * Held here rather than pushed straight into the engine because `K` is not a
 * property of the roster, which is what the `pets` payload otherwise is: it is
 * a property of a SCREEN, and the engine reads it off a display entry. The two
 * facts it needs therefore arrive down two independent channels -- the panel
 * identities on `bounds`, the numbers on `pets` -- in either order, with no
 * handshake, and each re-sent on its own schedule. Whichever lands second has
 * to be able to join itself onto the one already in hand, so both are kept.
 *
 * `{}` and "no opinion" are the same state on purpose: an absent entry is 1.0,
 * which is exactly what an untouched screen means. There is no separate
 * "not loaded yet" value, because there is nothing a caller could usefully do
 * with one -- a pet still has to be drawn at some size in that frame, and the
 * only defensible size is the uncorrected one.
 */
let taste = {}

/**
 * `K` for one display entry, from the map the widget sent.
 *
 * The join is on `tasteKey`, which the main process stamps onto every entry
 * from the untouched Electron `Display`. This file deliberately does NOT derive
 * a key of its own: by the time an entry gets here its rectangle has been cut
 * to the overlay frame and divided by this window's scale factor, so a key
 * built from it would describe the window rather than the panel and would move
 * whenever the window did. One derivation, in the one process that still has
 * the original display object, is the only arrangement in which the widget's
 * stored key and the overlay's lookup cannot drift apart.
 *
 * Everything unusable returns `undefined` rather than 1: an entry with no `k`
 * field at all is what the engine already treats as "no opinion", so the
 * absent case stays a single code path there instead of two that must agree.
 * That also keeps this forward-compatible with a main process too old to send
 * `tasteKey`, which simply gets the documented default on every screen.
 */
function tasteFor(d) {
  const key = d && typeof d.tasteKey === 'string' ? d.tasteKey : ''
  if (!key) return undefined
  const k = Number(taste[key])
  // Clamping is the engine's job, and deliberately left there: it owns the band
  // and would have to re-check anyway, so a second clamp here could only ever
  // disagree with it. This rejects the values that are not numbers at all.
  return Number.isFinite(k) && k > 0 ? k : undefined
}

/**
 * The display list with each screen's `K` joined on.
 *
 * A shallow copy per entry, never a rebuilt literal: the engine and the pet
 * spawn path both read fields this file has no business knowing about -- `sf`,
 * `ppi`, `floor`, `id` today, and whatever is added next -- and the last bug in
 * this area was a rebuilt object dropping every per-display field except the
 * floor. Spreading carries unknown fields through untouched and keeps per-entry
 * rejection the engine's decision, which is what the `setDisplays` call below
 * has always relied on.
 *
 * `k` is omitted rather than set to 1 when there is no opinion, so that a
 * payload dump distinguishes "the user set this screen to 1.0" from "this
 * screen was never touched". They behave identically; they are not the same
 * thing, and only one of them survives a change to the default.
 */
function withTaste(list) {
  return list.map((d) => {
    const k = tasteFor(d)
    return k === undefined ? { ...d } : { ...d, k }
  })
}

/**
 * Whether a display entry can be used at all.
 *
 * Deliberately limited to the geometry the renderer cannot do without. It must
 * never be tightened to require the per-display DPI fields (`sf`, `ppi`),
 * because this predicate gates *installation* rather than filtering fields: a
 * payload in which no entry satisfies it is not installed at all, and since
 * a map is only ever installed and never cleared that is a silent no-op which
 * leaves the previous, now-wrong map in place. A payload from a main process
 * that does not send the DPI fields must still install, and the engine must
 * fall back to a neutral multiplier for it.
 */
function usable(d) {
  return d && Number.isFinite(d.x) && Number.isFinite(d.y)
    && d.width > 0 && d.height > 0 && Number.isFinite(d.floor)
}

/**
 * The size of the world, in the same space the display rectangles are in.
 *
 * Taken from the union of the screens rather than from `window.innerWidth`,
 * which is the bug this replaces: the displays arrive in the overlay window's
 * CSS-pixel space and `innerWidth` is that window's own size, and on a desktop
 * whose monitors have different scale factors those are two different
 * coordinate systems. Measuring the world from the very array the engine
 * hit-tests against is the only way the two cannot disagree -- and no single
 * corrective constant exists that would let them be reconciled afterwards.
 *
 * `width`/`height` on the payload are the second choice and the window's own
 * size the last, both for the frames before any payload has arrived.
 */
function worldBox(b) {
  const ds = Array.isArray(b?.displays) ? b.displays.filter(usable) : []
  if (ds.length) {
    let left = 0
    let top = 0
    let right = 0
    let bottom = 0
    for (const d of ds) {
      left = Math.min(left, d.x)
      top = Math.min(top, d.y)
      right = Math.max(right, d.x + d.width)
      bottom = Math.max(bottom, d.y + d.height)
    }
    return { w: right - left, h: bottom - top }
  }
  // `worldWidth`/`worldHeight` are the frame already divided by the window's
  // own scale factor; `width`/`height` are DIPs, which is a different unit on
  // a mixed-DPI desktop and must never be used as a world size.
  if (b && b.worldWidth > 0 && b.worldHeight > 0) return { w: b.worldWidth, h: b.worldHeight }
  return { w: window.innerWidth, h: window.innerHeight }
}

function fit(b) {
  if (b) bounds = b
  const cur = b ?? bounds
  // `ground` keeps a resting pet above the taskbar. The window itself covers
  // the whole display, so a pet can still walk in front of it mid-hop -- which
  // is where a desktop pet belongs -- it simply does not choose to stand there.
  // It is the fallback: once `displays` arrives each screen supplies its own
  // floor, which is the only way a pet can rest correctly on a second monitor
  // whose taskbar is somewhere else entirely.
  world.ground = cur?.workBottom ?? 1
  // The window has exactly one CSS-to-device factor, and every display
  // rectangle in this payload has already been divided by it. A per-display
  // size correction is a ratio against *that* number, not against 1, so the
  // engine cannot compute one without it. Pushed before the map so a recompute
  // triggered by the map already sees the matching denominator rather than the
  // previous window's. Guarded by a capability check so this file stays usable
  // against an engine that has no such setter.
  const winScale = Number(cur?.scaleFactor)
  if (Number.isFinite(winScale) && winScale > 0 && typeof world.setWindowScale === 'function') {
    world.setWindowScale(winScale)
  }
  // Only ever *install* a display map, never clear one. A first frame with no
  // payload, or a resize that arrives before the new payload does, used to
  // hand `undefined` straight through to `setDisplays` and wipe a map that was
  // already correct -- after which every pet is placed against a scalar
  // fallback and lands outside every screen, which is invisible.
  //
  // `some(usable)` gates the install; every entry handed over is a shallow copy
  // of the raw one, so per-entry rejection stays the engine's job and no field
  // this file does not know about can be dropped in passing. The only field
  // added is `k`, the per-display taste multiplier, joined on from the map the
  // widget sent -- see `withTaste`.
  if (Array.isArray(cur?.displays) && cur.displays.some(usable)) {
    world.setDisplays(withTaste(cur.displays))
  }
  const box = worldBox(cur)
  world.setSize(box.w, box.h)
}

// A failed or missing fetch must never latch: until a payload lands the world
// is sized from the window, which is right only while there is one monitor, so
// the overlay keeps asking rather than living with it.
let retry = 0

function requestBounds() {
  window.nwPets.bounds().then((b) => {
    if (b) { retry = 0; fit(b) } else scheduleRetry()
  }).catch(scheduleRetry)
}

function scheduleRetry() {
  if (world.displays.length) return
  retry = Math.min(retry ? retry * 2 : 250, 4000)
  setTimeout(requestBounds, retry)
}

// Sized from the window first and corrected from the display a tick later. The
// two differ only in where the taskbar starts, and waiting on IPC for that
// would mean a frame with no world at all to put an arriving pet into.
fit(null)
world.start()
requestBounds()

// A resize means the main process has just moved the overlay onto a different
// desktop geometry, so the payload in hand describes the *previous* one. Re-
// fitting to it would pin the world to a desktop that no longer exists; the
// only honest answer is to ask for the new one. Until it lands the world keeps
// its current box, because guessing from `innerWidth` here is exactly the unit
// mix this file is getting rid of -- except before any payload has ever
// arrived, when the window's own size is all there is.
let resizeAsk = 0

window.addEventListener('resize', () => {
  if (!bounds) { fit(null); return }
  clearTimeout(resizeAsk)
  resizeAsk = setTimeout(requestBounds, 100)
})

window.nwPets.onBounds(fit)

// ── click-through ─────────────────────────────────────────────────────────────

let interactive = null
let pendingOff = 0

/**
 * Ask for the window to be made solid, or let it go click-through again.
 *
 * Turning solid is immediate -- a pet that cannot be grabbed the instant the
 * cursor touches it is a pet that feels broken. Letting go is delayed, and
 * that asymmetry is the point: each call that gets through ends in a
 * cross-process SetWindowLong on an always-on-top, unthrottled, full-desktop
 * window, so a hit test that oscillates on the boundary of a sprite would fire
 * one per mousemove. The hold turns that flapping into a single transition
 * once the cursor has genuinely left.
 */
function sync(want) {
  if (want) {
    clearTimeout(pendingOff)
    pendingOff = 0
    if (interactive === true) return
    interactive = true
    window.nwPets.setInteractive(true)
    return
  }
  if (interactive === false || pendingOff) return
  pendingOff = setTimeout(() => {
    pendingOff = 0
    interactive = false
    window.nwPets.setInteractive(false)
  }, 120)
}

let held = false
let hitX = NaN
let hitY = NaN

window.addEventListener('mousemove', (e) => {
  world.setCursor(e.clientX, e.clientY, true)
  // A drag has to keep the window solid even when the pointer runs ahead of the
  // sprite, which during a fast throw it always does.
  if (held) { hitX = NaN; sync(true); return }
  // `petAt` walks every pet and builds a rectangle for each, and mousemove
  // fires far faster than a cursor can cross a sprite, so sub-pixel jitter is
  // re-answered rather than re-asked.
  if (Math.abs(e.clientX - hitX) < 2 && Math.abs(e.clientY - hitY) < 2) return
  hitX = e.clientX
  hitY = e.clientY
  sync(Boolean(world.petAt(e.clientX, e.clientY)))
})

window.addEventListener('mouseleave', () => {
  world.cursor.inside = false
  hitX = NaN
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

/**
 * The screen a pet should walk on from: the one the cursor is on if the
 * pointer has been over the overlay, otherwise the first one reported.
 *
 * The cursor is the best available stand-in for "the monitor the person is
 * looking at", which is where a pet they have just let loose should appear.
 */
function entryScreen() {
  // Returns the display entry itself, never a copy: callers need the fields
  // this file does not read, and a rebuilt object literal drops them.
  const ds = world.displays
  if (!ds.length) return null
  const c = world.cursor
  return (Number.isFinite(c.x) && c.x > -9999 && world.displayAt(c.x, c.y)) || ds[0]
}

// ── the roster ────────────────────────────────────────────────────────────────

/**
 * Take a new taste map and re-size every pet if it actually changed.
 *
 * Re-installing the display map is what makes the change visible: `K` is read
 * off a display entry, so the numbers only reach a pet by way of one. That path
 * is already the right one to reuse rather than to shortcut -- `setDisplays`
 * re-derives each pet's screen and calls `applyDisplaySize` on all of them,
 * including the platform-bound ones, which is exactly the work a changed
 * multiplier needs doing.
 *
 * Guarded by an equality check because that path is not free and this handler
 * fires on every roster change, every rename and every slider drag: an
 * unguarded re-install would drop each pet's cached screen and re-clamp the
 * whole roster many times a second while the Size slider is moving.
 *
 * Compared by serialising rather than by reference, because the map is rebuilt
 * on the widget's side and crosses IPC, so it is a new object every time and
 * never reference-equal even when nothing in it moved. Key order is stable for
 * the same reason it can be: both sides build it from the same insertion
 * sequence, and a false miss costs one redundant re-install rather than a wrong
 * size, so the cheap comparison is the correct trade here.
 */
let tasteJson = '{}'

function setTaste(next) {
  const map = next && typeof next === 'object' && !Array.isArray(next) ? next : {}
  const json = JSON.stringify(map)
  if (json === tasteJson) return
  tasteJson = json
  taste = map
  // Only re-install a map that exists. Before the first `bounds` payload there
  // is nothing to join onto, and handing `setDisplays` an empty list here would
  // clear a map the rest of this file is careful never to clear -- after which
  // every pet is placed against a scalar fallback and lands off-screen.
  if (bounds && Array.isArray(bounds.displays) && bounds.displays.some(usable)) {
    world.setDisplays(withTaste(bounds.displays))
  }
}

window.nwPets.onState((state) => {
  if (!state) return
  // Before the roster, so that a pet added in this very payload is sized for
  // its screen in its first frame rather than a frame later. `world.add` reads
  // the display entry it is handed, and `entryScreen()` reads it out of the map
  // this call may have just replaced.
  setTaste(state.taste)
  if (state.opts) {
    // See OVERLAY_SIZE_FACTOR: a loose pet is drawn larger than a card-bound
    // one, because out here it has nothing to be in scale with.
    world.setOpts({
      ...state.opts,
      size: Math.round(state.opts.size * OVERLAY_SIZE_FACTOR),
    })
  }

  const keep = new Set()
  for (const row of state.pets ?? []) {
    keep.add(row.id)
    const live = world.byId(row.id)
    if (live) { live.setSizeFactor(row.size); continue }
    // A new arrival walks on from the edge of a real screen, rather than the
    // edge of the desktop's bounding box. Those are not the same rectangle:
    // between two monitors of different heights the box contains a strip that
    // belongs to no display, and `world.w - 24` is both the far side of the
    // *other* monitor and, often, inside that strip -- where nothing is drawn
    // and the pet is simply never seen.
    const screen = entryScreen()
    const fromLeft = Math.random() < 0.5
    const x = screen ? (fromLeft ? screen.x + 24 : screen.x + screen.width - 24)
      : (fromLeft ? 24 : world.w - 24)
    // The floor of the screen it is walking onto, not a fraction of the whole
    // desktop: across two monitors that fraction is a line through the middle
    // of nothing in particular.
    // `screen` itself, not `{ floor: screen.floor }`: rebuilding the entry here
    // stripped every per-display field except the floor, so a pet spawned on a
    // second monitor started life sized for the first one.
    const { floor } = screen ?? world.screenSpan(x, world.h)
    world.add({
      id: row.id,
      species: row.species,
      variant: row.variant,
      mode: 'free',
      size: row.size,
      // The entry itself, so a pet can be sized for the screen it appears on
      // in its first frame rather than a frame later, once something else has
      // resolved its position back to a display.
      display: screen ?? null,
      x,
      y: floor - 8,
      dir: fromLeft ? 1 : -1,
    })
  }
  for (const live of [...world.pets]) if (!keep.has(live.id)) world.remove(live.id)
})
