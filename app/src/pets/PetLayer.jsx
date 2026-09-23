import { useCallback, useEffect, useRef, useState } from 'react'
import { usePaneRects } from '../panes.jsx'
import { ASSET_BASE, World, bindPointer } from './engine.js'
import { usePets } from './store.jsx'

/**
 * Mirrors `LEVEL_EPS` in engine.js, which is not exported. The engine treats
 * two platforms within this many pixels as the same level, so the topmost-card
 * exclusion below has to use the same tolerance or a card the engine considers
 * level with the topmost one would still be handed over as ground.
 */
const LEVEL_EPS = 2

/**
 * The pets that live in the widget.
 *
 * A fixed, full-window layer that is transparent to the mouse everywhere except
 * on a pet, sitting *outside* the shell's `zoom` so its coordinates are the same
 * ones the cards report -- `getBoundingClientRect` values, which is what the
 * frost panes are positioned from too. Putting it inside the shell instead would
 * have it measuring in unscaled layout units while the cards it walks on are
 * drawn in scaled ones, and the two only agree at scale 1.
 *
 * The ground is the top edge of every card except the topmost one. The
 * exclusion is not arbitrary: a pet's body reaches *up* from its feet, so it
 * needs a card above it to overlap. On the topmost card there is nothing up
 * there but the edge of a window that is sized to exactly its content, and the
 * pet would be sliced in half by it.
 */
export function PetLayer({ enabled, scale }) {
  const { pets, opts, resolve, ready, swapMode } = usePets()
  const host = useRef(null)
  const world = useRef(null)

  // ── the world ───────────────────────────────────────────────────────────────

  useEffect(() => {
    const stage = host.current
    if (!stage) return undefined

    const w = new World({
      stage,
      assetBase: ASSET_BASE,
      opts,
      scale,
    })
    world.current = w

    // The world box is this layer's own border box, not `window.innerWidth` /
    // `innerHeight`. The two are not the same number: the window values include
    // the scrollbar gutter, while `.nw-pets` is the box `overflow: hidden`
    // actually clips to. Measured in Chromium at a 300x300 viewport with the
    // document overflowing -- which is exactly the transient state a drag-
    // resize passes through, content wider than the window that has not caught
    // up yet -- `innerHeight` reported 300 and the layer measured 285. That
    // fifteen-pixel strip is world the engine would place a pet into and the
    // clip would then slice off. The engine early-returns on an unchanged size,
    // so handing it the wrong box fails silently.
    let sizeRaf = 0
    const measure = () => {
      sizeRaf = 0
      const r = stage.getBoundingClientRect()
      // A layer that is not being displayed measures 0x0; taking that would
      // collapse the world and strand every pet at the origin on the way back.
      if (r.width > 0 && r.height > 0) w.setSize(r.width, r.height)
    }
    // Coalesced to one measurement per frame: a drag-resize fires a burst of
    // these, and each one walks every pet.
    const schedule = () => { if (!sizeRaf) sizeRaf = requestAnimationFrame(measure) }

    const ro = new ResizeObserver(schedule)
    ro.observe(stage)
    measure()

    // Not a fallback: on a real viewport resize Chromium dispatches this event
    // *before* it delivers the observation, so this is normally the first
    // notice of a new size and the observer is the one confirming it. Both are
    // kept because neither covers the other -- the observer also catches a
    // layout-only change that fires no resize event. They share the coalescer,
    // so the pair still costs one measurement per frame, not two.
    window.addEventListener('resize', schedule)

    // Cursor reaction needs the pointer wherever it is, but the layer only
    // *receives* events over a pet -- that is the whole point of it. So the
    // position is read from a window listener, which observes without taking
    // anything away from the cards underneath.
    const onMove = (e) => w.setCursor(e.clientX, e.clientY, true)
    const onLeave = () => { w.cursor.inside = false }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseleave', onLeave)

    return () => {
      ro.disconnect()
      if (sizeRaf) cancelAnimationFrame(sizeRaf)
      window.removeEventListener('resize', schedule)
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseleave', onLeave)
      w.stop()
      w.clear()
      world.current = null
    }
  }, [])                              // built once; everything else is pushed in

  useEffect(() => { world.current?.setOpts(opts) }, [opts])
  useEffect(() => { world.current?.setScale(scale) }, [scale])

  // ── the ground ──────────────────────────────────────────────────────────────

  const onRects = useCallback((rects) => {
    const w = world.current
    if (!w) return
    if (!rects.length) { w.setPlatforms([]); return }
    // Sorted by y already, so the first is the topmost -- and a pair of cards
    // side by side shares a y, hence the tolerance rather than an index.
    const top = rects[0].y
    w.setPlatforms(
      rects
        .filter((r) => r.y > top + LEVEL_EPS)
        .map((r) => ({ id: r.id, x1: r.x + 2, x2: r.x + r.w - 2, y: r.y })),
    )
  }, [])
  usePaneRects(onRects)

  // ── the roster ──────────────────────────────────────────────────────────────

  const wanted = pets.filter((p) => p.mode === 'widget')

  // Pets the roster claims live here but whose species cannot be resolved. They
  // are in neither world -- the overlay only takes `mode === 'screen'` -- so
  // without this they would exist in the settings list and nowhere else, which
  // reads as "the app lost my pet" rather than "this saved pet is broken".
  const [orphans, setOrphans] = useState([])
  // How many pets are actually alive in this world; drives the run/idle gate
  // below. Roster length will not do: an unresolvable pet is never added.
  const [alive, setAlive] = useState(0)

  useEffect(() => {
    const w = world.current
    if (!w || !ready) return
    if (!enabled) { w.clear(); setAlive(0); setOrphans([]); return }

    const keep = new Set()
    const missing = []
    for (const row of wanted) {
      const live = w.byId(row.id)
      // No setMode here: every pet in this world is added as 'platform' and
      // nothing else ever changes it, so the call could only ever be a no-op.
      if (live) { keep.add(row.id); live.setSizeFactor(row.size); continue }
      const full = resolve(row)
      // Marked as kept only once it is really going to be here. Adding to
      // `keep` before this guard made an unresolvable pet look present to the
      // removal pass while it had never been added at all.
      if (!full) { missing.push(row); continue }
      keep.add(row.id)
      // New arrivals drop in from above rather than appearing. A pet that
      // materialises on a card looks like a rendering bug; one that falls onto
      // it reads as having just turned up -- and falling is already a state the
      // engine has, so it lands on whatever card is under where it came down.
      try {
        const pet = w.add({
          id: row.id,
          species: full.species,
          variant: full.variant,
          mode: 'platform',
          platformId: null,
          size: row.size,
          x: w.w * (0.25 + Math.random() * 0.5),
          y: -20,
        })
        pet.state = 'air'
        pet.vy = 0
        pet.setClip('air')
      } catch (err) {
        // One malformed pet aborting the effect left the roster half-applied.
        console.error('[pets] could not add', row.id, err)
        keep.delete(row.id)
        missing.push(row)
      }
    }
    // The removal pass is deliberately outside the per-pet guard above: a pet
    // that cannot be constructed must not take the rest of the roster --
    // including pets the user just deleted -- down with it.
    for (const live of [...w.pets]) {
      if (keep.has(live.id)) continue
      try { w.remove(live.id) } catch (err) { console.error('[pets] remove failed', live.id, err) }
    }
    setAlive(w.pets.length)
    setOrphans(missing.map((p) => ({ id: p.id, name: p.name || p.id, speciesId: p.speciesId })))
  }, [
    // `mode` is part of the key even though `wanted` is already filtered by it:
    // without it, "moved to the overlay" and "deleted" are the same edit here.
    wanted.map((p) => `${p.id}:${p.mode}:${p.speciesId}:${p.color}:${p.size}`).join('|'),
    ready, enabled, resolve,
  ])

  // ── run / idle ──────────────────────────────────────────────────────────────

  // Hiding the layer used to leave the engine's rAF loop running forever. This
  // effect is the only thing that starts or stops it after construction, and it
  // re-runs whenever `alive` changes, so a world that was stopped while empty
  // comes back the moment a pet is added to it.
  useEffect(() => {
    const w = world.current
    if (!w) return
    if (enabled && alive > 0) w.start()
    else w.stop()
  }, [enabled, alive, ready])

  // ── gestures ────────────────────────────────────────────────────────────────

  const swapRef = useRef(swapMode)
  swapRef.current = swapMode

  useEffect(() => {
    const w = world.current
    if (!w) return undefined
    return bindPointer(w, {
      onGesture: (kind, pet) => { if (kind === 'swap') swapRef.current(pet.id) },
    })
  }, [])

  return (
    <>
      <div
        ref={host}
        className="nw-pets"
        aria-hidden="true"
        // `display`, not `visibility`: a hidden-but-laid-out layer still had a
        // size, so nothing ever told the engine it was idle. This also makes
        // the host measure 0x0 while off, which the size observer skips.
        style={{ display: enabled ? 'block' : 'none' }}
        data-pet-count={alive}
      />
      {enabled && orphans.length > 0 && (
        <div
          role="status"
          // Inline-styled on purpose: index.css is owned elsewhere and this is
          // a diagnostic surface, not part of the widget's design. No font
          // size of its own -- it inherits -- and it wraps rather than clips,
          // because a truncated pet name is exactly the identifying detail.
          style={{
            position: 'fixed',
            left: 8,
            bottom: 8,
            zIndex: 41,
            maxWidth: 'calc(100% - 16px)',
            padding: '4px 8px',
            borderRadius: 6,
            background: 'rgba(120, 32, 32, 0.82)',
            color: '#fff',
            pointerEvents: 'none',
            whiteSpace: 'normal',
            overflowWrap: 'anywhere',
          }}
        >
          {orphans.length === 1 ? 'A pet could not be loaded: ' : 'Some pets could not be loaded: '}
          {orphans.map((o) => `${o.name} (${o.speciesId || 'no species'})`).join(', ')}
        </div>
      )}
    </>
  )
}
