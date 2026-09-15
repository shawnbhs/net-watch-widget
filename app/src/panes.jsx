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
 * past the card edge.
 *
 * So: the visual box, which is what a pane has to match. The cost is that a
 * card must never be given a transform — an entrance that travels, a hover that
 * nudges — without the panes being reconsidered.
 */
function layoutRect(node) {
  const r = node.getBoundingClientRect()
  return { x: r.left, y: r.top, w: r.width, h: r.height }
}

export function PaneProvider({ children }) {
  const nodes = useRef(new Map())
  const frame = useRef(0)
  const last = useRef('')
  // While a layout is animating, every measurement is of a shape the cards are
  // only passing through. Sending those would march the panes across the screen
  // a frame behind, which is the one thing the frost cannot do smoothly.
  const suspended = useRef(false)

  const flush = useCallback(() => {
    frame.current = 0
    if (suspended.current) return
    const rects = []
    for (const [id, node] of nodes.current) {
      if (!node || !node.isConnected) continue
      const r = layoutRect(node)
      if (r.w < 2 || r.h < 2) continue
      rects.push({ id, ...r })
    }

    // Visual order, not registration order. Panes are handed out by position in
    // this array, and the map is ordered by when each card mounted -- so a card
    // that survives a mode switch (the footer) sorts ahead of ones that remount,
    // and every pane gets reassigned to a different card. The set of rectangles
    // is the same either way, so nothing looked wrong, but it moved every window
    // instead of the few that actually changed.
    rects.sort((a, b) => a.y - b.y || a.x - b.x)
    // Card geometry is stable between data updates, and every send costs an IPC
    // hop plus a SetWindowPos per pane. Skipping an identical batch is what
    // keeps an idle widget genuinely idle.
    const sig = JSON.stringify(rects)
    if (sig === last.current) return
    last.current = sig
    window.nw?.panes(rects)
  }, [])

  const schedule = useCallback(() => {
    if (frame.current) return
    frame.current = requestAnimationFrame(flush)
  }, [flush])

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

  /** Stop reporting geometry, and hide what is on screen, until `resume`. */
  const suspend = useCallback(() => {
    suspended.current = true
    last.current = ''
    window.nw?.hidePanes()
  }, [])

  /** Measure once, now that the layout has settled, and bring the frost back. */
  const resume = useCallback(() => {
    suspended.current = false
    schedule()
  }, [schedule])

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

  return (
    <PaneCtx.Provider value={{ register, schedule, blank, suspend, resume }}>
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

/** Suspend and resume geometry reporting around an animation. */
export function usePaneHold() {
  const ctx = useContext(PaneCtx)
  return {
    suspend: useCallback(() => ctx?.suspend(), [ctx]),
    resume: useCallback(() => ctx?.resume(), [ctx]),
  }
}
