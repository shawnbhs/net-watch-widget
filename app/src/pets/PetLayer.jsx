import { useCallback, useEffect, useRef } from 'react'
import { usePaneRects } from '../panes.jsx'
import { ASSET_BASE, World, bindPointer } from './engine.js'
import { usePets } from './store.jsx'

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
    w.setSize(window.innerWidth, window.innerHeight)
    w.start()

    const onResize = () => w.setSize(window.innerWidth, window.innerHeight)
    window.addEventListener('resize', onResize)

    // Cursor reaction needs the pointer wherever it is, but the layer only
    // *receives* events over a pet -- that is the whole point of it. So the
    // position is read from a window listener, which observes without taking
    // anything away from the cards underneath.
    const onMove = (e) => w.setCursor(e.clientX, e.clientY, true)
    const onLeave = () => { w.cursor.inside = false }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseleave', onLeave)

    return () => {
      window.removeEventListener('resize', onResize)
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
        .filter((r) => r.y > top + 1)
        .map((r) => ({ id: r.id, x1: r.x + 2, x2: r.x + r.w - 2, y: r.y })),
    )
  }, [])
  usePaneRects(onRects)

  // ── the roster ──────────────────────────────────────────────────────────────

  const wanted = pets.filter((p) => p.mode === 'widget')

  useEffect(() => {
    const w = world.current
    if (!w || !ready) return
    if (!enabled) { w.clear(); return }

    const keep = new Set()
    for (const row of wanted) {
      keep.add(row.id)
      const live = w.byId(row.id)
      if (live) { live.setMode('platform'); continue }
      const full = resolve(row)
      if (!full) continue
      // New arrivals drop in from above rather than appearing. A pet that
      // materialises on a card looks like a rendering bug; one that falls onto
      // it reads as having just turned up -- and falling is already a state the
      // engine has, so it lands on whatever card is under where it came down.
      const pet = w.add({
        id: row.id,
        species: full.species,
        variant: full.variant,
        mode: 'platform',
        platformId: null,
        x: w.w * (0.25 + Math.random() * 0.5),
        y: -20,
      })
      pet.state = 'air'
      pet.vy = 0
      pet.setClip('air')
    }
    for (const live of [...w.pets]) if (!keep.has(live.id)) w.remove(live.id)
  }, [wanted.map((p) => `${p.id}:${p.speciesId}:${p.color}`).join('|'), ready, enabled, resolve])

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
    <div
      ref={host}
      className="nw-pets"
      aria-hidden="true"
      style={{ visibility: enabled ? 'visible' : 'hidden' }}
    />
  )
}
