import { createContext, useCallback, useContext, useEffect, useRef } from 'react'

/**
 * Keeps one DWM-acrylic window behind every card.
 *
 * DWM frosts a whole window rectangle and cannot be clipped to a region, so a
 * single window can only ever be a frosted rectangle. Cards that appear to
 * float over nothing therefore need one backing window each, and the gaps
 * between them are empty because no window covers them.
 *
 * Cards register their DOM node here; a single rAF-throttled pass measures all
 * of them together and ships one batch to the main process. Measuring per card,
 * or on every React render, would mean a layout flush per card per frame.
 */
const PaneCtx = createContext(null)

/**
 * The smallest card a pane may be made for, in the integers the main process
 * will use.
 *
 * Windows will not make a window shorter than about 39 device pixels, so a pane
 * asked for less does not shrink with its card -- it stands proud of it, a slab
 * of frost past the card edge. Nothing here can raise that floor, so anything
 * measuring below a couple of pixels is not a card a pane can honestly follow;
 * see `measure` for what happens to it.
 */
const MIN_EXTENT = 2

/**
 * How many passes after a resume are allowed past the identical-batch skip.
 *
 * One would do if the caller always resumed *after* the layout it resumed for
 * had landed. Two costs a single extra measurement on a transition that happens
 * when a human drags a scale control, and covers the case where the resume and
 * the layout change are dispatched from the same commit and the second frame is
 * the first one that can be measured honestly.
 */
const RESUME_PASSES = 2

/**
 * A card's box, in the pixels the window is actually made of.
 *
 * This was an offsetTop/offsetLeft walk, because those are layout values and so
 * immune to a transform: the entrance animation used to travel, and a card
 * measured mid-flight put its pane 6px out with nothing to correct it
 * afterwards — a transform changes no box size, so the ResizeObserver never
 * fires again.
 *
 * Two things retired that. The entrance is opacity-only now (see `nw-rise`),
 * for a separate reason — a pane cannot follow a tween — so no card is ever
 * transformed. And the widget can be scaled by hand, which the offset values
 * cannot see: under CSS `zoom` they keep reporting the *unscaled* layout, and
 * multiplying them back up is not the same number the browser painted. At 1.4
 * that was a pixel out on half the cards, which is a hairline of frost showing
 * past the card edge. The widget now scales far further in both directions than
 * it did when that was discovered, so the gap between the two numbers is wider
 * than ever: this must stay the visual box.
 *
 * So: the visual box, which is what a pane has to match. The cost is that a
 * card must never be given a transform — an entrance that travels, a hover that
 * nudges — without the panes being reconsidered.
 *
 * The result is rounded here rather than left as the browser's fractions,
 * because the main process rounds it anyway before handing it to SetWindowPos.
 * Rounding on this side means the decision about whether a box is too small to
 * be a window is made on the same integers the window will be made from, and it
 * means sub-pixel jitter under an awkward zoom factor no longer produces a
 * batch that differs from the last one without differing from it on screen.
 */
function layoutRect(node) {
  const r = node.getBoundingClientRect()
  return {
    x: Math.round(r.left),
    y: Math.round(r.top),
    w: Math.round(r.width),
    h: Math.round(r.height),
  }
}

/** A rect the main process could actually build a window from. */
function usable(r) {
  return Number.isFinite(r.x) && Number.isFinite(r.y)
    && r.w >= MIN_EXTENT && r.h >= MIN_EXTENT
}

export function PaneProvider({ children }) {
  const nodes = useRef(new Map())
  const frame = useRef(0)
  const last = useRef('')
  /**
   * The last batch, and who else wants to see it.
   *
   * The pets walk on the tops of these same rectangles, and they need them in
   * the same coordinates and at the same moment the frost does -- a pet reading
   * a card's position from a second, later measurement would visibly lag the
   * card it is standing on whenever the layout moved. So the one pass that
   * already runs feeds both.
   */
  const rects = useRef([])
  const subs = useRef(new Set())
  /**
   * Whether `rects` is still a description of the screen.
   *
   * Only `watch` reads it. An existing subscriber keeps whatever it was last
   * handed, but a subscriber that attaches while the pass is stopped must not
   * be replayed a batch that is known to have been overtaken -- a pet layer
   * mounting mid-resize would build its ground out of it.
   */
  const current = useRef(false)

  /**
   * Two independent reasons to stop measuring, and neither may cancel the
   * other.
   *
   * `held` is the original one: while a layout is animating, every measurement
   * is of a shape the cards are only passing through. Sending those would march
   * the panes across the screen a frame behind, which is the one thing the
   * frost cannot do smoothly.
   *
   * `frosted` is the mode. Below roughly the scale at which the thinnest card
   * reaches the platform's minimum window height, per-card acrylic cannot
   * follow its card at all, so the panes are stood down entirely and the cards
   * paint their own background in CSS instead. That state lasts as long as the
   * widget is that small, which is indefinitely, and it must survive every
   * animation that happens meanwhile -- a mode switch that suspends and resumes
   * for its tween must not hand the frost back at a size it cannot be drawn at.
   * Hence two latches and not one flag: the pass runs only when both are clear.
   */
  const held = useRef(false)
  const frosted = useRef(false)
  const stopped = useCallback(() => held.current || frosted.current, [])

  /**
   * Passes that may ignore the identical-batch skip.
   *
   * Set when the pass restarts. The cached signature is stale by definition at
   * that point -- the layout changed while the pass was stopped, which is why
   * it was stopped -- so the one thing that must not happen is the skip
   * deciding the new batch matches and suppressing it, leaving the panes hidden
   * or, worse, back at the position they had before the resize.
   */
  const force = useRef(0)

  /**
   * Measure every registered card, in visual order, dropping what cannot be a
   * window.
   *
   * At the small end of the scale range a card's box can round to zero, or to
   * one pixel, or the card can be display:none for a frame while a view swaps.
   * Such a rect is *dropped*, not clamped and not sent. Clamping would invent a
   * size the card does not have, and since the platform floor is around 39
   * device pixels anyway, the window that came back would be a slab of frost
   * standing proud of a card that is barely there -- the exact artefact the
   * suspend mode exists to remove. Sending it raw is worse: the main process
   * clamps a size to at least 1, so a zero arrives as a real window, and a
   * negative one is undefined behaviour at the SetWindowPos call. A card with
   * no pane simply falls back to its CSS background, which is the same fallback
   * the suspended mode uses for every card, so the degenerate case degrades
   * into a state the widget already knows how to look right in.
   *
   * Dropping silently is how this would become a bug nobody can find, so the
   * drop is announced -- once per change of which cards are affected, because
   * this can run every frame and a per-frame warning is its own outage.
   */
  const dropped = useRef('')
  const measure = useCallback(() => {
    const batch = []
    const bad = []
    for (const [id, node] of nodes.current) {
      if (!node || !node.isConnected) continue
      const r = layoutRect(node)
      if (!usable(r)) { bad.push(`${id}:${r.w}x${r.h}`); continue }
      batch.push({ id, ...r })
    }
    const sig = bad.join(' ')
    if (sig !== dropped.current) {
      dropped.current = sig
      if (sig) console.warn('[panes] no pane for degenerate card rect:', sig)
    }
    // Visual order, not registration order. Panes are handed out by position in
    // this array, and the map is ordered by when each card mounted -- so a card
    // that survives a mode switch (the footer) sorts ahead of ones that remount,
    // and every pane gets reassigned to a different card. The set of rectangles
    // is the same either way, so nothing looked wrong, but it moved every window
    // instead of the few that actually changed.
    batch.sort((a, b) => a.y - b.y || a.x - b.x)
    return batch
  }, [])

  /**
   * One pass: measure, tell the subscribers, tell the main process.
   *
   * `quiet` runs the pass for the subscribers alone. It exists for the moment
   * the pass stops: the pets are standing on cards that are still on screen,
   * still painted, merely no longer frosted by a window, so the last thing they
   * are told should be true at the moment they stop being told anything.
   * Nothing goes to the main process from a quiet pass, because the panes are
   * being hidden in the same breath and a rect arriving after that would show
   * them again.
   */
  const flush = useCallback((quiet = false) => {
    frame.current = 0
    if (stopped() && !quiet) return
    const batch = measure()

    // Card geometry is stable between data updates, and every send costs an IPC
    // hop plus a SetWindowPos per pane. Skipping an identical batch is what
    // keeps an idle widget genuinely idle.
    const sig = JSON.stringify(batch)
    const forced = force.current > 0
    if (forced) force.current -= 1
    if (!forced && sig === last.current) return
    rects.current = batch
    current.current = true
    for (const fn of subs.current) fn(batch)
    if (quiet) {
      // A quiet pass sent nothing, so the next real one must not think the main
      // process has already seen this batch.
      last.current = ''
      return
    }
    last.current = sig
    window.nw?.panes(batch)
  }, [measure, stopped])

  /**
   * Ask for a pass on the next frame.
   *
   * While the pass is stopped this does not merely discard the result, it never
   * requests the frame: a measurement is a forced layout flush, and doing one
   * per frame to throw it away is precisely the cost this whole layer was built
   * to avoid. It matters most in the suspended mode, which is what a machine
   * running the widget very small is most likely to be busy during. What is
   * remembered instead is that somebody wanted one, which is enough, because
   * the restart measures unconditionally.
   */
  const schedule = useCallback(() => {
    if (stopped()) { current.current = false; return }
    if (frame.current) return
    // Wrapped rather than passed straight to rAF: the callback is handed a
    // timestamp, which would arrive here as a truthy `quiet`.
    frame.current = requestAnimationFrame(() => flush())
  }, [flush, stopped])

  /**
   * Hide every pane now, and guarantee the next measurement is sent.
   *
   * Called before a mode switch re-renders. The cached signature has to be
   * cleared too: if a layout ever came back byte-identical, the skip above
   * would suppress the send and the panes would stay hidden for good.
   */
  const blank = useCallback(() => {
    last.current = ''
    window.nw?.hidePanes()
  }, [])

  /**
   * Stop the pass, hide what is on screen, and leave the subscribers with a
   * picture that was true when the lights went out.
   */
  const stop = useCallback(() => {
    if (frame.current) {
      cancelAnimationFrame(frame.current)
      frame.current = 0
    }
    // Measured before the hide and delivered to the subscribers only. The cards
    // do not move when the frost goes away, so this is the pets' ground for as
    // long as the pass is down.
    flush(true)
    current.current = false
    last.current = ''
    window.nw?.hidePanes()
    // hidePanes is a one-shot that the next batch to reach the main process
    // undoes. The latch is what makes the stand-down survive every measurement
    // in between, including one sent by a subscriber or a race we did not see.
    window.nw?.frostSuspend(true)
  }, [flush])

  /**
   * Restart the pass with a measurement that cannot be skipped.
   *
   * Nothing between `stop` and the first of these passes reaches the main
   * process, so the frost cannot come back at the geometry it had before
   * whatever the pass was stopped for. The pass is deliberately deferred to a
   * frame rather than run here: a caller that flips the mode and restarts in
   * the same tick has not necessarily had its new layout applied, and measuring
   * it then would send exactly the stale rectangle this is trying to prevent.
   * rAF runs after layout and before the frame is painted, so the fresh batch
   * is out before anything can be seen at the old position.
   */
  const start = useCallback(() => {
    // Lifted before anything is measured: the main process drops rects while
    // the latch is set, so a batch sent first would be thrown away and the
    // frost would not come back until something else happened to move.
    window.nw?.frostSuspend(false)
    force.current = RESUME_PASSES
    schedule()
    // The second of the forced passes. Queued behind the first so that a layout
    // which only settles a frame later is still caught, and free when it agrees
    // with the first because the pass sends nothing it has already sent.
    requestAnimationFrame(() => { if (!stopped()) schedule() })
  }, [schedule, stopped])

  const apply = useCallback((latch, on) => {
    const want = on !== false
    if (latch.current === want) return
    latch.current = want
    if (want) stop()
    else if (!stopped()) start()
  }, [start, stop, stopped])

  /** Stop reporting geometry around an animation. `suspend(false)` resumes. */
  const suspend = useCallback((on) => apply(held, on), [apply])
  /** Resume after an animation. */
  const resume = useCallback(() => apply(held, false), [apply])

  /**
   * The frost-suspend switch, and the contract with the scale logic.
   *
   * `setFrostSuspended(true)` when the widget is drawn too small for a pane to
   * follow its card, `false` when it is not. Idempotent in both directions, so
   * it can be called from an effect that re-runs on every scale change without
   * any of the no-op calls costing a measurement, and it takes anything
   * boolean-ish: no argument means suspend, which is the reading that does the
   * least harm if a caller gets it wrong.
   */
  const setFrostSuspended = useCallback((on) => apply(frosted, on), [apply])

  /**
   * Every card is watched individually, not just the document.
   *
   * Watching only the root catches anything that changes the widget's overall
   * size, which is most things -- and silently misses a card that changes height
   * without the column doing so. Its frost then keeps the height it had when the
   * last batch went out. At scale 1 that was invisible, because the numbers
   * happened to round the same way; at 0.85 two cards sat under frost two pixels
   * too tall.
   */
  const observer = useRef(null)

  const register = useCallback((id, node) => {
    const prev = nodes.current.get(id)
    if (prev && observer.current) observer.current.unobserve(prev)
    if (node) {
      nodes.current.set(id, node)
      observer.current?.observe(node)
    } else {
      nodes.current.delete(id)
    }
    schedule()
  }, [schedule])

  useEffect(() => {
    // Cards register during commit, which is before this runs, so the ones
    // already on screen have to be picked up rather than waited for.
    const ro = new ResizeObserver(schedule)
    observer.current = ro
    ro.observe(document.documentElement)
    for (const node of nodes.current.values()) if (node) ro.observe(node)
    window.addEventListener('resize', schedule)
    return () => {
      ro.disconnect()
      observer.current = null
      window.removeEventListener('resize', schedule)
      if (frame.current) cancelAnimationFrame(frame.current)
    }
  }, [schedule])

  /**
   * Watch the measured card rectangles. Returns an unsubscribe.
   *
   * A subscriber that attaches while there is a batch worth having gets it at
   * once, so a pet layer mounting between passes has ground to stand on. If the
   * batch on hand is stale or there is none, it gets a fresh measurement rather
   * than nothing -- see below for why nothing was the wrong answer.
   */
  const watch = useCallback((fn) => {
    subs.current.add(fn)
    // Nothing worth replaying, so measure once for this subscriber.
    //
    // The old reading -- hand it nothing, it keeps what it had -- holds for a
    // subscriber that has been running. It is empty for one that has just
    // mounted, and for the pets "no ground" is not "keep what you had", it is
    // falling through the widget forever. That is not a corner: the frost pass
    // is stood down for as long as the widget is drawn too small to frost, and
    // `schedule` marks the batch stale on the way past, so every pet layer that
    // mounts at that size mounts into it.
    //
    // A measurement is a forced layout flush and this is one of them, on
    // subscribe, not one per frame. The cards are on screen and measurable
    // whether or not anything is frosting them.
    if (!current.current || !rects.current.length) {
      rects.current = measure()
      current.current = rects.current.length > 0
    }
    if (rects.current.length) fn(rects.current)
    return () => subs.current.delete(fn)
  }, [measure])

  return (
    <PaneCtx.Provider value={{
      register, schedule, blank, suspend, resume, setFrostSuspended, watch,
    }}>
      {children}
    </PaneCtx.Provider>
  )
}

/** Returns a ref callback to attach to a card's outermost element. */
export function usePane(id) {
  const ctx = useContext(PaneCtx)
  return useCallback((node) => {
    ctx?.register(id, node)
  }, [ctx, id])
}

/** Re-measure after something that changes layout but not the card list. */
export function usePaneSync(dep) {
  const ctx = useContext(PaneCtx)
  useEffect(() => { ctx?.schedule() }, [ctx, dep])
}

/** Hide every pane until the next measurement lands. */
export function usePaneBlank() {
  const ctx = useContext(PaneCtx)
  return useCallback(() => ctx?.blank(), [ctx])
}

/**
 * Subscribe to the measured card rectangles.
 *
 * `fn` must be stable, or every render resubscribes. The pet layer holds it in
 * a ref for exactly that reason.
 */
export function usePaneRects(fn) {
  const ctx = useContext(PaneCtx)
  useEffect(() => ctx?.watch(fn), [ctx, fn])
}

/**
 * Suspend and resume geometry reporting around an animation.
 *
 * `suspend()` also takes a boolean, so the pair can be driven from one value.
 * `setSuspended` is the same latch under the name a caller reaches for when it
 * has a flag rather than two events.
 */
export function usePaneHold() {
  const ctx = useContext(PaneCtx)
  return {
    suspend: useCallback((on) => ctx?.suspend(on), [ctx]),
    resume: useCallback(() => ctx?.resume(), [ctx]),
    setSuspended: useCallback((on) => ctx?.suspend(on), [ctx]),
  }
}

/**
 * The frost-suspend switch: `setFrostSuspended(true)` stands the acrylic panes
 * down, `false` brings them back with a fresh measurement.
 *
 * This is the one the scale logic wants. It is a separate latch from the
 * animation hold above, so an animation that suspends and resumes underneath it
 * cannot hand the frost back while the widget is still too small to wear it.
 */
export function usePaneSuspend() {
  const ctx = useContext(PaneCtx)
  return useCallback((on) => ctx?.setFrostSuspended(on), [ctx])
}

/** The same switch, under the name the scale side of this calls it. */
export const useFrostSuspend = usePaneSuspend
