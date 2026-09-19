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

const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v)
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

    this.k = 1            // px per source pixel, constant per pet
    this.action = 'idle'
    this.w = 24           // drawn character width  (not the canvas)
    this.h = 24           // drawn character height (not the canvas)
    this.anchorX = 0      // canvas-left -> character centre
    this.anchorY = 0      // canvas-top  -> character feet
    this.clip = null
    this.chasing = false

    this.target = null      // roam destination
    this.hopCooldown = 0    // stops two pets ping-ponging leaps
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
    const target = this.world.petHeight() * this.species.scale * this.sizeFactor
    this.k = target / (this.variant.baseH || target)
    this.layoutClip()
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

    this.el.style.width = `${(cw * k).toFixed(1)}px`
    this.el.style.height = `${(ch * k).toFixed(1)}px`
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

  snap() {
    if (this.mode === 'free') return
    const p = this.platform
    if (!p) return
    this.x = clamp(this.x, p.x1 + this.w * 0.4, p.x2 - this.w * 0.4)
    this.y = p.y
  }

  moveTo(platform, keepX) {
    if (!platform) return
    this.platformId = platform.id
    if (!keepX) this.x = rand(platform.x1 + this.w, platform.x2 - this.w)
    this.snap()
    this.state = 'land'
    this.timer = 0.35
    this.setClip('land')
  }

  /** Move one platform up (-1) or down (+1) in visual order. */
  step(delta) {
    if (this.mode === 'free') return false
    const ordered = this.world.platforms.slice().sort((a, b) => a.y - b.y)
    const here = ordered.findIndex((p) => p.id === this.platformId)
    const next = ordered[clamp(here + delta, 0, ordered.length - 1)]
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

  dragTo(cursorX, cursorY) {
    const now = performance.now()
    const dt = Math.max(8, now - this.lastCursor.t) / 1000
    const dx = cursorX - this.lastCursor.x

    this.x = cursorX + this.grabDX
    this.y = cursorY + this.grabDY

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
    const p = this.world.landingBelow(this.x, this.y) || this.world.nearest(this.x, this.y)
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
    if (this.y >= b.y1 - 1 && this.y <= b.y2 + 1) return
    const y = clamp(this.y, b.y1, b.y2)
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

  speedPx() {
    return BASE_SPEED * (this.h / 56) * this.world.opts.speed * this.tempo
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

  /** A card to hop to: one step up or down, and only if it is wide enough. */
  pickHopTarget() {
    const ordered = this.world.platforms.slice().sort((a, b) => a.y - b.y)
    if (ordered.length < 2) return null
    const here = ordered.findIndex((p) => p.id === this.platformId)
    if (here < 0) return null
    const candidates = [ordered[here - 1], ordered[here + 1]]
      .filter((p) => p && p.x2 - p.x1 > this.w * 1.5)
    return candidates.length ? pick(candidates) : null
  }

  decide() {
    if (this.mode === 'free') {
      this.state = 'roam'
      this.target = null
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
    const hopWeight = this.world.platforms.length > 1 ? 0.10 + live * 0.16 : 0

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

        const p = this.world.landingBelow(this.x, prevY)
        if (p && this.y >= p.y) {
          this.platformId = p.id
          this.y = p.y
          this.vy = 0
          this.vx = 0
          this.state = 'land'
          this.timer = 0.3
          this.setClip('land')
          this.snap()
          break
        }

        // Fell past everything: put it back on the lowest card. With no cards
        // at all -- a layout mid-swap -- it is dropped in again from the top
        // rather than left to accelerate off the bottom of the world forever.
        if (this.y > this.world.h + 120) {
          const ground = this.world.lowest()
          if (ground) this.moveTo(ground, false)
          else { this.y = -20; this.vy = 0 }
        }
        break
      }

      case 'hop': {
        this.hopT += dt
        const t = Math.min(1, this.hopT / this.hopDur)
        this.x = this.hopX0 + (this.hopX1 - this.hopX0) * t
        this.y = (this.hopY0 + (this.hopY1 - this.hopY0) * t) - Math.sin(Math.PI * t) * this.hopPeak
        // Lean into the take-off and out of the landing.
        this.tilt = Math.cos(Math.PI * t) * 9 * this.dir
        if (t >= 1) {
          this.x = this.hopX1
          this.y = this.hopY1
          this.tilt = 0
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
        if (!p) { this.state = 'air'; this.vy = 0; this.setClip('air'); break }
        const pace = this.speedPx() * (this.state === 'run' ? 2.6 : 1)

        this.vxNow = this.dir * pace
        this.x += this.vxNow * dt
        this.y = p.y
        this.timer -= dt
        this.curiosity(dt)

        const lo = p.x1 + this.w * 0.4
        const hi = p.x2 - this.w * 0.4
        if (this.x <= lo || this.x >= hi) {
          this.x = clamp(this.x, lo, hi)
          this.dir *= -1
          // Pause at the end of the card before turning back.
          this.state = 'idle'
          this.timer = rand(0.4, 1.4)
          this.setClip('idle')
        } else if (this.timer <= 0) {
          if (this.world.opts.calm) { this.state = 'idle'; this.timer = 4; this.setClip('idle') } else this.decide()
        }
        break
      }

      default:
        this.state = 'idle'
        this.timer = 1
    }

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
      const p = this.world.landingBelow(this.x, this.y) || this.world.lowest()
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

    this.w = 0
    this.h = 0
    this.pets = []
    this.platforms = []
    this.cursor = { x: -9999, y: -9999, inside: false }

    this.running = false
    this.last = 0
    this.frame = this.frame.bind(this)
  }

  petHeight() { return this.opts.size * this.scale }

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
  setDisplays(list) {
    this.displays = (Array.isArray(list) ? list : []).filter((d) => (
      d && Number.isFinite(d.x) && Number.isFinite(d.y)
      && d.width > 0 && d.height > 0 && Number.isFinite(d.floor)
    ))
    for (const p of this.pets) {
      if (p.mode !== 'free') continue
      const b = p.roamBounds()
      p.x = clamp(p.x, b.x1, b.x2)
      p.y = clamp(p.y, b.y1, b.y2)
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
        pet.platformId = null
        pet.state = 'air'
        pet.vy = 0
        pet.vx = 0
        pet.setClip('air')
      }
    }
  }

  platformById(id) { return this.platforms.find((p) => p.id === id) || null }

  /** The nearest platform strictly below `y` whose span contains `x`. */
  landingBelow(x, y) {
    let best = null
    let bestY = Infinity
    for (const p of this.platforms) {
      if (p.y < y - 1) continue
      if (x < p.x1 - 24 || x > p.x2 + 24) continue
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

  start() {
    if (this.running) return
    this.running = true
    this.last = performance.now()
    requestAnimationFrame(this.frame)
  }

  stop() { this.running = false }

  frame(now) {
    if (!this.running) return
    const dt = Math.min(0.05, (now - this.last) / 1000)
    this.last = now

    for (const pet of this.pets) pet.update(dt)
    this.resolveMeetings()
    for (const pet of this.pets) pet.render()

    requestAnimationFrame(this.frame)
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
