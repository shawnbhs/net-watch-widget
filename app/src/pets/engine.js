/**
 * One pet, and the little world it lives in.
 *
 * Ported from PetRail, with its rails replaced by *platforms*, because the two
 * places a pet can live here want different ground under them:
 *
 * - In the widget, a platform is the top edge of a card. The set of them is
 *   whatever the layout currently measures, so a pet walks the Hardware card
 *   and hops onto Timezone, and when the widget switches to compact view the
 *   ground moves under it and it lands on whatever is left.
 * - On the desktop, there are no platforms at all. A loose pet roams: it walks
 *   where it likes and hops between heights, the way PetRail's free-roam pets
 *   did.
 *
 * Nothing in here touches React, the sidecar or Electron. It is given a stage
 * element, a size, a list of platforms and a settings object, and it moves
 * sprites around inside them -- which is why the same file drives both the card
 * column and a full-screen overlay in another process.
 */

/**
 * Where the sprites are, relative to a page in `app/dist/`.
 *
 * Deliberately outside the bundle. They are 4.9MB of GIF that never change and
 * are referenced by names built at runtime from the manifest, so putting them
 * through vite would mean copying every one of them into dist on every build
 * for no benefit whatsoever.
 */
export const ASSET_BASE = '../assets/pets/'

/**
 * How far apart two card tops must be to count as different levels.
 *
 * Two cards in the same row are laid out at the same y, but a pixel of
 * rounding on a zoomed layout can make them differ by one; anything inside
 * this is the same shelf.
 */
const LEVEL_EPS = 2

const GRAVITY = 2600      // px/s^2 while airborne
const AIR_DRAG = 0.86     // horizontal damping per second in the air
const BASE_SPEED = 26     // px/s for a 56px pet at speed 1.0
const NOTICE_R = 170      // how close the cursor gets before a pet reacts

/**
 * Not every species ships every clip, so each state degrades to whatever the
 * sprite set actually has. A snake has no `lie`; a crab has no `jump`.
 */
const CLIP_CHAIN = {
  idle: ['idle', 'stand', 'lie', 'walk'],
  walk: ['walk', 'walk_fast', 'run', 'idle'],
  run: ['run', 'walk_fast', 'walk', 'idle'],
  lie: ['lie', 'idle', 'stand'],
  swipe: ['swipe', 'idle', 'stand'],
  ball: ['with_ball', 'lie', 'idle'],
  air: ['fall_from_grab', 'jump', 'run', 'swipe', 'idle'],
  held: ['swipe', 'jump', 'idle'],
  land: ['land', 'idle'],
}

/** States during which a pet neither starts nor answers a meeting. */
const BUSY = new Set(['held', 'air', 'hop', 'land'])

/**
 * How much bigger a loose pet is drawn than the same pet on a card.
 *
 * The size slider is tuned for a pet standing on a 43px card. Out on the
 * desktop there is nothing for it to be in scale with, so the figure is taken
 * as a proportion of a size that reads properly there instead of as an
 * absolute. It lives here rather than in the overlay because the widget's own
 * roster has to quote the resulting pixels back to the user, and two copies of
 * this number would eventually disagree.
 */
export const OVERLAY_SIZE_FACTOR = 1.7

/**
 * The band a finished sprite height is held inside, in CSS px.
 *
 * The per-display factor below multiplies a number the user already controls
 * with two sliders, so the product can reach either end of absurd: a pet too
 * small to see and too small to click, or one that covers the screen it is
 * standing on. Neither is recoverable by the user, because both are hard to
 * grab. The cap also keeps the pet small enough that the size-derived bounds
 * elsewhere in this file stay ordered -- see the note on `clamp` below.
 */
const PET_MIN_H = 16
const PET_MAX_H = 512

/**
 * Sanity band for the per-display density factor `M`.
 *
 * `M` is a ratio of two real measurements, so a garbled EDID or a scale factor
 * read from a display that has just been unplugged can hand back anything.
 * These are far wider than any real desktop (the measured pair on the machine
 * this was written for is 0.65 and 1.06) and exist only so a nonsense reading
 * cannot reach the size chain at all.
 */
const DISPLAY_SCALE_MIN = 0.25
const DISPLAY_SCALE_MAX = 4

/** Band for the per-display taste multiplier `K`. Default 1, i.e. no opinion. */
const DISPLAY_TASTE_MIN = 0.5
const DISPLAY_TASTE_MAX = 2

/**
 * Returns the MIDPOINT when `lo > hi`, deliberately, and that is load-bearing:
 * a pet wider than the world has no correct edge to sit against and the centre
 * is the only symmetric answer (there is a regression test pinning exactly
 * that). The consequence is that an inverted range never raises -- it silently
 * relocates. Every bound in this file is `x +/- w * k` or `top + h + k`, so
 * GROWING a pet is precisely what inverts them. Call sites that can now be
 * reached with a larger pet than before therefore test `lo > hi` themselves and
 * pick the standable end, rather than letting the midpoint through.
 */
const clamp = (v, lo, hi) => (lo > hi ? (lo + hi) / 2 : v < lo ? lo : v > hi ? hi : v)
/**
 * A pet's own size multiplier, sanitised.
 *
 * Applied on top of the world's size and the species' own scale, so it says
 * "this one is bigger than the others" rather than "this one is 40px" -- the
 * global slider then still moves the whole roster and keeps those differences.
 * A missing value is 1 because every pet saved before this existed has none,
 * and 1 is what they were being drawn at.
 */
const petFactor = (v) => {
  const n = Number(v)
  return Number.isFinite(n) && n > 0 ? clamp(n, 0.1, 8) : 1
}
const rand = (lo, hi) => lo + Math.random() * (hi - lo)
const pick = (arr) => arr[(Math.random() * arr.length) | 0]

let Z = 10

export class Pet {
  /**
   * @param {World} world
   * @param {object} spec `{ id, species, variant, mode, x, y, platformId, dir }`
   */
  constructor(world, spec) {
    this.world = world
    this.id = spec.id
    this.species = spec.species
    this.variant = spec.variant

    /** 'platform' -- bound to a card top. 'free' -- roaming the desktop. */
    this.mode = spec.mode === 'free' ? 'free' : 'platform'

    this.platformId = spec.platformId ?? null
    this.x = spec.x ?? rand(0.2, 0.8) * world.w
    this.y = spec.y ?? 0          // feet, in px
    this.vx = 0
    this.vy = 0
    this.dir = spec.dir ?? (Math.random() < 0.5 ? -1 : 1)

    this.state = 'idle'
    this.timer = rand(0.2, 1.6)
    this.tilt = 0
    this.bob = Math.random() * 6.28

    // A touch of per-pet variation, so two of the same species do not march
    // in lockstep down the same card.
    this.tempo = rand(0.82, 1.22)

    /** This pet's own multiplier on the world size. See `petFactor`. */
    this.sizeFactor = petFactor(spec.size)

    this.k = 1            // px per source pixel, re-derived on every size change
    /**
     * The screen this pet is currently being sized against, and the factors
     * that were in force the last time `layoutClip` ran.
     *
     * Kept so a seam crossing can be detected without asking the DOM, and so
     * the per-frame check can return before writing any style. `display` is
     * dropped whenever the display map is replaced, because an entry from a
     * monitor that has been unplugged must not keep sizing anything.
     */
    this.display = null
    this.dispM = 1
    this.dispK = 1
    this.action = 'idle'
    this.w = 24           // drawn character width  (not the canvas)
    this.h = 24           // drawn character height (not the canvas)
    this.anchorX = 0      // canvas-left -> character centre
    this.anchorY = 0      // canvas-top  -> character feet
    this.clip = null
    this.chasing = false

    this.target = null      // roam destination
    this.hopCooldown = 0    // stops two pets ping-ponging leaps
    this.cameFrom = null    // the card it hopped off, so it does not bounce back
    this.hopRest = 0        // seconds it owes the card it just landed on
    this.danceT = 0         // beat clock, only ticks while dancing
    this.vxNow = 0          // signed speed this frame, for meetings

    this.build()
    this.setClip('idle')
    this.applySize()
    if (this.mode === 'platform') this.snap()
  }

  // ── DOM ─────────────────────────────────────────────────────────────────────

  build() {
    const shadow = document.createElement('div')
    shadow.className = 'pet-shadow'
    this.world.stage.appendChild(shadow)
    this.shadowEl = shadow

    const img = document.createElement('img')
    img.className = 'pet-sprite'
    img.draggable = false
    img.dataset.petId = this.id
    img.alt = ''
    // Only needed for a sprite with no entry in metrics.json -- a folder
    // dropped into assets/pets without gen-metrics.py being re-run. The pet
    // then measures itself from the first frame the browser decodes.
    img.addEventListener('load', () => {
      if (!this.variant.m[this.action] && img.naturalHeight) {
        this.variant.m[this.action] =
          [img.naturalWidth, img.naturalHeight, 0, 0, img.naturalWidth, img.naturalHeight]
        if (!this.variant.baseH) this.variant.baseH = img.naturalHeight
        this.applySize()
      }
    })
    this.world.stage.appendChild(img)
    this.el = img
  }

  /**
   * One scale factor for the whole pet, taken from its reference clip, so that
   * changing animation never resizes the character.
   */
  applySize() {
    // `M` and `K` stay two factors, never one product. `M` is measured -- the
    // density of the panel under the pet, relative to the single CSS grid this
    // window was given -- and is what holds the pet at a constant PHYSICAL size
    // across monitors. `K` is an opinion about a particular screen. Folding
    // them together would make a taste setting indistinguishable from a
    // measurement the next time either has to be debugged.
    const d = this.governingDisplay()
    const M = this.world.displayScale(d)
    const K = this.world.displayTaste(d)
    const target = clamp(
      this.world.petHeight() * this.species.scale * this.sizeFactor * M * K,
      PET_MIN_H, PET_MAX_H,
    )
    this.dispM = M
    this.dispK = K
    this.k = target / (this.variant.baseH || target)
    const wOld = this.w
    const hOld = this.h
    this.layoutClip()
    // A drag is anchored to offsets captured at `pointerdown`, in the pixels of
    // the size the pet had then. `applyDisplaySize` refuses a held pet outright,
    // but the world-level paths -- the size slider, `setScale`, `setOpts` -- do
    // not, and they land here. Left alone the offsets keep pointing at a body
    // that is no longer that size: the grab point slides down towards the feet
    // and, at a screen edge, the narrowed corridor shoves the pet away from a
    // pointer that has not moved.
    if (this.state === 'held') this.regrab(wOld, hOld)
  }

  /**
   * The screen whose density sizes this pet, with a deadband at the seam.
   *
   * Without the deadband this oscillates every frame: `clampToWorld` writes
   * `x` using the pet's own half-width, the new `x` can land back over the
   * previous screen, that re-selects the previous size, which relaxes the
   * clamp, which lets it move back. The pet flickers between two sizes on the
   * boundary and `layoutClip` writes the DOM on every frame of it.
   *
   * So a different screen is only adopted once the pet's centre is properly
   * inside it -- further in than the clamp could ever push it back out. A point
   * over no display at all keeps the last good screen rather than falling back
   * to a default, because the dead rectangle under a short monitor is exactly
   * where a pet has no density to read and the old one is still the honest
   * answer.
   */
  governingDisplay() {
    const d = this.world.displayAt(this.x, this.y)
    if (!d) return this.display
    if (!this.display || d.id === this.display.id) {
      this.display = d
      return d
    }
    const pad = Math.max(8, this.w * 0.5)
    // A screen narrower than the deadband could never be entered at all, and a
    // pet that can never adopt it would wear the wrong size there forever.
    const inside = d.width <= pad * 2
      || (this.x >= d.x + pad && this.x <= d.x + d.width - pad)
    if (!inside) return this.display
    this.display = d
    return d
  }

  /**
   * Re-size this pet for the screen it is standing on. The ONLY place a live
   * pet's size may change as a result of where it is.
   *
   * The order below is the whole of it, and none of the steps commute:
   *
   * 1. Some states own the pet's geometry outright. A drag is anchored to
   *    offsets captured at `pointerdown`, and a hop and a fall are both arcs
   *    frozen at take-off; resizing under any of the three moves the pet out
   *    from under the thing that is steering it. This is the same guard
   *    `setPlatforms` already applies, for the same reason.
   * 2. The screen is resolved once, through the deadband, so steps 4-6 all
   *    agree about which screen they are working against.
   * 3. Nothing is written unless the factors actually moved. `layoutClip` has
   *    no dirty check of its own, so without this a pet standing still writes
   *    six style properties every frame for as long as it exists.
   * 4. The size goes through `applySize`, never through `this.k` or
   *    `this.w`/`this.h` directly: `render` reads the shadow's size back out of
   *    its own inline CSS, so a size written anywhere but `layoutClip` leaves
   *    the shadow at the old dimensions and mis-centred.
   * 5. A pet that just grew still has its feet where they were -- the body
   *    grows upward -- but its head can now be above its own screen's top edge,
   *    and a platform pet can overhang further than the card allows.
   *    `setSizeFactor` re-snaps platform pets and has never handled free ones;
   *    a free pet crossing a seam is precisely the case it does not cover.
   * 6. The world box is the bound that cannot go stale, so it goes last.
   */
  applyDisplaySize() {
    if (this.state === 'held' || this.state === 'air' || this.state === 'hop') return

    const d = this.governingDisplay()
    if (!d) return

    const M = this.world.displayScale(d)
    const K = this.world.displayTaste(d)
    // Half a percent: below that the rounding in `layoutClip` cannot produce a
    // different pixel, so the write would be pure cost.
    const same = (a, b) => Math.abs(a - b) <= Math.abs(b) * 0.005
    if (same(M, this.dispM) && same(K, this.dispK)) return

    this.applySize()

    // A destination chosen at the old size can sit outside the bounds the new
    // size produces, and the pet would then walk to the clamp and stop there.
    this.target = null

    if (this.mode === 'platform') this.snap()
    else this.settleToFloor()

    this.clampToWorld()
  }

  /**
   * Resize this pet alone.
   *
   * A no-op when the factor has not moved, because this is called for every
   * pet on every roster change and `layoutClip` writes to the DOM.
   */
  setSizeFactor(size) {
    const f = petFactor(size)
    if (f === this.sizeFactor) return
    this.sizeFactor = f
    this.applySize()
    // A platform pet's feet stay put while its body grows upward, but a free
    // one that just got taller can end up with its feet below its own floor.
    if (this.mode === 'platform') this.snap()
  }

  /**
   * Size and anchor the `<img>` for the clip currently playing.
   *
   * The canvas is whatever the artist drew; what is lined up with the platform
   * is the character's own opaque box inside it.
   */
  layoutClip() {
    const m = this.variant.m[this.action]
    if (!m) return

    const [cw, ch, bx, by, bw, bh] = m
    const k = this.k

    this.w = Math.max(6, bw * k)
    this.h = Math.max(6, bh * k)
    this.anchorX = (bx + bw / 2) * k
    this.anchorY = (by + bh) * k

    // The canvas itself needs a floor too, separate from the character box
    // above: a pet scaled down enough (a tiny `sizeFactor` on a small world
    // size) can make `cw * k` / `ch * k` round to 0px, which collapses the
    // `<img>` to nothing -- invisible and, since it has no box, unclickable.
    this.el.style.width = `${Math.max(1, cw * k).toFixed(1)}px`
    this.el.style.height = `${Math.max(1, ch * k).toFixed(1)}px`
    // Flip and tilt around the character's feet, not the canvas corner.
    this.el.style.transformOrigin = `${this.anchorX.toFixed(1)}px ${this.anchorY.toFixed(1)}px`

    this.shadowEl.style.width = `${Math.round(this.w * 0.82)}px`
    this.shadowEl.style.height = `${Math.max(3, Math.round(this.h * 0.18))}px`
  }

  destroy() {
    this.el.remove()
    this.shadowEl.remove()
  }

  lift() { this.el.style.zIndex = String(++Z) }

  // ── clips ───────────────────────────────────────────────────────────────────

  clipFor(kind) {
    for (const action of CLIP_CHAIN[kind] || CLIP_CHAIN.idle) {
      if (this.variant.clips[action]) return action
    }
    return Object.keys(this.variant.clips)[0]
  }

  setClip(kind) {
    const action = this.clipFor(kind)
    const src = action && this.variant.clips[action]
    if (!src || src === this.clip) return
    this.clip = src
    this.action = action
    this.el.src = this.world.assetBase + src
    this.layoutClip()
  }

  // ── platforms ───────────────────────────────────────────────────────────────

  get platform() { return this.world.platformById(this.platformId) }

  /**
   * Keep the drawn character inside the world, whatever the cards say.
   *
   * Platform clamps are all expressed against a card's own span, which is only
   * as trustworthy as the last measurement of it. When the window shrinks
   * faster than the cards are re-measured, that span still describes the old,
   * wider window and walks the pet straight out of the visible area, where
   * `overflow: hidden` slices it in half. The world box is the one bound that
   * cannot go stale, so every position ultimately passes through here.
   */
  clampToWorld() {
    const halfW = this.w * 0.5
    const w = this.world.w
    const h = this.world.h
    if (w > 0) this.x = clamp(this.x, halfW, w - halfW)
    // Feet, not centre: `y` is where the character stands.
    //
    // The lower bound is the floor of the screen under the pet, not the bottom
    // of the world. The overlay covers whole displays, so the world's bottom
    // edge runs *behind the taskbar*; clamping to it is what let a pet stand
    // on the clock. `roamBounds()` already knew the right floor, but this runs
    // after it on nine call sites and silently widened the box again.
    //
    // `Math.min` rather than the floor alone: a display map that has not
    // arrived yet, or a point over no display at all, must not be able to
    // clamp a pet *below* the world and out of sight.
    //
    // The head clearance is the top of the SCREEN the pet is over, not the top
    // of the world -- the same asymmetry the floor above corrects, in the other
    // direction. A screen that does not start at the world's own origin (any
    // monitor mounted lower than its neighbour, which is the common case: the
    // measured desktop's secondary starts 154 CSS px down) has a band of world
    // above it that is part of no display and draws nothing. A pet clamped to
    // `this.h` there keeps its feet inside the world and hangs its head in that
    // blank band, and growing the pet for a dense panel is exactly what pushes
    // the head up into it. `roamBounds` has always used `top + this.h`; this
    // ran after it on nine call sites and quietly widened the box again.
    //
    // Applied only while the pet FITS between that top edge and the floor.
    // When it does not, the display-absolute bound would invert the range, and
    // `clamp` answers an inverted range with its midpoint -- which for a pet
    // taller than its screen would be a NEW way to park it behind the taskbar,
    // traded for a head that was merely clipped. The world-absolute clearance
    // is the fallback because it is byte-for-byte the old behaviour: a pet too
    // big for its screen is no worse off than it is today, and there is a green
    // test pinning that midpoint for the pet-wider-than-the-world case.
    if (h > 0) {
      const { top, floor } = this.world.screenSpan(this.x, this.y)
      const bottom = Number.isFinite(floor) ? Math.min(h, floor) : h
      const head = Number.isFinite(top) && top > 0 ? top + this.h : this.h
      this.y = clamp(this.y, head <= bottom ? head : this.h, bottom)
    }
  }

  snap() {
    if (this.mode === 'free') return
    const p = this.platform
    if (!p) return
    this.x = clamp(this.x, p.x1 + this.w * 0.4, p.x2 - this.w * 0.4)
    this.y = p.y
    // The card's span is advisory; the window's is not.
    this.clampToWorld()
  }

  moveTo(platform, keepX) {
    if (!platform) return
    this.platformId = platform.id
    this.cameFrom = null
    // `rand` has no inverted-range guard, and this corridor is a full sprite
    // width in from each edge: a pet that grew wider than half the card gets a
    // point OUTSIDE the card it is being placed on. This is the recovery path
    // for a pet that fell off the world, so landing it off the card again is
    // the one outcome it must not have.
    if (!keepX) {
      const lo = platform.x1 + this.w
      const hi = platform.x2 - this.w
      this.x = lo > hi ? (platform.x1 + platform.x2) / 2 : rand(lo, hi)
    }
    this.snap()
    this.state = 'land'
    this.timer = 0.35
    this.setClip('land')
  }

  /**
   * The cards on the nearest level up (-1) or down (+1), nearest in x first.
   *
   * Levels, not list positions. Cards sit two to a row -- ip beside rf, lan
   * beside ping, hw beside tz -- so in a list sorted by y the entry next to
   * this one is as often the card *beside* the pet as the card above or below
   * it. Walking that list by index produced a leap to the same height, which
   * is not a level change and does not read as one; worse, the two cards in a
   * row are each other's neighbour, so a pet could hop ip->rf->ip->rf without
   * ever leaving the row. That is the loop this used to fall into.
   *
   * The list is ordered by how far each card is from the pet in x, so taking
   * the head of it keeps a hop as vertical as the layout allows, and the tail
   * is there for `pickHopTarget` when straight up or straight down is the card
   * the pet has just come from.
   */
  /**
   * Of these cards, the ones this pet has room to land on.
   *
   * `1.5 * w` is the comfortable figure -- room to land and then walk a little.
   * It was also an absolute veto, and a pet sized up for a dense panel can be
   * wide enough that EVERY card fails it. The pet then has nowhere to hop, for
   * as long as it is that size, and simply stops using half its behaviour; it
   * reads as the pet having frozen, with nothing to indicate why.
   *
   * So the comfortable width is preferred and a tighter one accepted rather
   * than returning nothing. `1.1` is the floor because the walk clamp needs
   * `0.8 * w` of card to keep a pet from turning round on the spot every frame
   * -- landing somewhere it cannot stand would trade a frozen pet for a
   * twitching one.
   */
  roomyOf(cards) {
    const fits = (k) => cards.filter((p) => p.x2 - p.x1 > this.w * k)
    const easy = fits(1.5)
    return easy.length ? easy : fits(1.1)
  }

  levelCards(delta) {
    const here = this.platform
    if (!here) return []
    const side = this.roomyOf(this.world.platforms.filter((p) => (
      delta < 0 ? p.y < here.y - LEVEL_EPS : p.y > here.y + LEVEL_EPS
    )))
    if (!side.length) return []
    // The nearest level in that direction, then every card sitting on it.
    const level = side.reduce((best, p) => (
      Math.abs(p.y - here.y) < Math.abs(best.y - here.y) ? p : best
    )).y
    const off = (p) => Math.abs(p.x1 + p.x2 - 2 * this.x)
    return side
      .filter((p) => Math.abs(p.y - level) <= LEVEL_EPS)
      .sort((a, b) => off(a) - off(b))
  }

  /** The card one level up (-1) or down (+1), or null. */
  levelStep(delta) {
    return this.levelCards(delta)[0] || null
  }

  /**
   * The other cards on the pet's own level, nearest in x first.
   *
   * Hopping sideways is not a level change, and a pet that does it by accident
   * bounces along a row -- that is the bug `levelCards` exists to prevent, and
   * nothing here reopens it: the card just left is excluded, and with cards two
   * to a row that leaves no sideways move at all on the hop after this one.
   *
   * What it buys is a way out of a dead end. On a two-level layout -- three
   * rows of cards, since the topmost is not ground -- "never go back where you
   * came from" leaves exactly one legal card at every step, and four cards in
   * a fixed order is a loop however honestly each hop was chosen.
   */
  rowCards() {
    const here = this.platform
    if (!here) return []
    const off = (p) => Math.abs(p.x1 + p.x2 - 2 * this.x)
    return this.roomyOf(this.world.platforms.filter((p) => (
      p.id !== here.id && Math.abs(p.y - here.y) <= LEVEL_EPS
    ))).sort((a, b) => off(a) - off(b))
  }

  /** Move one platform up (-1) or down (+1) in visual order. */
  step(delta) {
    if (this.mode === 'free') return false
    const next = this.levelStep(delta)
    if (!next || next.id === this.platformId) return false
    this.hopToPlatform(next)
    return true
  }

  // ── interaction ─────────────────────────────────────────────────────────────

  grab(cursorX, cursorY) {
    this.state = 'held'
    this.chasing = false
    this.grabDX = this.x - cursorX
    this.grabDY = this.y - cursorY
    this.lastCursor = { x: cursorX, y: cursorY, t: performance.now() }
    this.vx = 0
    this.vy = 0
    this.el.classList.add('is-held')
    this.lift()
    this.setClip('held')
  }

  /**
   * Where a dragged pet is allowed to be, for a position it is being asked for.
   *
   * The bottom bound is the floor of the screen the pet would land over, not
   * the bottom of the world -- the same bound `clampToWorld` is careful to use
   * and the one the drag path was missing. The world's bottom edge runs behind
   * the taskbar and, on a desktop whose monitors are different heights, across
   * the dead rectangle under the shorter one: a pet let go in either place is
   * out of sight, and the rectangle is also exactly where the size resolver has
   * no panel density to read.
   *
   * The floor is resolved for the position being proposed, not the one the pet
   * still has, so the bound changes on the frame the cursor crosses the seam
   * rather than one frame late.
   *
   * `this.h > bottom` is spelled out: a pet taller than the band between the
   * top of the world and that floor inverts the range, and `clamp` answers an
   * inverted range with its midpoint, which would leave the pet floating in the
   * middle of the screen under the cursor. The floor is the standable end.
   */
  dragClamp(px, py) {
    const x = clamp(px, this.w * 0.5, this.world.w - this.w * 0.5)
    const { floor } = this.world.screenSpan(x, py)
    const bottom = Number.isFinite(floor) ? Math.min(this.world.h, floor) : this.world.h
    const y = this.h > bottom ? bottom : clamp(py, this.h, bottom)
    return { x, y }
  }

  /**
   * Re-anchor a held pet whose size just changed under it.
   *
   * `grabDX`/`grabDY` are absolute pixels taken at `pointerdown`. Grab a pet by
   * the head on the sparse panel and the offset is most of its height; make it
   * half again as tall and that same offset is its belly, so the sprite appears
   * to slide through the user's hand as it inflates. Scaling both offsets by
   * the size ratio keeps hold of the same POINT ON THE BODY -- `grabDY` by the
   * height because `y` is the feet and the body grows upward from them.
   *
   * Then the clamp is re-run from the last known cursor, so the pet settles at
   * a legal position for its new size in the same frame rather than waiting for
   * a `pointermove` that may never come if the pointer is holding still.
   * Deliberately not via `dragTo`: that would recompute the tilt and the throw
   * velocity from a zero-length move and quietly discard the flick the user is
   * in the middle of.
   */
  regrab(wOld, hOld) {
    if (wOld > 0) this.grabDX *= this.w / wOld
    if (hOld > 0) this.grabDY *= this.h / hOld
    const c = this.lastCursor
    if (!c) return
    const { x, y } = this.dragClamp(c.x + this.grabDX, c.y + this.grabDY)
    this.x = x
    this.y = y
  }

  dragTo(cursorX, cursorY) {
    const now = performance.now()
    const dt = Math.max(8, now - this.lastCursor.t) / 1000
    const dx = cursorX - this.lastCursor.x

    // A drag can carry the cursor anywhere, including straight off the edge of
    // the widget or desktop -- clamp to the world box so the sprite never gets
    // dragged out where it can neither be seen nor grabbed again, and to the
    // floor of the screen under it so it cannot be parked behind the taskbar.
    const at = this.dragClamp(cursorX + this.grabDX, cursorY + this.grabDY)
    this.x = at.x
    this.y = at.y

    // Swing the sprite into the direction of travel: it makes the pet feel
    // like it has weight, rather than being glued to the pointer.
    const vx = dx / dt
    this.tilt = clamp(-vx * 0.02, -18, 18)
    if (Math.abs(vx) > 40) this.dir = vx > 0 ? 1 : -1

    this.throwVx = vx
    this.lastCursor = { x: cursorX, y: cursorY, t: now }
  }

  release() {
    this.el.classList.remove('is-held')

    // A loose pet has no platform to fall to, so it carries on from wherever
    // it was put down.
    if (this.mode === 'free') {
      const b = this.roamBounds()
      this.x = clamp(this.x, b.x1, b.x2)
      this.y = clamp(this.y, b.y1, b.y2)
      // Roam bounds come from the display list, which lags an unplugged screen
      // or a resized desktop; the world box is measured, so it cannot.
      this.clampToWorld()
      this.state = 'roam'
      this.target = null
      this.setClip('idle')
      return
    }

    this.state = 'air'
    this.vx = clamp((this.throwVx || 0) * 0.45, -900, 900)
    this.vy = 0
    this.setClip('air')
  }

  poke() {
    if (this.state === 'held') return
    this.state = 'swipe'
    this.timer = 0.55
    this.setClip('swipe')
    this.faceCursor()
  }

  faceCursor() {
    if (!this.world.cursor.inside) return
    this.dir = this.world.cursor.x >= this.x ? 1 : -1
  }

  toggleChase() {
    this.chasing = !this.chasing
    if (this.chasing) {
      this.state = 'chase'
      this.setClip('run')
    } else {
      this.state = 'idle'
      this.timer = 0.6
      this.setClip('idle')
    }
    return this.chasing
  }

  // ── leaps ───────────────────────────────────────────────────────────────────

  /**
   * A parabolic arc to an arbitrary point. Used for vaulting over another pet,
   * for hopping between cards, and for a loose pet changing height.
   */
  hopTo(x1, y1, peak) {
    this.state = 'hop'
    this.hopT = 0
    this.hopDur = clamp(Math.hypot(x1 - this.x, y1 - this.y) / 420, 0.34, 0.95)
    this.hopX0 = this.x
    this.hopY0 = this.y
    this.hopX1 = x1
    this.hopY1 = y1
    this.hopPeak = peak
    if (Math.abs(x1 - this.x) > 4) this.dir = x1 > this.x ? 1 : -1
    this.hopCooldown = 1.1
    this.lift()
    this.setClip('air')
  }

  /**
   * Leap onto another card.
   *
   * The landing is directly above or below wherever the pet already is where
   * that is on the target card, and the nearest end of it otherwise -- so a pet
   * crossing to a card that does not reach this far along jumps to its corner
   * rather than off into the gutter.
   */
  hopToPlatform(p) {
    const landing = clamp(this.x, p.x1 + this.w * 0.5, p.x2 - this.w * 0.5)
    const dy = Math.abs(p.y - this.y)
    this.cameFrom = this.platformId
    // Owed to the card it is aiming at: a pet that may hop the instant it lands
    // bounces between two cards several times a second, which is the same loop
    // read at speed. Long enough to walk a little of the new card first.
    this.hopRest = rand(2.4, 4.2)
    this.hopToId = p.id
    this.hopTo(landing, p.y, Math.min(90, dy * 0.5 + this.h * 0.7))
  }

  /** Two pets have met on the same card. This one vaults; the other waits. */
  hopOver(other) {
    const clearance = (this.w + other.w) / 2 + 12
    let landing = other.x + this.dir * clearance

    if (this.mode === 'platform') {
      const p = this.platform
      if (!p) return false
      const lo = p.x1 + this.w * 0.4
      const hi = p.x2 - this.w * 0.4
      // Not enough card left to land on: turn round instead.
      if (landing < lo || landing > hi) return false
      this.hopToId = p.id
    } else {
      const b = this.roamBounds()
      landing = clamp(landing, b.x1, b.x2)
      this.hopToId = null
    }

    this.hopTo(landing, other.y, Math.max(this.h, other.h) * 0.8 + 10)
    return true
  }

  /** The pet being jumped over holds still, so the vault reads clearly. */
  yieldTo() {
    if (BUSY.has(this.state)) return
    if (this.state === 'dance') return     // already stationary; let it finish
    this.state = 'idle'
    this.timer = rand(0.45, 0.8)
    this.hopCooldown = 0.9
    this.setClip('idle')
  }

  /** Switch between riding the cards and roaming the desktop. */
  setMode(mode) {
    const next = mode === 'free' ? 'free' : 'platform'
    if (this.mode === next) return
    this.mode = next
    this.target = null
    this.chasing = false

    if (next === 'free') {
      this.state = 'roam'
      this.setClip('walk')
      return
    }
    // Coming home: fall onto whatever card is underneath.
    const p = this.world.landingBelow(this.x, this.y, this.w * 0.5)
      || this.world.nearest(this.x, this.y)
    if (p) this.platformId = p.id
    this.state = 'air'
    this.vx = 0
    this.vy = 0
    this.setClip('air')
  }

  // ── free roaming ────────────────────────────────────────────────────────────

  /**
   * Where this pet may wander.
   *
   * Left and right are the whole desktop, deliberately: clamping a pet to the
   * screen it is on is what "pets only appear on the main monitor" was, and a
   * pet that cannot cross a seam cannot be dragged across one either. Up and
   * down come from the screen under it instead, and are re-read on every call,
   * so crossing that seam changes which floor it is standing on.
   */
  roamBounds() {
    const { top, floor } = this.world.screenSpan(this.x, this.y)
    return {
      x1: this.w * 0.6,
      x2: this.world.w - this.w * 0.6,
      y1: top + this.h + 24,
      y2: floor - 6,
    }
  }

  /**
   * Put a loose pet back on the floor of the screen it is actually over.
   *
   * Walking across a seam can move the ground without the pet moving at all:
   * two monitors need not be the same height or share a bottom edge, so the
   * floor it left is not the floor it arrives on. Left alone it either hangs
   * in the air over a taller neighbour or drops into the dead rectangle under
   * a shorter one and vanishes -- which is exactly what happened the first
   * time a pet was let across.
   *
   * A hop rather than a snap, because stepping up onto a screen is a thing a
   * pet does and teleporting is not.
   */
  settleToFloor() {
    const b = this.roamBounds()
    // Both bounds carry the pet's own height (`y1 = top + h + 24`), so a pet
    // too tall for the band between a screen's top edge and its floor inverts
    // them. The old "am I already settled?" test compared against `y1` and `y2`
    // separately, which no y on the number line can satisfy once y1 > y2 -- so
    // this ran every roam frame and launched a hop every time, forever, while
    // `clamp` parked the pet at the midpoint of a band it cannot fit in.
    // Resolving the target first and asking whether we are already AT it is
    // the same test when the band is sane, and terminates when it is not.
    // The floor is the end to choose: a pet standing on the floor is visible
    // and standable, and the midpoint is neither.
    const y = b.y1 > b.y2 ? b.y2 : clamp(this.y, b.y1, b.y2)
    if (Math.abs(this.y - y) <= 1) return
    this.hopToId = null
    this.hopTo(this.x, y, Math.min(120, Math.abs(y - this.y) * 0.45 + this.h * 0.6))
    this.target = null
  }

  pickRoamTarget() {
    const b = this.roamBounds()
    // Mostly short strolls, occasionally a dash right across the screen.
    const reach = Math.random() < 0.25 ? this.world.w * 0.6 : this.world.w * 0.22
    const tx = clamp(this.x + rand(-reach, reach), b.x1, b.x2)
    const ty = Math.random() < 0.55
      ? this.y
      : clamp(this.y + rand(-this.world.h * 0.3, this.world.h * 0.3), b.y1, b.y2)
    this.target = { x: tx, y: ty }
  }

  // ── behaviour ───────────────────────────────────────────────────────────────

  /**
   * Walking pace, in CSS px per second.
   *
   * Pace is tied to height on purpose -- a bigger pet takes bigger strides --
   * and that is what carries the per-display factor `M` through to motion: on a
   * denser panel the pet is more CSS pixels tall, so it covers more CSS pixels
   * per second, and both cancel to the SAME REAL-WORLD speed. A pet that kept a
   * constant pixel pace would visibly trudge across the dense screen and scurry
   * across the sparse one.
   *
   * `K` is divided back out because it is not a measurement. It is somebody's
   * opinion that pets should look bigger on one screen, and letting an opinion
   * about size silently become an opinion about speed makes the two impossible
   * to tune independently -- turn the pet up on the big monitor and it starts
   * sprinting, with nothing on screen to say why.
   */
  speedPx() {
    const taste = this.dispK > 0 ? this.dispK : 1
    return BASE_SPEED * (this.h / taste / 56) * this.world.opts.speed * this.tempo
  }

  /**
   * A dance is a full stop, never something that happens mid-stride: the pet
   * plants itself where it is and shakes.
   */
  startDance() {
    this.state = 'dance'
    this.danceT = 0
    this.timer = rand(2.6, 6.5)
    this.setClip('walk')            // legs keep moving, on the spot
  }

  wantsDance() {
    return this.world.opts.dance && !this.world.opts.calm
  }

  /**
   * A card to hop to: one level up or down, and only if it is wide enough.
   *
   * Straight up and straight down are the first choices, because a vertical
   * leap is the one that reads as a level change. What stops that being a
   * two-card bounce is that the card the pet just left is taken out of the
   * running: with three levels it simply carries on in the direction it was
   * going, and with two it falls through to the card *diagonally* across. That
   * is still a level change, and it walks the pet around the layout instead of
   * back and forth over its own footprints.
   *
   * Excluding one card is not enough on its own, though. A four-card layout
   * then has exactly one legal target at every step, which is a fixed circuit
   * -- so a fifth of the time the pet takes the card beside it instead (see
   * `rowCards`), and the circuit stops being predictable.
   *
   * Only when the card it came from is genuinely the sole place to stand does
   * it go back there.
   */
  pickHopTarget() {
    const up = this.levelCards(-1)
    const down = this.levelCards(1)
    // Sideways is a garnish on a repertoire of level changes, never a
    // repertoire of its own: with no level to change to, a pet has only the
    // card beside it and the one it came from, and alternating between two
    // cards forever is the loop itself. On a layout like that it stays put.
    if (!up.length && !down.length) return null

    const notBack = (p) => p.id !== this.cameFrom
    const straight = [up[0], down[0]].filter(Boolean)
    const fresh = straight.filter(notBack)
    const diagonal = [...up.slice(1), ...down.slice(1)].filter(notBack)
    const beside = this.rowCards().filter(notBack)

    // A level change is the point of hopping, so it wins most of the time.
    if (beside.length && Math.random() < 0.22) return pick(beside)
    if (fresh.length) {
      if (diagonal.length && Math.random() < 0.3) return pick(diagonal)
      return pick(fresh)
    }
    if (diagonal.length) return pick(diagonal)
    if (beside.length) return pick(beside)
    // Nothing anywhere but the card it came from: going back beats being stuck.
    return straight.length ? pick(straight) : null
  }

  decide() {
    if (this.mode === 'free') {
      this.state = 'roam'
      this.target = null
      return
    }
    // No cards at all: nothing to walk along and nowhere to hop to, so it
    // waits rather than stepping off into a fall it cannot land from.
    if (!this.world.platforms.length) {
      this.state = 'idle'
      this.timer = rand(0.5, 1.5)
      this.setClip('idle')
      return
    }
    const live = this.world.opts.liveliness / 100        // 0 lazy .. 1 hyper
    const roll = Math.random()
    const restWeight = 0.40 - live * 0.28                // .40 -> .12
    const runWeight = 0.08 + live * 0.20                 // .08 -> .28
    const danceWeight = this.wantsDance() ? 0.06 + live * 0.12 : 0
    // Hopping between cards is what makes a widget pet read as living in the
    // widget rather than on one card of it, so it is weighted like a first-class
    // activity rather than as a rare flourish.
    const hopWeight = this.world.platforms.length > 1 && this.hopRest <= 0
      ? 0.10 + live * 0.16
      : 0

    let cut = restWeight
    if (roll < cut) {
      const lazy = Math.random() < 0.45 && this.variant.clips.lie
      this.state = lazy ? 'lie' : 'idle'
      this.timer = rand(1.8, 6) * (1.4 - live)
      this.setClip(lazy ? 'lie' : 'idle')
      return
    }
    cut += hopWeight
    if (roll < cut) {
      const target = this.pickHopTarget()
      if (target) { this.hopToPlatform(target); return }
    }
    cut += danceWeight
    if (roll < cut) { this.startDance(); return }
    cut += runWeight
    if (roll < cut) {
      this.state = 'run'
      this.timer = rand(1.2, 3.2)
      this.setClip('run')
      return
    }
    this.state = 'walk'
    this.timer = rand(2.5, 8)
    this.setClip('walk')
    if (Math.random() < 0.35) this.dir *= -1
  }

  curiosity(dt) {
    const o = this.world.opts
    if (!o.follow || !this.world.cursor.inside || o.calm) return
    const dx = this.world.cursor.x - this.x
    const dy = this.world.cursor.y - this.y
    const d = Math.hypot(dx, dy)
    if (d > NOTICE_R) return

    // Close by: look at the cursor. Very close: occasionally give chase.
    if (this.state === 'idle' || this.state === 'lie') {
      this.dir = dx >= 0 ? 1 : -1
      if (d < 110 && Math.random() < dt * 0.9) {
        this.state = 'chase'
        this.setClip('run')
      }
    } else if (this.state === 'walk' && d < 120 && Math.random() < dt * 0.5) {
      this.state = 'chase'
      this.setClip('run')
    }
  }

  update(dt) {
    this.vxNow = 0
    if (this.hopCooldown > 0) this.hopCooldown -= dt
    if (this.hopRest > 0) this.hopRest -= dt

    switch (this.state) {
      case 'held':
        break

      case 'air': {
        if (this.mode === 'free') { this.state = 'roam'; this.target = null; break }
        const prevY = this.y
        this.vy += GRAVITY * dt
        this.vx *= Math.pow(AIR_DRAG, dt)
        this.y += this.vy * dt
        this.x += this.vx * dt

        // Bounce off the sides rather than vanishing past them.
        if (this.x < this.w * 0.5) { this.x = this.w * 0.5; this.vx = Math.abs(this.vx) * 0.55 }
        if (this.x > this.world.w - this.w * 0.5) {
          this.x = this.world.w - this.w * 0.5
          this.vx = -Math.abs(this.vx) * 0.55
        }

        const p = this.world.landingBelow(this.x, prevY, this.w * 0.5)
        if (p && this.y >= p.y) {
          this.platformId = p.id
          this.cameFrom = null
          this.y = p.y
          this.vy = 0
          this.vx = 0
          this.state = 'land'
          this.timer = 0.3
          this.setClip('land')
          this.snap()
          break
        }

        // Fell past everything. With a card to land on it goes back to the
        // lowest one; with no cards at all it waits at the foot of the widget.
        //
        // It used to be dropped in again from the top instead, which is fine
        // for the case that was in mind -- a layout mid-swap, cards back within
        // a frame -- and a visible infinite loop for the case that was not.
        // The widget drawn small enough to stand the frost pass down reports no
        // card geometry at all, for as long as it stays that size, so there was
        // never a card to land on and the pet fell through the widget from the
        // top over and over. Waiting costs nothing: `setPlatforms` puts it back
        // in the air the moment there is ground to aim at.
        // A body length below the world, never a flat 120. The threshold is
        // asking "is this sprite still visible?", and that question is answered
        // in units of the sprite: 120px is most of the way past a 26px pet and
        // nowhere near past a pet sized up for a dense panel, which can stand
        // 512px tall (PET_MAX_H). Declaring THAT one lost snatches away a
        // sprite whose head is still well inside the window and drops it on
        // the lowest card -- a teleport, from the user's side, of a pet they
        // can plainly see. The 120 stays as the floor so a small pet's
        // threshold is unchanged, and the +24 matches `roamBounds`' own
        // standing margin so the two bounds do not disagree by a hair.
        if (this.y > this.world.h + Math.max(120, this.h + 24)) {
          const ground = this.world.lowest()
          if (ground) { this.moveTo(ground, false); break }
          // Half-width per side, like every other horizontal bound in this
          // file (`clampToWorld`, `dragClamp`, `roamBounds` all use `w * 0.5`
          // or `w * 0.6`). A full `this.w` insets the corridor by a whole body
          // on each side, which for a pet grown for a dense panel is most of a
          // narrow world -- so the recovery that exists to put the pet back
          // where it can be seen shoves it, in one frame, further than it
          // would have travelled had it never been rescued.
          //
          // And the inversion is spelled out rather than hidden. The old
          // `Math.max(this.w, ...)` collapsed an inverted range onto its LEFT
          // inset, parking a pet wider than the world hard against a bound
          // that is past the right edge. There is no corridor at all when the
          // pet is wider than the world; the world's centre is the only
          // symmetric answer, and it is the one `clampToWorld` gives, so the
          // two agree instead of fighting on the next frame.
          const halfW = this.w * 0.5
          const lo = halfW
          const hi = this.world.w - halfW
          this.x = lo > hi ? this.world.w / 2 : clamp(this.x, lo, hi)
          this.y = this.world.h - 8
          this.vy = 0
          this.vx = 0
          // `h - 8` is shallower than the sprite itself once the widget is
          // small enough, which would hang its head out of the top edge.
          this.clampToWorld()
          this.platformId = null
          this.state = 'idle'
          this.timer = rand(0.6, 1.4)
          this.setClip('idle')
        }
        break
      }

      case 'hop': {
        this.hopT += dt
        const t = Math.min(1, this.hopT / this.hopDur)
        this.x = this.hopX0 + (this.hopX1 - this.hopX0) * t
        this.y = (this.hopY0 + (this.hopY1 - this.hopY0) * t) - Math.sin(Math.PI * t) * this.hopPeak
        // The arc was aimed when the world was a different size, and its peak
        // is unbounded upwards, so mid-flight needs the same bound as landing.
        this.clampToWorld()
        // Lean into the take-off and out of the landing.
        this.tilt = Math.cos(Math.PI * t) * 9 * this.dir
        if (t >= 1) {
          this.x = this.hopX1
          this.y = this.hopY1
          this.tilt = 0
          // Landing on a stale endpoint escapes the window just as surely as
          // flying to one does.
          this.clampToWorld()
          // The card it was aiming for may have gone -- a mode switch, a card
          // that collapsed -- in which case it falls instead of standing on a
          // platform that is no longer there.
          if (this.hopToId && this.world.platformById(this.hopToId)) {
            this.platformId = this.hopToId
            this.state = 'land'
            this.timer = 0.22
            this.setClip('land')
            this.snap()
          } else if (this.mode === 'platform') {
            this.state = 'air'
            this.vy = 0
            this.setClip('air')
          } else {
            this.state = 'land'
            this.timer = 0.22
            this.setClip('land')
          }
          this.hopToId = null
        }
        break
      }

      case 'roam': {
        if (!this.target) this.pickRoamTarget()
        const b = this.roamBounds()
        const dx = this.target.x - this.x
        const dy = this.target.y - this.y

        // A change of height is a hop, never a glide.
        if (Math.abs(dy) > 10) {
          this.hopToId = null
          this.hopTo(
            clamp(this.x + clamp(dx, -240, 240), b.x1, b.x2),
            clamp(this.target.y, b.y1, b.y2),
            Math.min(120, Math.abs(dy) * 0.45 + this.h * 0.6),
          )
          this.target = null
          break
        }

        if (Math.abs(dx) < 5) {
          this.target = null
          if (this.wantsDance() && Math.random() < 0.28) { this.startDance(); break }
          this.state = Math.random() < 0.3 && this.variant.clips.lie ? 'lie' : 'idle'
          this.timer = rand(0.8, 3.5) * (1.4 - this.world.opts.liveliness / 100)
          this.setClip(this.state)
          break
        }

        const dash = Math.abs(dx) > this.world.w * 0.25
        this.setClip(dash ? 'run' : 'walk')
        this.dir = dx > 0 ? 1 : -1
        this.vxNow = this.dir * this.speedPx() * (dash ? 2.4 : 1)
        this.x = clamp(this.x + this.vxNow * dt, b.x1, b.x2)
        // `b` was read before this step; crossing a screen boundary during it
        // is what makes the floor underneath stale.
        this.settleToFloor()
        this.curiosity(dt)
        break
      }

      case 'dance': {
        this.danceT += dt
        this.timer -= dt
        // Shimmy: face left, face right, repeat on the beat.
        this.dir = Math.floor(this.danceT / 0.5) % 2 === 0 ? 1 : -1
        if (this.timer <= 0 || !this.wantsDance()) {
          if (this.mode === 'free') { this.state = 'roam'; this.target = null } else this.decide()
        }
        break
      }

      case 'land':
        this.timer -= dt
        if (this.timer <= 0) {
          if (this.mode === 'free') { this.state = 'roam'; this.target = null } else this.decide()
        }
        break

      case 'chase': {
        const cursor = this.world.cursor
        let targetX
        let lockY = null

        if (this.mode === 'free') {
          const b = this.roamBounds()
          targetX = clamp(cursor.x, b.x1, b.x2)
          // A loose pet can climb towards the cursor as well as run at it.
          const ty = clamp(cursor.y, b.y1, b.y2)
          const dy = ty - this.y
          if (Math.abs(dy) > 26 && this.hopCooldown <= 0) {
            this.hopToId = null
            this.hopTo(
              clamp(this.x + clamp(targetX - this.x, -200, 200), b.x1, b.x2),
              ty,
              Math.min(110, Math.abs(dy) * 0.4 + this.h * 0.5),
            )
            break
          }
        } else {
          const p = this.platform
          if (!p) { this.state = 'idle'; break }
          targetX = clamp(cursor.x, p.x1 + this.w * 0.4, p.x2 - this.w * 0.4)
          lockY = p.y
        }

        const dx = targetX - this.x
        if (!cursor.inside || Math.abs(dx) < 6) {
          this.state = 'swipe'
          this.timer = 0.6
          this.setClip('swipe')
          this.faceCursor()
          break
        }
        this.dir = dx > 0 ? 1 : -1
        this.vxNow = this.dir * this.speedPx() * 3.1
        this.x += this.vxNow * dt
        if (lockY !== null) this.y = lockY
        // The chase target was clamped to a card span or to roam bounds, both
        // of which can describe a wider window than the one on screen now.
        this.clampToWorld()
        break
      }

      case 'swipe':
      case 'idle':
      case 'lie':
        this.timer -= dt
        this.curiosity(dt)
        if (this.timer <= 0) {
          if (this.chasing) { this.state = 'chase'; this.setClip('run') } else if (this.world.opts.calm) {
            this.timer = 3
            this.setClip('idle')
          } else this.decide()
        }
        break

      case 'walk':
      case 'run': {
        if (this.mode === 'free') { this.state = 'roam'; this.target = null; break }
        const p = this.platform
        // Walked off a card that has gone. Falling is right when there is
        // something below to land on, and is the fall loop again when there is
        // not.
        if (!p) {
          if (this.world.platforms.length) {
            this.state = 'air'
            this.vy = 0
            this.setClip('air')
          } else {
            this.state = 'idle'
            this.timer = rand(0.6, 1.4)
            this.setClip('idle')
          }
          break
        }
        const pace = this.speedPx() * (this.state === 'run' ? 2.6 : 1)

        this.vxNow = this.dir * pace
        this.x += this.vxNow * dt
        this.y = p.y
        this.timer -= dt
        this.curiosity(dt)

        const lo = p.x1 + this.w * 0.4
        const hi = p.x2 - this.w * 0.4
        if (lo > hi) {
          // The card is narrower than the pet needs to stand on it, which a
          // pet that grew on a denser screen can reach. Turning here does not
          // help: `clamp` answers an inverted range with its midpoint, and the
          // midpoint of [lo, hi] when lo > hi lies PAST hi, so the edge test
          // fires again on the very next frame and the pet turns on the spot
          // forever. Park it in the middle of the card and idle instead; the
          // idle timer ends the loop the way any other stop does.
          this.x = (p.x1 + p.x2) / 2
          this.state = 'idle'
          this.timer = rand(0.4, 1.4)
          this.setClip('idle')
        } else if (this.x <= lo || this.x >= hi) {
          this.x = clamp(this.x, lo, hi)
          this.dir *= -1
          // Pause at the end of the card before turning back.
          this.state = 'idle'
          this.timer = rand(0.4, 1.4)
          this.setClip('idle')
        } else if (this.timer <= 0) {
          if (this.world.opts.calm) { this.state = 'idle'; this.timer = 4; this.setClip('idle') } else this.decide()
        }
        // After the turn, never instead of it: the card clamp above owns the
        // deliberate overhang a pet is meant to have at a card's edge, and
        // this only bites when the card is describing a window that is gone.
        this.clampToWorld()
        break
      }

      default:
        this.state = 'idle'
        this.timer = 1
    }

    // A pet that has just crossed a monitor seam is standing on a panel with a
    // different pixel density, so its real-world size changed although nothing
    // about the pet did. Here, rather than in any of the six branches that
    // write `x` above: all of them join at this line, it is after the clamp and
    // the settle that can still move the pet, and it is before `render` runs
    // for this frame, so the new size is painted with the position that earned
    // it instead of one frame late.
    this.applyDisplaySize()

    // Ease the drag tilt back to level.
    if (this.state !== 'held') this.tilt += (0 - this.tilt) * Math.min(1, dt * 9)

    const moving = Math.abs(this.vxNow)
    this.bob += dt * (moving > this.speedPx() * 1.6 ? 15 : moving > 0.5 ? 9 : 2.5)
  }

  render() {
    const calm = this.world.opts.calm
    let bobY = Math.abs(this.vxNow) > 0.5 && !calm ? Math.sin(this.bob) * 0.9 : 0
    let tilt = this.tilt
    let swayX = 0

    // The shake is pure presentation -- `this.x` never moves, so the pet stays
    // exactly where it stopped.
    if (this.state === 'dance' && !calm) {
      const beat = this.danceT * 7.4
      swayX = Math.sin(beat) * 2.6
      bobY -= Math.abs(Math.sin(beat * 2)) * 3.2
      tilt += Math.sin(beat) * 9
    }

    const px = Math.round(this.x + swayX - this.anchorX)
    const py = Math.round(this.y - this.anchorY + bobY)

    this.el.style.transform =
      `translate3d(${px}px,${py}px,0) rotate(${tilt.toFixed(1)}deg) scaleX(${this.dir})`

    if (!this.world.opts.shadows) {
      this.shadowEl.style.opacity = '0'
      return
    }

    // While airborne the shadow stays on the ground below and shrinks.
    let groundY = this.y
    let spread = 1
    if (this.mode === 'free') {
      // Nothing underneath to cast onto, so the shadow rides the pet itself and
      // only softens while it is mid-leap.
      const lift = this.state === 'hop' ? clamp((this.hopY1 - this.y) / 160, 0, 1) : 0
      this.shadowEl.style.opacity = String(0.42 * (1 - lift * 0.7))
      groundY = this.state === 'hop' ? this.hopY1 : this.y
      spread = 1 + lift * 0.4
    } else if (this.state === 'air' || this.state === 'held' || this.state === 'hop') {
      const p = this.world.landingBelow(this.x, this.y, this.w * 0.5) || this.world.lowest()
      if (p) {
        groundY = p.y
        const gap = clamp((groundY - this.y) / 320, 0, 1)
        spread = 1 + gap * 0.5
        this.shadowEl.style.opacity = String(0.55 * (1 - gap * 0.75))
      }
    } else {
      this.shadowEl.style.opacity = '0.5'
    }

    const sw = parseFloat(this.shadowEl.style.width)
    const sh = parseFloat(this.shadowEl.style.height)
    this.shadowEl.style.transform =
      `translate3d(${Math.round(this.x - sw / 2)}px,${Math.round(groundY - sh / 2)}px,0) scale(${spread.toFixed(2)})`
  }

  bounds() {
    const left = this.x - this.w / 2
    const top = this.y - this.h
    return { left, top, right: left + this.w, bottom: top + this.h }
  }

  contains(x, y) {
    const b = this.bounds()
    return x >= b.left && x <= b.right && y >= b.top && y <= b.bottom
  }
}

/**
 * The stage the pets live on: a size, a set of platforms, the settings, and
 * the frame loop.
 */
export class World {
  constructor({ stage, assetBase, opts, scale = 1, ground = 1 }) {
    this.stage = stage
    this.assetBase = assetBase
    this.opts = {
      speed: 1, size: 26, liveliness: 50, follow: true,
      dance: true, shadows: true, calm: false, ...opts,
    }
    /** Multiplies the drawn size, for a widget that has been scaled by hand. */
    this.scale = scale
    /**
     * Where a loose pet's resting floor is, as a fraction of the height.
     * Only consulted when `displays` is empty -- the first frame, before the
     * main process has said what the desktop actually looks like.
     */
    this.ground = ground

    /**
     * The screens this world covers, in its own coordinates.
     *
     * The overlay spans every monitor, so "the floor" is not one number: each
     * screen has its own taskbar, its own height and its own top edge. A pet
     * takes the floor of whichever screen it is currently over, which is what
     * lets it walk across a seam and keep standing on something.
     */
    this.displays = []

    /**
     * The single CSS-to-device factor this WINDOW was given, `S`.
     *
     * One window has one CSS pixel, whatever the desktop under it looks like,
     * and everything in `displays` has already been divided by this number to
     * get here. It is therefore the denominator that turns a panel's own
     * density back into "how much bigger or smaller than nominal should a pet
     * be drawn over here".
     *
     * 0 means the host has not said. See `windowScaleOf` for what is used then.
     */
    this.windowScale = 0

    this.w = 0
    this.h = 0
    this.pets = []
    this.platforms = []
    this.cursor = { x: -9999, y: -9999, inside: false }

    this.running = false
    /** Pending requestAnimationFrame handle, or 0 when no frame is scheduled. */
    this.raf = 0
    this.last = 0
    this.frame = this.frame.bind(this)
  }

  petHeight() { return this.opts.size * this.scale }

  /** The window's CSS-to-device factor, told or inferred. */
  setWindowScale(s) {
    const n = Number(s)
    const next = Number.isFinite(n) && n > 0 ? n : 0
    if (next === this.windowScale) return
    this.windowScale = next
    for (const p of this.pets) p.applySize()
  }

  /**
   * `S`, with a fallback.
   *
   * Windows hands a window the scale factor of whichever monitor it covers most
   * in device pixels, so for an overlay spanning the desktop that is the
   * densest screen it meaningfully overlaps. The largest `sf` in the map is
   * therefore the right guess when nobody has said, and it degrades to 1 on a
   * payload that predates `sf` entirely -- which makes `M` exactly 1 and the
   * whole per-display correction a no-op, rather than a wrong answer.
   */
  windowScaleOf() {
    if (this.windowScale > 0) return this.windowScale
    let max = 0
    for (const d of this.displays) {
      const sf = Number(d.sf)
      if (Number.isFinite(sf) && sf > max) max = sf
    }
    return max > 0 ? max : 1
  }

  /**
   * `M` -- how much bigger a pet must be drawn on this screen to come out the
   * same PHYSICAL size as on the screen the window was scaled for.
   *
   * Real PPI is preferred because it is the actual answer: the window draws at
   * `96 * S` dots per inch, the panel has `ppi`, and the ratio is the
   * correction. `scaleFactor` is the fallback and only an approximation of it,
   * because Windows rounds a display's scale to a user-facing percentage that
   * has nothing to do with the panel's real dot pitch -- it recovers most of
   * the error and is the best available when there is no EDID to read.
   *
   * Anything unusable returns exactly 1, which is the old behaviour, so a
   * payload without the new fields installs and draws as it always did.
   */
  displayScale(d) {
    if (!d) return 1
    const S = this.windowScaleOf()
    if (!(S > 0)) return 1

    const ppi = Number(d.ppi)
    const sf = Number(d.sf)
    let m
    if (Number.isFinite(ppi) && ppi > 0) m = ppi / (96 * S)
    else if (Number.isFinite(sf) && sf > 0) m = sf / S
    else return 1

    if (!Number.isFinite(m) || m <= 0) return 1
    return clamp(m, DISPLAY_SCALE_MIN, DISPLAY_SCALE_MAX)
  }

  /**
   * `K` -- a deliberate per-screen opinion about size, on top of the measured
   * correction. Held apart from `M` permanently: one is a measurement and one
   * is a preference, and a bug in either is only findable while they are still
   * two numbers.
   */
  displayTaste(d) {
    if (!d) return 1
    const k = Number(d.k)
    if (!Number.isFinite(k) || k <= 0) return 1
    return clamp(k, DISPLAY_TASTE_MIN, DISPLAY_TASTE_MAX)
  }

  /**
   * The screen a point is on, or the one it belongs to if it is on none.
   *
   * The bounding box of a set of monitors is not always covered by them: a
   * short screen beside a tall one leaves a rectangle under it that is part of
   * the desktop's extent and part of no display. A pet walking out of the tall
   * screen at its own floor height crosses straight into that rectangle, where
   * nothing is drawn and it simply disappears.
   *
   * Which is why horizontal position decides first. A pet walks along x, so
   * once it is past the seam it is on the new screen and should be standing on
   * *its* floor, even though it is still nearer the old screen's rectangle --
   * one pixel past the edge of a 1440-tall monitor is a whole 500px nearer to
   * it than to the 900-tall one it has just walked onto. Straight nearest-rect
   * matching answers that question the wrong way round and leaves the pet in
   * the gap; asking "whose column is this?" first does not.
   */
  displayAt(x, y) {
    if (!this.displays.length) return null
    const spans = (d) => x >= d.x && x < d.x + d.width

    let column = null
    let columnDy = Infinity
    let nearest = null
    let nearestDist = Infinity

    for (const d of this.displays) {
      const dx = x < d.x ? d.x - x : x > d.x + d.width ? x - (d.x + d.width) : 0
      const dy = y < d.y ? d.y - y : y > d.y + d.height ? y - (d.y + d.height) : 0
      if (dx === 0 && dy === 0) return d
      if (spans(d) && dy < columnDy) { columnDy = dy; column = d }
      const dist = dx * dx + dy * dy
      if (dist < nearestDist) { nearestDist = dist; nearest = d }
    }
    return column ?? nearest
  }

  /** The top edge and resting floor of the screen under a point. */
  screenSpan(x, y) {
    const d = this.displayAt(x, y)
    return d
      ? { top: d.y, floor: d.floor }
      : { top: 0, floor: this.h * this.ground }
  }

  setSize(w, h) {
    if (w === this.w && h === this.h) return
    const before = this.pets.map((p) => ({
      xf: this.w ? p.x / this.w : 0.5,
      yf: this.h ? p.y / this.h : 0.5,
    }))
    this.w = w
    this.h = h
    this.pets.forEach((p, i) => {
      p.x = before[i].xf * w
      if (p.mode === 'free') {
        const b = p.roamBounds()
        p.y = clamp(before[i].yf * h, b.y1, b.y2)
      } else p.snap()
      // Unconditionally, after either branch. This is the path the user's bug
      // runs down: `snap()` clamps to a card, and at the instant the window
      // shrinks those cards still span the old, wider window, so the pet is
      // left outside the new one and sliced by `overflow: hidden`. The sprite
      // also keeps its pixel size while the world loses width, so even a
      // fraction that was safely inside before can land past the edge.
      p.clampToWorld()
      // A world resize moves a pet by a FRACTION of the width, so a pet can end
      // up on a different monitor without having moved itself an inch. Nothing
      // else would notice: `setSize` is not a crossing and not a display
      // change, so without this the pet keeps the density of the screen it used
      // to be on, indefinitely.
      p.applyDisplaySize()
    })
  }

  /**
   * Replace the set of screens.
   *
   * Free pets are re-clamped here rather than left to drift back on their own,
   * because a display change is exactly the moment a pet can end up standing
   * somewhere that no longer exists -- a monitor unplugged out from under it,
   * or a taskbar that moved to the other edge without the desktop's overall
   * size changing at all, which `setSize` would not even see.
   */
  setDisplays(list, windowScale) {
    // Deliberately NOT tightened to require `sf` or `ppi`. A payload built
    // before those existed must still install and simply produce M = 1;
    // rejecting it here would turn an old payload into a silent no-op rather
    // than an error anyone could see.
    this.displays = (Array.isArray(list) ? list : []).filter((d) => (
      d && Number.isFinite(d.x) && Number.isFinite(d.y)
      && d.width > 0 && d.height > 0 && Number.isFinite(d.floor)
    ))
    const s = Number(windowScale)
    if (Number.isFinite(s) && s > 0) this.windowScale = s

    for (const p of this.pets) {
      // Drop the cached screen first: it may be an entry that is no longer in
      // the map at all, and a size taken from a monitor that has been unplugged
      // is worse than no cache.
      p.display = null
      if (p.mode === 'free') {
        const b = p.roamBounds()
        p.x = clamp(p.x, b.x1, b.x2)
        p.y = clamp(p.y, b.y1, b.y2)
      }
      // Platform pets too: a monitor swapped under a card changes the density
      // its pet is standing at, even though the card has not moved.
      p.applyDisplaySize()
    }
  }

  setScale(scale) {
    if (scale === this.scale) return
    this.scale = scale
    for (const p of this.pets) p.applySize()
  }

  setOpts(opts) {
    const sizeChanged = opts.size !== undefined && opts.size !== this.opts.size
    Object.assign(this.opts, opts)
    if (sizeChanged) for (const p of this.pets) p.applySize()
  }

  /**
   * Replace the ground.
   *
   * Every platform is re-supplied on each layout change rather than diffed,
   * because a card's rectangle changes far more often than the set of cards
   * does -- and a pet standing on a card that moved a pixel has to move with it
   * in the same frame, or it visibly floats.
   *
   * A pet whose card has gone entirely is dropped rather than teleported: it
   * falls, and lands on whatever is under it. That is what a mode switch looks
   * like from a pet's point of view, and falling is both the honest animation
   * and the one that needs no special case.
   */
  setPlatforms(list) {
    this.platforms = list
    const ids = new Set(list.map((p) => p.id))
    for (const pet of this.pets) {
      if (pet.mode !== 'platform') continue
      if (pet.platformId && ids.has(pet.platformId)) {
        if (pet.state !== 'held' && pet.state !== 'air' && pet.state !== 'hop') pet.snap()
      } else if (pet.state !== 'held' && pet.state !== 'air' && pet.state !== 'hop') {
        // Drop it into 'air' whether or not any platform survived the swap:
        // the fall handler already lands it on whatever is below when there
        // is ground, and settles it at the foot of the widget when there is
        // none, so there is no need to special-case an empty platform list
        // here and leave the pet stranded, floating over ground that is gone.
        pet.platformId = null
        pet.state = 'air'
        pet.vy = 0
        pet.vx = 0
        pet.setClip('air')
      }
    }
  }

  platformById(id) { return this.platforms.find((p) => p.id === id) || null }

  /**
   * The nearest platform strictly below `y` whose span contains `x`.
   *
   * `pad` is how far outside a card's span `x` may be and still count as above
   * it. `x` is the pet's CENTRE, so the honest answer is half a sprite width:
   * that is the question being asked -- is any of this body over that card?
   *
   * The flat 24px it defaults to was written when every pet was about one size.
   * A pet scaled up for a dense panel is wider than 48px all by itself, so its
   * own body can be over a card that this says it is not above, and it falls
   * straight past the card under its feet. `snap` compounds it: a standing pet
   * is only clamped to `0.4 * w` inside the card's ends, so the sprite legally
   * hangs over the edge and the two rules disagree about the same pet.
   *
   * Callers that know the pet pass `pet.w * 0.5`; the default keeps every
   * caller that does not byte-identical, and the `Math.max` means a small pet
   * never gets a TIGHTER tolerance than before.
   */
  landingBelow(x, y, pad = 24) {
    const tol = Math.max(24, pad)
    let best = null
    let bestY = Infinity
    for (const p of this.platforms) {
      if (p.y < y - 1) continue
      if (x < p.x1 - tol || x > p.x2 + tol) continue
      if (p.y < bestY) { bestY = p.y; best = p }
    }
    return best
  }

  lowest() {
    let best = null
    for (const p of this.platforms) if (!best || p.y > best.y) best = p
    return best
  }

  nearest(x, y) {
    let best = null
    let bestD = Infinity
    for (const p of this.platforms) {
      const cx = clamp(x, p.x1, p.x2)
      const d = Math.hypot(x - cx, y - p.y)
      if (d < bestD) { bestD = d; best = p }
    }
    return best
  }

  add(spec) {
    const pet = new Pet(this, spec)
    this.pets.push(pet)
    // The only place `pets` ever grows, and so the only place a world that
    // parked itself for want of anything to animate can learn otherwise.
    this.wake()
    return pet
  }

  remove(id) {
    const i = this.pets.findIndex((p) => p.id === id)
    if (i < 0) return
    this.pets[i].destroy()
    this.pets.splice(i, 1)
  }

  byId(id) { return this.pets.find((p) => p.id === id) || null }

  clear() {
    for (const p of this.pets) p.destroy()
    this.pets = []
  }

  petAt(x, y) {
    // Topmost first: later pets are drawn over earlier ones.
    for (let i = this.pets.length - 1; i >= 0; i--) {
      if (this.pets[i].contains(x, y)) return this.pets[i]
    }
    return null
  }

  setCursor(x, y, inside) {
    this.cursor.x = x
    this.cursor.y = y
    this.cursor.inside = inside
  }

  /**
   * Two pets walking into each other: one vaults, the other holds still, and
   * they carry on past each other.
   *
   * The jumper is whichever is moving faster, so a wanderer hops over a pet
   * that is standing about rather than both of them stopping dead.
   */
  resolveMeetings() {
    for (let i = 0; i < this.pets.length; i++) {
      const a = this.pets[i]
      if (a.hopCooldown > 0 || BUSY.has(a.state)) continue

      for (let j = i + 1; j < this.pets.length; j++) {
        const b = this.pets[j]
        if (b.hopCooldown > 0 || BUSY.has(b.state)) continue

        // Only pets sharing a walking line can collide.
        if (Math.abs(a.y - b.y) > 14) continue
        if (a.mode === 'platform' && b.mode === 'platform' && a.platformId !== b.platformId) continue

        // Two pets standing still are not a meeting. A dancer counts as
        // standing still, so the walker vaults over it.
        if (Math.abs(a.vxNow) < 1 && Math.abs(b.vxNow) < 1) continue

        const dx = b.x - a.x
        const gap = Math.abs(dx) - (a.w + b.w) / 2
        if (gap > 10) continue

        // Are they actually closing on each other?
        const closing = (a.vxNow - b.vxNow) * Math.sign(dx || 1)
        if (closing <= 1 && gap > -2) continue

        const jumper = Math.abs(a.vxNow) >= Math.abs(b.vxNow) ? a : b
        const stayer = jumper === a ? b : a

        // Aim the jumper at the pet it is meeting before it takes off.
        jumper.dir = stayer.x >= jumper.x ? 1 : -1

        if (jumper.hopOver(stayer)) {
          stayer.yieldTo()
        } else {
          // No room to land: both turn round instead.
          jumper.dir *= -1
          jumper.hopCooldown = 0.8
          stayer.hopCooldown = 0.8
        }
        break
      }
    }
  }

  /**
   * Schedule a frame, if one is wanted and none is already pending.
   *
   * `running` means "this world should animate", not "a frame is queued" -- the
   * two come apart whenever the loop parks itself with no pets to draw. The
   * `raf` handle is what makes double-scheduling impossible, so this is safe
   * to call from anywhere, as often as it likes.
   */
  wake() {
    if (!this.running || this.raf || !this.pets.length) return
    // Fresh clock: the gap since the last frame may be arbitrarily long, and
    // a stale one would hand the first frame a jump instead of a step.
    this.last = performance.now()
    this.raf = requestAnimationFrame(this.frame)
  }

  start() {
    // Idempotent on purpose: PetLayer calls this on repeated renders.
    if (this.running) return
    this.running = true
    this.last = performance.now()
    this.raf = requestAnimationFrame(this.frame)
  }

  /**
   * Really stop, rather than letting one more frame through.
   *
   * Flipping the flag alone left a frame already queued to fire. On the
   * desktop overlay -- a transparent, always-on-top window the size of the
   * whole desktop, with `backgroundThrottling` disabled -- that loop went on
   * compositing forever behind a layer nobody was looking at.
   */
  stop() {
    this.running = false
    if (this.raf) cancelAnimationFrame(this.raf)
    this.raf = 0
  }

  frame(now) {
    this.raf = 0
    if (!this.running) return
    const dt = Math.min(0.05, (now - this.last) / 1000)
    this.last = now

    for (const pet of this.pets) pet.update(dt)
    this.resolveMeetings()
    for (const pet of this.pets) pet.render()

    // Park rather than re-arm when there is nothing to draw. `add()` is the
    // only way back into a non-empty world and it calls `wake()`; `running`
    // deliberately stays true so that wake path needs no second flag, and so
    // `start()` stays idempotent for the callers that rely on it.
    if (!this.pets.length) return
    this.raf = requestAnimationFrame(this.frame)
  }
}

/** How far a press may wander before it counts as a drag rather than a click. */
const DRAG_SLOP = 4

/**
 * Wire the mouse up to a world's pets.
 *
 * Pointer capture rather than window listeners, on purpose. In the widget the
 * pet layer is transparent to the mouse everywhere except on a pet, so a drag
 * that left the sprite would otherwise stop getting moves the instant it did --
 * and the events it did get would be stolen from the cards underneath.
 *
 * `onGesture` reports the things the host has to persist or act on: a mode
 * toggle, and a drag or a poke worth writing down.
 */
export function bindPointer(world, { onGesture } = {}) {
  const say = (kind, pet) => onGesture?.(kind, pet)
  let pressed = null
  let origin = null
  let moved = false
  let dragging = null

  const down = (e) => {
    if (e.button !== 0) return
    const pet = world.petAt(e.clientX, e.clientY)
    if (!pet) return
    pressed = pet
    origin = { x: e.clientX, y: e.clientY }
    moved = false
    e.preventDefault()
    e.stopPropagation()
    try { e.target.setPointerCapture?.(e.pointerId) } catch { /* not capturable */ }
  }

  const move = (e) => {
    world.setCursor(e.clientX, e.clientY, true)
    if (dragging) { dragging.dragTo(e.clientX, e.clientY); return }
    if (!pressed || moved) return
    if (Math.hypot(e.clientX - origin.x, e.clientY - origin.y) <= DRAG_SLOP) return
    moved = true
    pressed.grab(origin.x, origin.y)
    dragging = pressed
  }

  const up = (e) => {
    if (dragging) {
      dragging.release()
      say('drop', dragging)
    } else if (pressed && !moved) {
      pressed.poke()
      say('poke', pressed)
    }
    try { e.target.releasePointerCapture?.(e.pointerId) } catch { /* never had it */ }
    pressed = null
    dragging = null
    moved = false
  }

  // Double-click is the same gesture in both worlds read from opposite sides:
  // it sends a pet across. In the widget that means out onto the desktop; on
  // the desktop it means back to the cards.
  const dbl = (e) => {
    const pet = world.petAt(e.clientX, e.clientY)
    if (!pet) return
    e.preventDefault()
    e.stopPropagation()
    say('swap', pet)
  }

  const menu = (e) => {
    const pet = world.petAt(e.clientX, e.clientY)
    if (!pet) return
    e.preventDefault()
    e.stopPropagation()
    // On the desktop there is nothing above to hop to, so the right-click does
    // the other thing a loose pet can be told: follow the cursor, or stop.
    if (pet.mode === 'free') { pet.toggleChase(); say('chase', pet) } else if (pet.step(-1)) say('hop', pet)
  }

  const wheel = (e) => {
    const pet = world.petAt(e.clientX, e.clientY)
    if (!pet || pet.mode === 'free') return
    e.preventDefault()
    pet.step(e.deltaY > 0 ? 1 : -1)
  }

  const stage = world.stage
  stage.addEventListener('pointerdown', down)
  stage.addEventListener('pointermove', move)
  stage.addEventListener('pointerup', up)
  stage.addEventListener('pointercancel', up)
  stage.addEventListener('dblclick', dbl)
  stage.addEventListener('contextmenu', menu)
  stage.addEventListener('wheel', wheel, { passive: false })

  return () => {
    stage.removeEventListener('pointerdown', down)
    stage.removeEventListener('pointermove', move)
    stage.removeEventListener('pointerup', up)
    stage.removeEventListener('pointercancel', up)
    stage.removeEventListener('dblclick', dbl)
    stage.removeEventListener('contextmenu', menu)
    stage.removeEventListener('wheel', wheel)
  }
}
