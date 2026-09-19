import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { PaneProvider, usePaneSync, usePaneHold } from './panes.jsx'
import { useSidecar, send } from './useSidecar.js'
import { Card, CardHead, Cell, Figure, Pair, Pill, PrimaryButton, Row } from './components/Glass.jsx'
import { Bar, pctTone } from './components/Bar.jsx'
import { Ring } from './components/Ring.jsx'
import { IconButton, NetDot } from './components/Footer.jsx'
import { SectionIcon } from './components/Icons.jsx'
import { CountryChip } from './components/Country.jsx'
import { PetsCard } from './components/Pets.jsx'
import { PetsProvider, useOverlaySync } from './pets/store.jsx'
import { PetLayer } from './pets/PetLayer.jsx'

export default function App() {
  return (
    <PaneProvider>
      <PetsProvider>
        <Widget />
      </PetsProvider>
    </PaneProvider>
  )
}

const BOOT = Date.now()
const INTRO_MS = 900

/**
 * Size the window to a measured element.
 *
 * `tab` is the mode, not a hint. The main process positions a tab from its own
 * width and restores the remembered free position when one stops being a tab,
 * so this flag is what actually moves the window between the two -- which is
 * why the size and the mode travel together in a single message.
 *
 * `delta` sizes for a height the page has not reached yet; see ModeBox.
 */
/**
 * A usable widget size, whatever it is handed.
 *
 * The floor is a platform limit, not taste. A card's frost is a window, and
 * **Windows will not make a window shorter than 39px** -- measured directly, and
 * unmoved by `minHeight: 1` or by making it resizable. The two thinnest cards,
 * the title and the footer, are 43px, so below about 0.90 their frost stands
 * proud of them by a pixel or two. 0.92 is the first hundredth that measures
 * clean on every card.
 *
 * The ceiling is not a taste limit either, and it is no longer 2x. What the
 * widget is actually allowed to grow to is whatever the monitor can hold --
 * `fitScale` measures that, and on every real display it binds long before the
 * number below does. SCALE_MAX is only the rail that catches a nonsense
 * measurement: a card caught mid-layout reporting a 1px box would otherwise
 * produce a fit in the thousands and a window the size of a city.
 *
 * It was 2 because the full view stops fitting a 1080p screen at about there,
 * which is a true statement about one layout on one monitor and was never a
 * reason the *compact* view or the tab could not go further. On this 1440p
 * screen the full view fits at ~1.7 and is limited by the screen either way;
 * the shorter views are the ones that were being held back.
 */
const SCALE_MIN = 0.79
const SCALE_MAX = 12

/**
 * The ceiling used when there is no usable fitted measurement.
 *
 * Two quite different cases, both meaning "the screen has not told us anything
 * we can grow by": the layout has not been measured yet, and the layout was
 * measured and does not fit even at the floor. Neither should hand the person
 * the full rail -- an unmeasured widget is not licence to zoom to 12x -- so
 * both fall back to what the ceiling used to be, which is known to be sane.
 */
const SCALE_MAX_UNFITTED = 2

/**
 * Where the widget starts when nothing has been remembered.
 *
 * A touch over 1:1. The panel's type is small by design, which on a high-DPI
 * screen reads as cramped on first launch; a few percent fixes that without
 * the result looking like it was scaled by accident.
 */
const SCALE_DEFAULT = 1.08

/**
 * Screen edge left clear when fitting the widget to the monitor.
 *
 * Enough that the widget is never flush against the taskbar or the top of the
 * screen at its largest, and enough to absorb the pixel or two that rounding a
 * zoomed layout back to whole device pixels can add.
 */
const FIT_MARGIN = 48

/**
 * The ceiling a fitted measurement is actually allowed to impose.
 *
 * A fitted maximum below the floor is not a ceiling, it is a screen that cannot
 * hold this layout at any size it still renders correctly at -- the full view on
 * a high-DPI monitor is exactly that. Honouring such a number would pin the
 * ceiling onto the floor and leave the range a single point, which is what froze
 * the resize drag in the full view while the shorter compact view kept working.
 *
 * So a ceiling that has collapsed past the floor is discarded rather than
 * clamped up: the widget is going to overhang whatever is chosen, so the choice
 * is handed back to the person making it and the whole range stays reachable.
 */
function scaleCeiling(max) {
  const fitted = Math.min(SCALE_MAX, Number(max) || SCALE_MAX_UNFITTED)
  return fitted >= SCALE_MIN ? fitted : SCALE_MAX_UNFITTED
}

function clampScale(v, max = SCALE_MAX) {
  const n = Number(v)
  if (!Number.isFinite(n)) return SCALE_DEFAULT
  // The floor wins over the fitted ceiling: a screen too small for even the
  // minimum is still better served by a widget that renders correctly and
  // overhangs than by one squeezed below where its frost stops matching. When
  // that happens the fitted ceiling is dropped entirely rather than folded onto
  // the floor, so the drag still has somewhere to go -- see scaleCeiling.
  const ceiling = scaleCeiling(max)
  return Math.min(ceiling, Math.max(SCALE_MIN, Math.round(n * 100) / 100))
}

/**
 * The work area of the monitor the widget is on, as the main process last
 * reported it.
 *
 * Module scope rather than state because it is not rendered -- it only feeds
 * the next measurement -- and because the listener that maintains it is torn
 * down and rebuilt every time the view changes, which a value living inside
 * that effect would not survive.
 *
 * `window.screen` would seem to be the obvious source and cannot be trusted
 * for this: dragging the widget between two monitors of the same scale factor
 * leaves `availWidth`/`availHeight` reporting the screen it came from, so the
 * fitted ceiling never moves. Electron's main process reads the OS directly.
 */
let screenArea = null

/**
 * The largest scale at which the widget still fits the monitor it is on.
 *
 * `screen.availWidth/availHeight` are the work area of the display the window
 * is currently on, already in CSS pixels, so the whole calculation can be done
 * here without asking the main process anything. The unscaled content size is
 * the measured box divided back by the scale it was drawn at -- `zoom` is a
 * layout scale, so the rect is already multiplied by it.
 *
 * Rounded *down* to the hundredth the slider works in, so the fitted maximum
 * is always a size that fits rather than the first one that does not.
 *
 * The result is the *true* fit and is deliberately not raised to SCALE_MIN: a
 * layout taller than the screen genuinely fits at nothing the widget can draw,
 * and saying so honestly is what lets the consumer tell that case apart from a
 * real ceiling. scaleCeiling is where that decision is made.
 */
function fitScale(node, scale) {
  const r = node?.getBoundingClientRect()
  if (!r) return SCALE_MAX_UNFITTED
  const w = r.width / scale
  const h = r.height / scale
  if (!(w > 0) || !(h > 0)) return SCALE_MAX_UNFITTED
  const availW = (screenArea?.width ?? window.screen?.availWidth ?? window.innerWidth) - FIT_MARGIN
  const availH = (screenArea?.height ?? window.screen?.availHeight ?? window.innerHeight) - FIT_MARGIN
  const fit = Math.min(availW / w, availH / h)
  if (!Number.isFinite(fit)) return SCALE_MAX_UNFITTED
  return Math.min(SCALE_MAX, Math.floor(fit * 100) / 100)
}

/**
 * Keeps the fitted ceiling current.
 *
 * Re-measured on three things. A window resize. A `mode` change, because the
 * full view is a great deal taller than the compact one and a limit computed
 * from the wrong layout is the whole bug: scaled up with the Pets card
 * present, the widget grew past the bottom of the screen. And a move to
 * another monitor, which the main process has to announce.
 *
 * That last one used to be folded into the first, on the reasoning that a
 * move between screens looks like a resize from in here. It does not, unless
 * the two screens differ in scale factor: carry the widget from a 2560x1440
 * monitor to a 1600x900 one at the same scale factor and its CSS size is
 * unchanged, no resize fires, and the ceiling stays the one the bigger screen
 * justified -- so the widget can still be dragged to a size the screen it is
 * now on cannot hold.
 *
 * The measurement is deferred by a frame so it reads the layout the mode
 * switch actually produced rather than the one it is leaving.
 */
function useScaleLimit(shell, scale, mode) {
  const [max, setMax] = useState(SCALE_MAX_UNFITTED)
  const scaleRef = useRef(scale)
  scaleRef.current = scale

  useEffect(() => {
    if (mode === null) return undefined
    let raf = 0
    const measure = () => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => setMax(fitScale(shell.current, scaleRef.current)))
    }
    measure()
    window.addEventListener('resize', measure)
    const offDisplay = window.nw?.onDisplay?.((info) => {
      if (info?.width > 0 && info?.height > 0) {
        screenArea = { width: info.width, height: info.height }
      }
      measure()
    })
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', measure)
      offDisplay?.()
    }
  }, [shell, mode])

  return max
}

function resizeTo(node, tab, delta = 0) {
  const r = node?.getBoundingClientRect()
  if (!r) return
  window.nw?.resize({
    width: Math.ceil(r.width),
    height: Math.ceil(r.height) + delta,
    tab,
  })
}

/**
 * True only while the widget is first appearing.
 *
 * The entrance animation must not replay on a mode switch. Every card is a
 * fresh mount when the mode changes, so an unconditional `nw-rise` starts them
 * all at opacity 0 and the widget spends most of a second showing nothing but
 * the frost behind where its text should be. A mode switch is a deliberate act
 * with an obvious result; it does not need announcing, and it cannot be
 * animated honestly anyway -- the frost is a separate OS window and cannot
 * follow a tween.
 */
function useIntro() {
  const [on, setOn] = useState(() => Date.now() - BOOT < INTRO_MS)
  useEffect(() => {
    if (!on) return undefined
    const left = INTRO_MS - (Date.now() - BOOT)
    if (left <= 0) { setOn(false); return undefined }
    const t = setTimeout(() => setOn(false), left)
    return () => clearTimeout(t)
  }, [on])
  return on
}

/**
 * Entrance props for a card: the animation during the intro, nothing after.
 * Takes the card's own classes so the spread merges with them instead of
 * replacing them -- a spread `className` wins over an earlier prop.
 */
function useRise() {
  const intro = useIntro()
  return (delay, cls = '') => (intro
    ? { className: `${cls} nw-rise`, style: { animationDelay: delay } }
    : { className: cls })
}

/** True while the shell is moving the window and the frost is hidden. */
function useMoving() {
  const [moving, setMoving] = useState(false)
  useEffect(() => window.nw?.onMoving(setMoving), [])
  return moving
}

/**
 * How long a changed address stays flagged.
 *
 * Long enough to still be there when you next glance over, short enough that
 * the widget is not permanently red after a routine reconnect. A change is the
 * thing this widget exists to notice -- an exit that moved is either a tunnel
 * that dropped or one that reconnected somewhere else -- so it is worth a
 * minute of shouting, and not worth more.
 */
const ALERT_MS = 60000

/** '…' before the first lookup, '?' or 'Error' when one fails. */
const NOT_AN_ADDRESS = new Set(['', '?', 'Error', '…'])

/**
 * `{ on, from }` -- whether an address has just changed, and what it was.
 *
 * Two kinds of change are deliberately not changes. The first value is not:
 * the widget has only now learned what the address always was, and flagging
 * that would paint the panel red on every launch. Nor is anything involving a
 * placeholder, because a failed lookup is not a move -- treating one as a move
 * would cry wolf every time the network hiccups, which is exactly when a real
 * warning most needs to be believed.
 */
function useChanged(value) {
  const [alert, setAlert] = useState(null)
  const prev = useRef(undefined)

  useEffect(() => {
    if (typeof value !== 'string' || NOT_AN_ADDRESS.has(value)) return
    const before = prev.current
    prev.current = value
    if (before !== undefined && before !== value) {
      setAlert({ from: before, until: Date.now() + ALERT_MS })
    }
  }, [value])

  // The timer lives on its own so the flag clears itself on time even when no
  // further readings arrive -- which is precisely the case where the address
  // stopped changing, and the one where a stuck red border would be worst.
  useEffect(() => {
    if (!alert) return undefined
    const t = setTimeout(() => setAlert(null), Math.max(0, alert.until - Date.now()))
    return () => clearTimeout(t)
  }, [alert])

  return { on: Boolean(alert), from: alert?.from }
}

/** Props that paint a card as just-changed, and say what it changed from. */
function alertProps({ on, from }, what) {
  return on ? { 'data-alert': 'true', title: `${what} changed — was ${from}` } : {}
}

/** The wall clock, ticking locally rather than waiting on the network cycle. */
function useClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(id)
  }, [])
  return now
}

function Widget() {
  const s = useSidecar()
  const [compact, setCompact] = useState(false)
  // The tab is a mode of its own rather than a smaller compact: it is docked to
  // the screen edge instead of placed, and it is the only view with nothing to
  // read but numbers. It also has to be right on the first render -- see
  // `startTab` in preload.js.
  const [mini, setMini] = useState(() => Boolean(window.nw?.startTab))
  // Which screen edge the tab belongs to. The main process owns the dock and
  // tells the page only when the *edge* changes, because that is the only part
  // of it the page draws differently -- a row of dials on the top or the
  // bottom, a column on a side. Where it sits along that edge never reaches
  // here. Any of the four edges can arrive, including from `--nw-edge=` on the
  // first frame; an unknown value falls back to the top rather than rendering
  // an undefined padding class.
  const [edge, setEdge] = useState(() => (
    ['top', 'bottom', 'left', 'right'].includes(window.nw?.startEdge)
      ? window.nw.startEdge
      : 'top'))
  useEffect(() => window.nw?.onDock?.(setEdge), [])
  const [locked, setLocked] = useState(false)
  const [spinning, setSpinning] = useState(false)
  // How big the whole widget is drawn, as CSS `zoom` on the shell. Zoom rather
  // than a transform because it is a *layout* scale: every measured box grows
  // with it, so the window still follows the content and the frost panes still
  // match their cards, without either one being told the factor.
  const [scale, setScale] = useState(() => clampScale(window.nw?.startScale))
  const [resizing, setResizing] = useState(false)
  // Free-resize mode: the corner grip is always there, but a status readout is
  // not a window anyone expects to have a resize border, so the affordance is
  // easy to miss. This turns it into something you cannot miss -- a full-width
  // handle across the footer -- without adding a second way to do the sum.
  const [resizeMode, setResizeMode] = useState(false)
  const shell = useRef(null)

  const [animating, setAnimating] = useState(false)
  const { suspend, resume } = usePaneHold()
  const moving = useMoving()
  usePaneSync(animating)
  usePaneSync(scale)

  // How large this widget may be drawn on this screen, in this view. `null`
  // during the mode animation, because a box that is mid-tween is not a height
  // worth deriving a limit from.
  const fitMax = useScaleLimit(shell, scale, animating ? null : `${mini}:${compact}`)

  // What the resize controls are actually allowed to enforce and to report. The
  // measurement above is the honest fit, which in the full view on a high-DPI
  // screen lands under the floor; scaleCeiling turns that into a ceiling the
  // control can still be dragged within instead of a range one value wide.
  const maxScale = scaleCeiling(fitMax)

  // Nothing may sit above the fitted ceiling, whoever put it there -- a
  // remembered scale from a larger monitor, or a view switch from compact into
  // the much taller full one at a size only the compact view could afford.
  useEffect(() => {
    setScale((v) => clampScale(v, fitMax))
  }, [fitMax])

  // Watched here rather than inside the views, and that is the whole of it
  // working. Compact, Full and the tab are mounted and destroyed as you switch
  // between them, so a detector living in one of them remembers nothing from
  // before the switch: an address that moved while the mini bar was showing
  // looked like a first reading the moment the panel was opened, and a first
  // reading is deliberately never a change. Watching from here, which lasts as
  // long as the widget does, means the warning survives every switch -- and
  // means all three views agree about it.
  const ipChanged = useChanged(s.net?.ip)
  const rfChanged = useChanged(s.net?.ip2)

  // Publishes the pets that have been let loose, which is what opens and closes
  // the desktop overlay window. Watched from here for the same reason the
  // address alerts are: the roster outlives every view switch, and the overlay
  // must not blink out because the widget was collapsed to its tab.
  useOverlaySync()

  // Hide the frost before the new layout renders, not after, and keep it hidden
  // for the whole transition. A pane is an OS window: repositioning one lands
  // about a tenth of a second behind the page, so a frost that tried to follow
  // an animating layout would trail it the entire way. Hidden, the cards fall
  // back to a flat panel and the shape animates cleanly; the frost returns in
  // one step once the geometry has settled.
  const swapMode = useCallback(() => {
    suspend()
    setAnimating(true)
    setCompact((c) => !c)
  }, [suspend])

  const swapTab = useCallback(() => {
    suspend()
    setAnimating(true)
    setMini((m) => !m)
  }, [suspend])

  const onSettled = useCallback(() => {
    setAnimating(false)
    resume()
  }, [resume])

  // The tab sizes and docks itself, the way ModeBox does for the two expanded
  // layouts. Exactly one of the two is mounted at a time, so exactly one of
  // them settles the panes -- this effect does nothing on the path ModeBox owns.
  useLayoutEffect(() => {
    if (!mini) return
    resizeTo(shell.current, true)
    onSettled()
  }, [mini, edge, onSettled])

  // The window follows the content -- that is the whole reason this migration
  // was worth doing, since the Tk build had to measure every chip and re-fit
  // every card by hand. The exception is the mode transition: resizing a
  // transparent window once per frame is expensive and visibly steppy, so
  // ModeBox sizes the window itself, once at each end of the animation.
  const animatingRef = useRef(false)
  animatingRef.current = animating
  const miniRef = useRef(mini)
  miniRef.current = mini

  useEffect(() => {
    const node = shell.current
    if (!node) return undefined
    const ro = new ResizeObserver(() => {
      if (animatingRef.current) return
      resizeTo(node, miniRef.current)
    })
    ro.observe(node)
    return () => ro.disconnect()
  }, [])

  // A scale change resizes the window, and the observer above cannot be relied
  // on to notice: it reports the content box in layout units, which `zoom`
  // leaves untouched. So the new size is pushed out here, in the commit that
  // drew it -- the same reason ModeBox sizes the window itself. The panes
  // re-measure themselves, since every card's box has changed.
  useLayoutEffect(() => {
    resizeTo(shell.current, miniRef.current)
  }, [scale])

  const refresh = useCallback(() => {
    setSpinning(true)
    send({ cmd: 'refresh' })
    setTimeout(() => setSpinning(false), 900)
  }, [])

  const copy = (text) => () => {
    if (text && text !== '?' && text !== 'Error' && text !== '…') {
      send({ cmd: 'copy', text })
    }
  }

  // A narrow side gutter, not a symmetric one. The vertical padding is
  // breathing room, but every horizontal pixel of it is dead space beside the
  // cards: transparent, yet still inside the window, so it also swallows clicks
  // meant for whatever sits behind the widget.
  //
  // The tab has no gutter at all. Its card has to reach the window's own edge,
  // because the window deliberately overhangs the screen there so that the
  // frost's rounded corners are cut off -- any padding outside the card would
  // put those corners back on screen.
  return (
    <>
      <div
        ref={shell}
        className={mini ? 'w-fit' : 'w-[372px] px-1 py-2'}
        style={{ zoom: scale }}
        data-flat={moving || animating || resizing}
        onContextMenu={(e) => { e.preventDefault(); if (mini) swapTab(); else swapMode() }}
      >
        {mini ? (
          <MiniBar
            s={s}
            edge={edge}
            locked={locked}
            onExpand={swapTab}
            changed={ipChanged.on ? ipChanged : rfChanged}
            scale={scale}
            maxScale={maxScale}
            onScale={setScale}
            onScaleStart={() => { suspend(); setResizing(true) }}
            onScaleEnd={() => { setResizing(false); resume() }}
          />
        ) : (
          <div className="flex flex-col gap-2">
            <TitleCard s={s} locked={locked} />
            <ModeBox compact={compact} shell={shell} onSettled={onSettled}>
              {compact
                ? <Compact s={s} copy={copy} ipChanged={ipChanged} rfChanged={rfChanged} />
                : <Full s={s} copy={copy} ipChanged={ipChanged} rfChanged={rfChanged} />}
            </ModeBox>
            <FooterCard
              s={s}
              compact={compact}
              locked={locked}
              spinning={spinning}
              onToggleCompact={swapMode}
              onToggleMini={swapTab}
              onToggleLock={() => setLocked((l) => !l)}
              onRefresh={refresh}
              scale={scale}
              maxScale={maxScale}
              onScale={setScale}
              resizeMode={resizeMode}
              onToggleResize={() => setResizeMode((r) => !r)}
              onScaleStart={() => { suspend(); setResizing(true) }}
              onScaleEnd={() => { setResizing(false); resume() }}
            />
          </div>
        )}
      </div>

      {/* Outside the shell on purpose, and outside its `zoom` with it.

          The pets walk on the card rectangles the panes are measured from, and
          those are `getBoundingClientRect` values -- already scaled. A layer
          inside the shell would be laying out in unscaled units and the two
          would agree only at scale 1. Being a sibling also keeps it out of the
          ResizeObserver that sizes the window to its content, which a
          full-window layer would otherwise peg to the screen.

          Hidden while tabbed: a tab is one card, and with the topmost excluded
          as the pets' ceiling there is no ground left in it to stand on. */}
      <PetLayer enabled={!mini} scale={scale} />
    </>
  )
}

/**
 * Animates its own height when the mode changes.
 *
 * The content swaps instantly; what transitions is the box around it. That is
 * the only thing that *can* transition here -- the frost behind each card is a
 * separate OS window and cannot be tweened, so the cards are flat-panelled for
 * the duration and the glass is handed back in one step at the end.
 *
 * Growing resizes the window up front so the incoming content is never clipped
 * by a window that is still small; shrinking waits until the end so the last
 * frames are not cut off by a window that has already shrunk.
 */
const SWAP_MS = 260

function ModeBox({ compact, shell, onSettled, children }) {
  const box = useRef(null)
  const lastH = useRef(0)
  const settle = useRef(null)

  useLayoutEffect(() => {
    const el = box.current
    if (!el) return undefined
    const from = lastH.current
    const to = el.scrollHeight
    lastH.current = to

    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    // `delta` sizes the window for a height the page has not reached yet.
    // Measuring the shell mid-transition reports the height it is animating
    // *from*, so growing by measurement leaves the window small for the whole
    // animation and clips the content that is expanding into it.
    //
    // Always `tab: false`: this box is only mounted in the expanded modes, and
    // the first call after coming back from the tab is what returns the window
    // to the position it was dragged to.
    const sizeWindow = (delta = 0) => resizeTo(shell.current, false, delta)

    // First render, an unchanged height, or reduced motion: no transition.
    if (!from || from === to || reduce) {
      el.style.height = ''
      el.style.overflow = ''
      sizeWindow()
      onSettled()
      return undefined
    }

    el.style.overflow = 'hidden'
    el.style.height = `${from}px`
    el.getBoundingClientRect()          // flush, so the next line is a change
    el.style.transition = `height ${SWAP_MS}ms cubic-bezier(0.22, 1, 0.36, 1)`
    el.style.height = `${to}px`
    if (to > from) sizeWindow(to - from)   // grow the window before the content

    clearTimeout(settle.current)
    settle.current = setTimeout(() => {
      el.style.transition = ''
      el.style.height = ''
      el.style.overflow = ''
      sizeWindow()                      // and shrink it only now
      onSettled()
    }, SWAP_MS + 20)

    return () => clearTimeout(settle.current)
  }, [compact, shell, onSettled])

  return <div ref={box} className="flex flex-col gap-2">{children}</div>
}

/* ── title ─────────────────────────────────────────────────────────────────── */

function TitleCard({ s, locked }) {
  const rise = useRise()
  const now = useClock()
  const dragging = useRef(null)
  const net = s.net

  // Dragging is reported as a delta rather than an absolute position. Sending
  // the cursor position would snap the window's corner to the pointer on the
  // first move; the delta keeps the grab point under the finger.
  const onDown = (e) => {
    if (locked || e.button !== 0) return
    dragging.current = { x: e.screenX, y: e.screenY }
    e.currentTarget.setPointerCapture(e.pointerId)
  }
  const onMove = (e) => {
    const d = dragging.current
    if (!d) return
    window.nw?.drag({
      dx: e.screenX - d.x, dy: e.screenY - d.y, x: e.screenX, y: e.screenY,
    })
    dragging.current = { x: e.screenX, y: e.screenY }
  }
  const onUp = (e) => {
    if (!dragging.current) return
    dragging.current = null
    e.currentTarget.releasePointerCapture?.(e.pointerId)
    window.nw?.dragEnd()
  }

  const live = net?.ip && net.ip !== '?' && net.ip !== 'Error'
  return (
    <Card id="title" {...rise()}>
      <div
        className={'flex items-center gap-2.5 px-3 py-2.5 '
          + (locked ? '' : 'cursor-grab active:cursor-grabbing')}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        title={locked ? 'Position locked' : 'Drag to move'}
      >
        <SectionIcon name="spark" />
        <span className="glass-text text-[14px] font-semibold tracking-tight">
          Net Watch
        </span>
        <span
          className="h-[5px] w-[5px] shrink-0 rounded-full"
          style={{
            background: live ? 'var(--color-good)' : 'var(--color-faint)',
            boxShadow: live ? '0 0 6px var(--color-good)' : 'none',
          }}
          title={live ? 'Receiving data' : 'Waiting for data'}
        />

        <span className="flex-1" />

        <Clocks now={now} tehran={s.tehran} />
        <VpnChip vpn={net?.vpn} />
      </div>
    </Card>
  )
}

/**
 * A 12-hour wall clock, zero-padded: '01:30 pm'.
 *
 * Built by hand rather than left to `toLocaleTimeString`, because the two
 * things that matter here are exactly the two a locale is free to change its
 * mind about: whether the hour is padded, and whether the suffix is there at
 * all. Seconds are dropped -- the row they sit in is tight, and a clock that is
 * read at a glance is not read to the second.
 */
function clock12(d) {
  const h = d.getHours()
  const h12 = h % 12 === 0 ? 12 : h % 12
  return `${String(h12).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')} `
    + (h < 12 ? 'am' : 'pm')
}

/** Local wall clock, and Tehran alongside it when the sidecar has reported it. */
function Clocks({ now, tehran }) {
  return (
    <span className="flex shrink-0 items-baseline gap-1.5">
      {/* A fixed slot, not a fitted one. The title card's width is measured and
          pushed to the main process, so anything that changes it moves the
          window -- and a 12-hour clock changes width twice a day, at 9 to 10
          and again at am to pm. The minimum is comfortably wider than the
          longest reading, so in practice the box is simply always this wide. */}
      <span
        className="glass-text min-w-[62px] shrink-0 whitespace-nowrap text-right text-[11px]
                   tabular-nums text-ink-2"
      >
        {clock12(now)}
      </span>
      {tehran && (
        <>
          <span className="glass-text text-[10px] text-faint">·</span>
          <span className="glass-text text-[10px] tabular-nums text-muted" title="Tehran time">
            THR {tehran}
          </span>
        </>
      )}
    </span>
  )
}

/**
 * The filled blue chip from the design -- not a coloured word.
 *
 * `tab` is the docked variant, and the three things it changes all exist for
 * the same reason. It is larger, because a 9px chip beside 11px dial figures
 * reads as a mistake rather than as a smaller thing. It is a fixed width, and
 * it holds its place with a dash before the first lookup answers rather than
 * not being there -- because the tab is positioned from its own size, so a chip
 * that appeared, or that grew from VPN to DIRECT, would shove the whole window
 * sideways.
 */
function VpnChip({ vpn, tab }) {
  const unknown = vpn == null
  if (unknown && !tab) return null
  // Every state carries a border, transparent where the design has none. Only
  // the VPN state is drawn with a visible one, and a border that comes and goes
  // changes the chip's height by two pixels -- which in the tab's column layout
  // is two pixels of window, and so a move, and so a blink.
  return (
    <span
      className={'glass-text shrink-0 rounded-[5px] border text-center font-semibold '
        + 'uppercase tracking-wide '
        + (tab ? 'w-[50px] py-[3px] text-[10.5px] ' : 'ml-1 px-1.5 py-[2px] text-[9px] ')
        + (unknown
          ? 'border-transparent bg-white/[0.08] text-faint'
          : vpn
            ? 'border-white/30 bg-gradient-to-b from-[#3d95ff] to-[#1f77f0] text-white'
            : 'border-transparent bg-white/12 text-muted')}
      title={unknown ? 'Waiting for the first pair of IP lookups'
        : vpn ? 'The two IP lookups disagree: a tunnel is rewriting one of them'
          : 'Both IP lookups agree'}
    >
      {unknown ? '—' : vpn ? 'VPN' : 'Direct'}
    </span>
  )
}

/* ── full ──────────────────────────────────────────────────────────────────── */

function Full({ s, copy, ipChanged, rfChanged }) {
  const net = s.net
  const hw = s.hw
  const tz = s.tz
  const rise = useRise()
  return (
    <>
      <Pair>
        <Card id="ip" {...rise('40ms', 'pb-2')} {...alertProps(ipChanged, 'IP address')}>
          <CardHead icon="globe">IP address</CardHead>
          <Figure value={net?.ip ?? '…'} onCopy={copy(net?.ip)} />
          <div className="mt-1 flex items-center gap-1.5 px-3">
            <CountryChip code={net?.code} small />
            <span className="glass-text truncate text-[10.5px] text-ink-2">
              {net?.country ?? '…'}
            </span>
          </div>
        </Card>

        <Card id="rf" {...rise('55ms', 'pb-2')} {...alertProps(rfChanged, 'Runflare exit')}>
          <CardHead icon="signal">Runflare</CardHead>
          <Figure value={net?.ip2 ?? '…'} onCopy={copy(net?.ip2)} />
          <div className="mt-1 flex items-center gap-1.5 px-3">
            <CountryChip code={net?.code2} small />
            <span className="glass-text truncate text-[10.5px] text-ink-2">
              {net?.country2 ?? '…'}
            </span>
          </div>
        </Card>
      </Pair>

      <Pair>
        <Card id="lan" {...rise('70ms', 'pb-2')}>
          <CardHead icon="route">Local / gateway</CardHead>
          <Figure value={net?.local ?? '…'} onCopy={copy(net?.local)} />
          <div className="mt-1.5 flex items-baseline gap-2 px-3">
            <span className="glass-text text-[9.5px] uppercase tracking-wide text-faint">GW</span>
            <span className="glass-text truncate text-[10.5px] tabular-nums text-muted">
              {net?.gw ?? '…'}
            </span>
          </div>
        </Card>

        <Card id="ping" {...rise('85ms', 'py-2.5')}>
          <PingTarget host="1.1.1.1" ms={net?.ping1} loss={net?.loss1} onCopy={copy(net?.ping1)} />
          <div className="mt-1.5" />
          <PingTarget host="8.8.8.8" ms={net?.ping2} loss={net?.loss2} onCopy={copy(net?.ping2)} />
        </Card>
      </Pair>

      <Pair>
        <Card id="hw" {...rise('100ms', 'pb-2')}>
          <CardHead icon="chip">Hardware</CardHead>
          <HwRow label="CPU" value={hw?.cpu} />
          <HwRow label="RAM" value={hw?.ram} />
          <HwRow label="GPU" value={hw?.gpu} fallback={hw?.gpu_raw} />
        </Card>

        <Card id="tz" {...rise('115ms', 'pb-2')}>
          <CardHead icon="clock">Timezone</CardHead>
          <div className="glass-text truncate px-3 text-[11px] text-ink-2">
            {tz?.tz ? `${tz.suspect ? '⚠ ' : ''}${tz.tz}` : 'checking…'}
          </div>
          <div className={'glass-text mt-1 truncate px-3 text-[10px] ' + tzMatch(tz, net).tone}>
            {tzMatch(tz, net).text}
          </div>
        </Card>
      </Pair>

      <AiCard ai={s.ai} rise={rise} />
      <ChecksCard checks={s.checks} ip={s.net?.ip} rise={rise} />
      {/* Last of the cards, just above the footer: it is the one row here that
          is a control rather than a readout, so it sits with the controls
          rather than interrupting the run of numbers. */}
      <PetsCard rise={rise} />
    </>
  )
}

function PingTarget({ host, ms, loss, onCopy }) {
  return (
    <div className="px-3">
      <div className="glass-text text-[12.5px] font-semibold tabular-nums">{host}</div>
      <div className="mt-0.5 flex items-center gap-2">
        <span
          className={'glass-text text-[10.5px] tabular-nums ' + pingTone(ms)
            + (onCopy ? ' cursor-pointer hover:text-accent-2' : '')}
          onClick={onCopy}
          title={onCopy ? 'Click to copy' : undefined}
        >
          {ms ?? '—'}
        </span>
        <span className="h-[9px] w-px bg-white/20" />
        <span className={'glass-text text-[10.5px] tabular-nums '
          + (loss > 0 ? 'text-bad' : 'text-muted')}>
          {loss == null ? '—' : `${loss}% loss`}
        </span>
      </div>
    </div>
  )
}

function HwRow({ label, value, fallback }) {
  const has = value != null
  return (
    <div className="flex items-baseline gap-2 px-3 py-[1.5px]">
      <span className="glass-text w-[30px] shrink-0 text-[10.5px] text-muted">{label}</span>
      <span className={`glass-text text-[11px] font-medium tabular-nums ${pctTone(value)}`}>
        {has ? `${Math.round(value)}%` : (fallback ?? '—')}
      </span>
    </div>
  )
}

/* ── AI usage ──────────────────────────────────────────────────────────────── */

const AI_ROWS = [
  ['Claude · 5h', 'claude', 'session_pct', 'session_reset'],
  ['Claude · week', 'claude', 'week_pct', 'week_reset'],
  ['Claude · model', 'claude', 'model_pct', 'model_reset'],
  ['GPT · 5h', 'codex', 'sess_pct', 'sess_reset_ts'],
  ['GPT · week', 'codex', 'week_pct', 'week_reset_ts'],
]

function AiCard({ ai, rise }) {
  const cl = ai?.claude
  const gt = ai?.codex
  const spinning = ai?.status === 'polling'

  return (
    <Card id="ai" {...rise('130ms', 'pb-2')}>
      <CardHead
        icon="spark"
        right={
          <span className="flex items-center gap-2">
            <button
              type="button"
              title="Poll AI usage now"
              onClick={() => send({ cmd: 'ai_refresh' })}
              className={'text-[11px] leading-none text-muted transition hover:text-accent '
                + (spinning ? 'animate-[nw-spin_900ms_linear_infinite]' : '')}
            >
              &#8635;
            </button>
            <span className="glass-text text-[9.5px] tabular-nums text-faint">
              {ai?.polled_at ? `◷ ${ai.polled_at}` : '…'}
            </span>
          </span>
        }
      >
        AI usage
      </CardHead>

      {ai?.off ? (
        <Row label="Status" value={ai.reason} tone="text-warn" />
      ) : (
        <>
          {AI_ROWS.map(([name, who, pctKey, resetKey]) => {
            const d = who === 'claude' ? cl : gt
            const label = pctKey === 'model_pct' && cl?.model_name
              ? `Claude · ${cl.model_name}`
              : name
            // A held value is the last real reading, kept through a failed
            // poll. It is shown dimmed rather than replaced by a dash: the
            // number is still true, it is just no longer fresh.
            const held = Boolean(d?.err && d?.held)
            return (
              <UsageRow
                key={name}
                label={label}
                pct={d && (!d.err || held) ? d[pctKey] : null}
                reset={d && !d.err ? d[resetKey] : ''}
                held={held}
              />
            )
          })}
          <AiNote
            cl={cl}
            gt={gt}
            retry={ai?.retry}
            age={ai?.age}
            wait={ai?.status === 'wait' ? ai : null}
          />
        </>
      )}
    </Card>
  )
}

/** Name, percent, bar and countdown, as one line. */
function UsageRow({ label, pct, reset, held }) {
  return (
    <div className={'flex items-center gap-2 px-3 py-[1.5px] ' + (held ? 'opacity-55' : '')}>
      <span className="glass-text w-[84px] shrink-0 truncate text-[10px] text-ink-2">
        {label}
      </span>
      <span
        className={'glass-text w-[28px] shrink-0 text-right text-[10.5px] font-medium '
          + 'tabular-nums ' + (held ? 'text-muted' : pctTone(pct))}
      >
        {pct == null ? '—' : `${Math.round(pct)}%`}
      </span>
      <span className="w-[76px] shrink-0"><Bar value={pct} /></span>
      <span className="glass-text min-w-0 flex-1 truncate text-[9.5px] text-faint">
        {reset ? `resets ${reset}` : ''}
      </span>
    </div>
  )
}

/** "rate limited · retry in 57m" -- why it is stale, and when it will not be. */
function untilRetry(seconds) {
  if (!seconds) return ''
  const m = Math.round(seconds / 60)
  return m >= 60 ? ` · retry in ${Math.floor(m / 60)}h${m % 60 ? ` ${m % 60}m` : ''}`
    : ` · retry in ${m}m`
}

/**
 * The line under the bars: plan names when all is well, failures when not.
 *
 * A CLI that was never logged in is a setup hint in muted text, not a red
 * failure -- that distinction is the whole reason the sidecar forwards `setup`
 * separately from `err`. A dead login is the one failure that can be acted on
 * from here, so it becomes a clickable prompt.
 */
/** "N min" from a number of seconds, for a countdown that is never precise. */
function waitLabel(seconds) {
  const m = Math.round((Number(seconds) || 0) / 60)
  return m >= 1 ? `${m} min` : 'under a minute'
}

function AiNote({ cl, gt, retry, age, wait }) {
  const notes = []
  // Why pressing refresh did nothing. Without this the button is simply inert
  // during a backoff, which reads as broken rather than as deliberate.
  if (wait) {
    notes.push(wait.reason
      ? `${wait.reason} — ${waitLabel(wait.seconds)} left`
      : `next poll in ${waitLabel(wait.seconds)}`)
  }
  let expired = null
  for (const [who, d] of [['Claude', cl], ['GPT', gt]]) {
    if (!d?.err) continue
    const m = /LOGIN_EXPIRED:(\w+)/.exec(d.err)
    if (m) {
      expired = expired ?? m[1]
      notes.push(`${who}: login expired — click to sign in`)
    } else {
      notes.push(`${who}: ${d.err}${untilRetry(retry)}`)
    }
  }
  if (gt?.limit_reached) notes.push('GPT limit reached!')

  if (notes.length) {
    const onlySetup = (cl?.setup || !cl?.err) && (gt?.setup || !gt?.err)
    return (
      <div
        className={'glass-text mt-1 px-3 text-[10px] '
          + (onlySetup ? 'text-faint' : 'text-warn')
          + (expired ? ' cursor-pointer underline decoration-dotted' : '')}
        onClick={expired ? () => send({ cmd: 'ai_login', which: expired }) : undefined}
      >
        {notes.join('  |  ')}
      </div>
    )
  }

  const plans = []
  if (age) plans.push(`as of ${agoLabel(age)} ago`)
  if (cl && !cl.err) plans.push(`Claude ${cl.plan || 'MAX'}`)
  if (gt && !gt.err) plans.push(`GPT ${gt.plan || 'PLUS'}`)
  if (!plans.length) return null
  return (
    <div className="glass-text mt-1 px-3 text-[10px] text-muted">
      {plans.join(' · ')}
    </div>
  )
}

/* ── quick checks ──────────────────────────────────────────────────────────── */

const LOOKUPS = [
  ['BGP', (ip) => `https://bgpview.io/ip/${ip}`],
  ['IQPS', (ip) => `https://ipqualityscore.com/free-ip-lookup-proxy-vpn-test/lookup/${ip}`],
  ['Scam', (ip) => `https://scamalytics.com/ip/${ip}`],
  ['bgp.he', (ip) => `https://bgp.he.net/ip/${ip}#_bgpmap`],
  ['Ping', (ip) => `https://ping.pe/${ip}`],
]

function ChecksCard({ checks, ip, rise }) {
  // Cleared locally rather than through the sidecar: "forget what you showed
  // me" is view state, and a round trip to Python to blank seven labels would
  // be a slower way to get the same pixels.
  const [cleared, setCleared] = useState(false)
  const c = cleared ? null : checks
  const live = ip && ip !== '?' && ip !== 'Error'

  const run = () => { setCleared(false); send({ cmd: 'checks' }) }
  const has = c && !c.failed && !c.loading

  return (
    <Card id="chk" {...rise('160ms', 'pb-2.5')}>
      <CardHead
        icon="shield"
        right={
          <span className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setCleared(true)}
              className="text-[9.5px] text-faint transition hover:text-accent"
            >
              Clear
            </button>
            <PrimaryButton onClick={run} title="Run the quick checks now">Run</PrimaryButton>
          </span>
        }
      >
        Quick checks
      </CardHead>

      <div className="grid grid-cols-2 gap-x-1">
        <div>
          <Cell label="ASN" value={has ? (c.asn || '—') : '—'} />
          <Cell label="ISP" value={has ? (c.isp || '—') : '—'} />
          <Cell
            label="Proxy / VPN"
            value={has ? (c.proxy ? 'Proxy' : 'Clean') : '—'}
            tone={has ? (c.proxy ? 'text-bad' : 'text-good') : 'text-faint'}
          />
          <Cell
            label="Datacenter"
            value={has ? (c.hosting ? 'Datacenter' : 'Residential') : '—'}
            tone={has ? (c.hosting ? 'text-warn' : 'text-good') : 'text-faint'}
          />
        </div>
        <div>
          <Cell label="Hostname" value={has ? (c.host || '—') : '—'} />
          <Cell
            label="DNS"
            value={has
              ? (c.dns_leak
                ? `LEAK: ${(c.dns ?? []).join(' | ')}`
                : ((c.dns ?? []).join(' | ') || '—'))
              : '—'}
            tone={has ? (c.dns_leak ? 'text-bad' : 'text-good') : 'text-faint'}
          />
          <Cell
            label="Score"
            value={has && c.score != null ? `${c.score}/100` : '—'}
            tone={has ? scoreTone(c.score) : 'text-faint'}
          />
        </div>
      </div>

      {c?.loading && !has && (
        <div className="glass-text mt-1 px-3 text-[10px] text-faint">checking…</div>
      )}
      {c?.failed && (
        <div className="glass-text mt-1 px-3 text-[10px] text-bad">lookup failed</div>
      )}

      <div className="mt-2 flex flex-wrap gap-1.5 px-3">
        {LOOKUPS.map(([name, url]) => (
          <Pill
            key={name}
            title={live ? `Open ${name} for ${ip}` : 'Waiting for a public IP'}
            onClick={() => live && send({ cmd: 'open', url: url(ip) })}
          >
            {name}
          </Pill>
        ))}
      </div>
    </Card>
  )
}

/* ── the tab ───────────────────────────────────────────────────────────────── */

/**
 * What an absent AI reading means, for the dial's tooltip.
 *
 * The sidecar's own words, not a euphemism for them. "rate limited" is the
 * whole answer to why a dial is empty -- the usage endpoints lock out for an
 * hour at a time -- and collapsing it into "lookup failed" throws away the one
 * piece of information that would stop someone going to check their login.
 */
function aiNote(a) {
  if (!a) return 'no reading yet'
  if (a.setup) return 'not set up'
  if (!a.err) return 'no reading yet'
  return String(a.err).startsWith('LOGIN_EXPIRED') ? 'login expired' : a.err
}

/**
 * The best reading a provider has, and which window it is of.
 *
 * Two things here are not a property lookup. A free ChatGPT plan has no
 * five-hour window at all -- the endpoint returns only a 30-day one -- so a dial
 * wired to the session is permanently empty on those accounts however healthy
 * they are. The first window with a number in it wins, and the label says which
 * one it was.
 *
 * And a failed poll keeps the last real number rather than blanking it, the way
 * the full view's rows do. A dash would claim no reading exists when the truth
 * is that it could not be asked for again yet.
 */
function aiDial(a, windows) {
  const held = Boolean(a?.err && a.held)
  if (a && (!a.err || held)) {
    for (const [key, label] of windows) {
      if (a[key] != null) return { value: a[key], label, held }
    }
  }
  return { value: null, label: windows[0][1], held: false }
}

/** How far a press may wander before it counts as a drag rather than a click. */
const DRAG_SLOP = 4

/**
 * The widget collapsed to a tab on an edge of the screen.
 *
 * It answers one question -- is anything running hot -- and nothing else. A dial
 * each for CPU, RAM, GPU and the two AI allowances, then the ping and the
 * adapter dot. Every name, address and number worth copying lives in the
 * expanded view, which is one click away, because the whole tab is the button.
 *
 * Four things about it are load-bearing rather than stylistic:
 *
 * Its size does not depend on its data. Every dial is always drawn, showing a
 * dash until it has a reading, and the ping and the VPN chip sit in slots of a
 * fixed width. This is not tidiness: a docked tab is *positioned* from its own
 * size, so anything that changes that size moves the whole window. Drawing only
 * the dials that had data meant the tab came up 166px wide, then jumped 153px
 * sideways a second later when the AI readings landed -- and took a frost blink
 * with it, because a moving window hides its panes. A dash in a dial is also
 * the more honest picture: the reading is missing, not the machine.
 *
 * The edge decides the shape. On the top or the bottom it is a row; on the left
 * or the right it is a column, because a 330px-wide bar down the side of a
 * screen is not a tab, it is a wall. Nothing else changes -- same dials, same
 * order.
 *
 * Corner rounding needs no per-edge case. The card is rounded on all four
 * corners and the window overhangs its screen edge by `TAB_BLEED`, so the two
 * corners that would give the dock away are cut off by the screen itself --
 * whichever edge that happens to be.
 *
 * The lopsided padding. The window sits `TAB_BLEED` px *past* its edge so that
 * DWM's rounded corners on the frost behind this card are cut off by the screen
 * (the reasoning is in electron/main.js). The same ten pixels are padded back
 * on, on whichever side the bleed is, so the dials stay centred in the part that
 * can be seen -- the two numbers have to agree, and they are written down in
 * both places.
 *
 * Drag versus click. Both are the same gesture until the pointer moves, so a
 * press only becomes a drag after a few pixels, and a press that did move
 * swallows the click that follows it. Without the threshold the tab expands
 * under a hand that was trying to slide it along the edge.
 */

/* Padding, per edge: the bleed plus the ordinary inset on the side that runs
   off the screen, the ordinary inset everywhere else. */
const TAB_PAD = {
  top: 'px-2.5 pt-[20px] pb-[10px]',
  bottom: 'px-2.5 pb-[20px] pt-[10px]',
  left: 'py-2.5 pl-[20px] pr-[10px]',
  right: 'py-2.5 pr-[20px] pl-[10px]',
}

/* Which edges lay the dials out as a row.

   A membership test rather than a comparison against 'top', because the bottom
   edge is the top edge's mirror and wants the same row. The obvious
   `edge !== 'top'` would have drawn the bottom tab as a column -- a 330px-wide
   wall standing on the taskbar -- and it is the kind of mistake that only shows
   up after the dock itself already works. */
const HORIZONTAL_EDGES = ['top', 'bottom']

function MiniBar({
  s, edge, locked, onExpand, changed,
  scale, maxScale, onScale, onScaleStart, onScaleEnd,
}) {
  const net = s.net
  const hw = s.hw
  const cl = s.ai?.claude
  const gt = s.ai?.codex
  const { up, busy } = s.netstate ?? { up: true, busy: false }
  const ping = pickPing(net)
  const claude = aiDial(cl, [['session_pct', '5h'], ['week_pct', 'week']])
  const codex = aiDial(gt, [['sess_pct', '5h'], ['week_pct', 'week']])
  const rise = useRise()
  const drag = useRef(null)
  const column = !HORIZONTAL_EDGES.includes(edge)

  const onDown = (e) => {
    if (e.button !== 0) return
    drag.current = { x: e.screenX, y: e.screenY, moved: false, held: true }
    e.currentTarget.setPointerCapture(e.pointerId)
  }
  const onMove = (e) => {
    const d = drag.current
    // `held`, not merely `drag.current`: the gesture has to outlive the button
    // for the click below to know it was a drag, and a pointermove with nothing
    // pressed is an ordinary hover. Without the flag, every hover over the tab
    // after a drag was reported as more of that drag -- the tab crept out from
    // under its edge whenever the mouse crossed it, and stayed there, because no
    // button ever came up to send the `dragEnd` that docks it.
    if (!d?.held || locked) return
    const dx = e.screenX - d.x
    const dy = e.screenY - d.y
    if (!d.moved && Math.abs(dx) + Math.abs(dy) < DRAG_SLOP) return
    d.moved = true
    // The pointer's own position travels with the delta. The delta moves the
    // window; the position is what decides which edge the tab is heading for,
    // and it has to be the hand rather than the window, because the window
    // changes shape on the way and its centre moves with it.
    window.nw?.drag({ dx, dy, x: e.screenX, y: e.screenY })
    d.x = e.screenX
    d.y = e.screenY
  }
  const onUp = (e) => {
    const d = drag.current
    if (!d?.held) return
    d.held = false
    e.currentTarget.releasePointerCapture?.(e.pointerId)
    if (d.moved) window.nw?.dragEnd()
  }
  // Expanding hangs off `click` rather than off the pointer-up above, so the
  // tab is a real clickable: it answers a keyboard or a synthesised click the
  // same way it answers the mouse. `moved` is what a drag leaves behind, and it
  // is cleared on the next press rather than here, because a drag released off
  // the element never produces a click to clear it. That outliving is why the
  // press is tracked by its own `held` flag rather than by the record existing.
  const onClick = () => {
    if (!drag.current?.moved) onExpand()
  }

  return (
    <Card
      id="tab"
      {...rise('40ms', 'cursor-pointer transition-colors duration-200 '
        + 'hover:border-white/45 ' + TAB_PAD[edge])}
      {...alertProps(changed, 'Exit address')}
      onPointerDown={onDown}
      onPointerMove={onMove}
      onPointerUp={onUp}
      onPointerCancel={onUp}
      onClick={onClick}
      aria-label="Open the full widget"
      title={locked
        ? 'Click to open the full widget'
        : `Click to open the full widget · drag to move it, or to another edge`}
    >
      <div className={'flex items-center gap-1.5 ' + (column ? 'flex-col' : '')}>
        <SectionIcon name="spark" size={24} />
        <Ring label="CPU" value={hw?.cpu} />
        <Ring label="RAM" value={hw?.ram} />
        <Ring label="GPU" value={hw?.gpu} note={hw?.gpu_raw} />
        <Ring
          label={`Claude · ${claude.label}`}
          hue="ai"
          value={claude.value}
          stale={claude.held}
          note={aiNote(cl)}
        />
        <Ring
          label={`Codex · ${codex.label}`}
          hue="ai"
          value={codex.value}
          stale={codex.held}
          note={aiNote(gt)}
        />

        <span className={'shrink-0 bg-white/15 ' + (column ? 'h-px w-[20px]' : 'h-[20px] w-px')} />

        <span
          className={'glass-text w-[46px] shrink-0 truncate text-center text-[11.5px] '
            + 'tabular-nums ' + ping.tone}
        >
          {ping.text}
        </span>
        <TabAddress ip={net?.ip} column={column} />
        <NetDot up={up} busy={busy} />
        <VpnChip vpn={net?.vpn} tab />
        <TabResizeGrip
          column={column}
          scale={scale}
          onScale={onScale}
          max={maxScale}
          onStart={onScaleStart}
          onEnd={onScaleEnd}
        />
      </div>
    </Card>
  )
}

/**
 * The exit address, in the tab.
 *
 * It is the reading the widget exists for, so it belongs on the strip and not
 * only behind a click -- but the strip is two different shapes. On the top and
 * bottom edges there is width to spare and the address reads as one ordinary
 * line. On the left and right edges the tab is a narrow column, and a dotted
 * quad laid out sideways there would either be cut off or would widen the whole
 * window; so it is stacked instead, an octet per line with the joining dot on a
 * line of its own.
 *
 * Every line is centred rather than padded into place. A dot is one glyph wide
 * against three digits, and any alignment that is not real text centring leaves
 * it stuck against one side of the column instead of sitting between the two
 * numbers it joins.
 *
 * Nothing is ever truncated. The row is `whitespace-nowrap` and the tab sizes
 * itself to its content, so a longer address widens the strip rather than
 * disappearing off the end of it; the column form already wraps by
 * construction.
 */
function TabAddress({ ip, column }) {
  const live = ip && ip !== '?' && ip !== 'Error'
  const text = live ? ip : '…'

  if (!column) {
    return (
      <span
        className="glass-text shrink-0 whitespace-nowrap text-center text-[11.5px]
                   tabular-nums text-ink-2"
        title="Exit address"
      >
        {text}
      </span>
    )
  }

  // Split on the dots rather than rendering them with the octets, because the
  // separator is its own line here and a joined string cannot be centred a
  // piece at a time.
  const parts = live ? text.split('.') : [text]
  return (
    <span
      className="glass-text flex shrink-0 flex-col items-center text-center text-[11.5px]
                 leading-[1.05] tabular-nums text-ink-2"
      title="Exit address"
    >
      {parts.map((part, i) => (
        <span key={`${part}-${i}`} className="block w-full text-center">
          {i > 0 && <span className="block w-full text-center text-faint">.</span>}
          {part}
        </span>
      ))}
    </span>
  )
}

/**
 * The tab's resize handle.
 *
 * The same `useScaleDrag` the corner grip and the footer band use, so dragging,
 * double-click to reset and remembering the size on release all behave exactly
 * as they do in the expanded views -- there is one scale and one way of
 * changing it.
 *
 * What is different is that the tab's own card already owns the pointer: it
 * moves the window on a drag and expands the widget on a click. So every event
 * this handle takes is stopped here rather than allowed to bubble, otherwise a
 * resize would slide the tab along its edge and then open the panel on release.
 */
function TabResizeGrip({ column, scale, onScale, onStart, onEnd, max }) {
  const handlers = useScaleDrag({ scale, onScale, onStart, onEnd, max })
  const stop = (fn) => (e) => { e.stopPropagation(); fn(e) }

  return (
    <span
      className={'grid shrink-0 cursor-nwse-resize place-items-center rounded-[5px] '
        + 'text-faint transition hover:bg-white/15 hover:text-ink-2 '
        + (column ? 'h-[16px] w-[20px]' : 'h-[20px] w-[16px]')}
      title={`Drag to resize the widget · double-click to reset `
        + `(${Math.round(scale * 100)}% of ${Math.round(max * 100)}% max)`}
      onPointerDown={stop(handlers.onPointerDown)}
      onPointerMove={stop(handlers.onPointerMove)}
      onPointerUp={stop(handlers.onPointerUp)}
      onPointerCancel={stop(handlers.onPointerCancel)}
      onDoubleClick={stop(handlers.onDoubleClick)}
      onClick={(e) => e.stopPropagation()}
    >
      <svg viewBox="0 0 16 16" className="h-[11px] w-[11px]" stroke="currentColor" strokeWidth="1.4">
        <path d="M15 9 9 15M15 13l-2 2" strokeLinecap="round" />
      </svg>
    </span>
  )
}

/* ── compact ───────────────────────────────────────────────────────────────── */

/**
 * The summary view: the same readings as the full panel, one line each.
 *
 * Three cards where there was one. The exit address is the headline and keeps
 * the full width; Runflare and the LAN share the row below it, because they are
 * each two short values and a half-width card holds both comfortably. That
 * pairing is what keeps this view a summary -- stacked full-width they would
 * cost as much height as the full view they exist to be an alternative to.
 *
 * Every card carries its own `c-`-prefixed id. The ids are what the main
 * process places the frost panes from, so a repeat of a full-view id would put
 * two cards' glass in one place and leave a card without any.
 */
function Compact({ s, copy, ipChanged, rfChanged }) {
  const net = s.net
  const hw = s.hw
  const cl = s.ai?.claude
  const gt = s.ai?.codex
  const rise = useRise()
  const ping = pickPing(net)
  return (
    <>
      <Card id="c-ip" {...rise('40ms', 'pb-2')} {...alertProps(ipChanged, 'IP address')}>
        <CardHead icon="globe" tight right={<CountryChip code={net?.code} small />}>
          {net?.country ?? 'Net Watch'}
        </CardHead>
        <Figure value={net?.ip ?? '…'} onCopy={copy(net?.ip)} />
        <div className="mt-1 flex items-baseline gap-2 px-3">
          <span className={'glass-text text-[10.5px] tabular-nums ' + ping.tone}>
            {ping.text}
          </span>
          <span className="glass-text min-w-0 flex-1 truncate text-[10px] text-faint">
            {net?.isp || (s.ready ? 'looking up…' : 'starting…')}
          </span>
        </div>
      </Card>

      <Pair>
        <Card id="c-rf" {...rise('55ms', 'pb-2')} {...alertProps(rfChanged, 'Runflare exit')}>
          <CardHead icon="signal" tight right={<CountryChip code={net?.code2} small />}>
            Runflare
          </CardHead>
          <Figure value={net?.ip2 ?? '…'} onCopy={copy(net?.ip2)} />
          {/* Wrapped, never clipped: a country name that ran off the end of a
              half-width card would be indistinguishable from a different
              country with the same first few letters. */}
          <div className="glass-text mt-1 break-words px-3 text-[10px] leading-snug text-ink-2">
            {net?.country2 ?? '…'}
          </div>
        </Card>

        <Card id="c-lan" {...rise('55ms', 'pb-2')}>
          <CardHead icon="route" tight>Local / gateway</CardHead>
          <Figure value={net?.local ?? '…'} onCopy={copy(net?.local)} />
          <div className="mt-1 flex items-baseline gap-2 px-3">
            <span className="glass-text shrink-0 text-[9.5px] uppercase tracking-wide text-faint">
              GW
            </span>
            <span className="glass-text min-w-0 break-all text-[10px] tabular-nums text-muted">
              {net?.gw ?? '…'}
            </span>
          </div>
        </Card>
      </Pair>

      <Card id="c-hw" {...rise('70ms', 'py-2')}>
        <div className="flex gap-3 px-3">
          <Meter label="CPU" value={hw?.cpu} />
          <Meter label="RAM" value={hw?.ram} />
          <Meter label="GPU" value={hw?.gpu} fallback={hw?.gpu_raw} />
        </div>
      </Card>

      {s.ai && (
        <Card id="c-ai" {...rise('100ms', 'py-2')}>
          <div className="flex gap-3 px-3">
            <Meter
              label="Claude"
              value={cl && !cl.err ? cl.session_pct : null}
              fallback={cl?.setup ? 'setup' : cl?.err ? 'err' : '—'}
            />
            <Meter
              label="Codex"
              value={gt && !gt.err ? gt.sess_pct : null}
              fallback={gt?.setup ? 'setup' : gt?.err ? 'err' : '—'}
            />
          </div>
        </Card>
      )}
    </>
  )
}

function Meter({ label, value, fallback }) {
  const has = value != null
  return (
    <div className="min-w-0 flex-1">
      <div className="flex items-baseline justify-between">
        <span className="glass-text text-[9.5px] uppercase tracking-wide text-muted">{label}</span>
        <span className={`glass-text text-[10.5px] font-semibold tabular-nums ${pctTone(value)}`}>
          {has ? `${Math.round(value)}%` : (fallback ?? '—')}
        </span>
      </div>
      <div className="mt-1"><Bar value={value} /></div>
    </div>
  )
}

/* ── footer ────────────────────────────────────────────────────────────────── */

/**
 * The corner grip that sizes the whole widget.
 *
 * It scales rather than reflows, and that is the honest thing for this layout
 * to do: the cards are a fixed 372px column of hand-picked type sizes, so a
 * wider window would only stretch the panels and leave 10px text stranded in
 * the middle of them. Dragging the corner makes the widget *bigger*, which is
 * what someone asking to resize a status readout wants.
 *
 * The arithmetic is in screen pixels, not in scale units. Mapping the pointer's
 * travel straight onto a multiplier makes the widget accelerate away at large
 * sizes and crawl at small ones; converting to a width, moving that, and
 * dividing back keeps the corner under the finger at every scale.
 *
 * Double-click resets to 1, because a scale you cannot get back out of is a
 * trap -- there is no menu here to reach for.
 */
const SCALE_BASE = 372      // the shell's unscaled width, which the drag adjusts

/**
 * The drag itself, shared by the corner grip and the wide handle.
 *
 * One implementation with two affordances on it, rather than two sets of
 * arithmetic that would drift apart the first time either end of the range
 * moved. Both are clamped by the same fitted ceiling and the same platform
 * floor, so neither route can be used to break the layout.
 */
function useScaleDrag({ scale, onScale, onStart, onEnd, max }) {
  const drag = useRef(null)
  const latest = useRef(scale)
  latest.current = scale

  const onPointerDown = (e) => {
    if (e.button !== 0) return
    drag.current = { x: e.screenX, y: e.screenY, from: scale }
    e.currentTarget.setPointerCapture(e.pointerId)
    onStart()
  }
  const onPointerMove = (e) => {
    const d = drag.current
    if (!d) return
    // Both axes, so the same gesture works from a corner and from a bar: the
    // travel is the diagonal projection rather than one axis picked
    // arbitrarily.
    const travel = ((e.screenX - d.x) + (e.screenY - d.y)) / 2
    onScale(clampScale((SCALE_BASE * d.from + travel) / SCALE_BASE, max))
  }
  const onPointerUp = (e) => {
    if (!drag.current) return
    drag.current = null
    e.currentTarget.releasePointerCapture?.(e.pointerId)
    onEnd()
    window.nw?.scale(latest.current)   // remembered only once the drag is over
  }
  const onDoubleClick = () => {
    const v = clampScale(SCALE_DEFAULT, max)
    onScale(v)
    window.nw?.scale(v)
  }

  return {
    onPointerDown,
    onPointerMove,
    onPointerUp,
    onPointerCancel: onPointerUp,
    onDoubleClick,
  }
}

function ResizeGrip({ scale, onScale, onStart, onEnd, max, active }) {
  const handlers = useScaleDrag({ scale, onScale, onStart, onEnd, max })

  return (
    <span
      className={'absolute bottom-[2px] right-[2px] grid h-[15px] w-[15px] cursor-nwse-resize '
        + 'place-items-center transition hover:text-ink-2 '
        + (active ? 'text-accent' : 'text-faint')}
      title={`Drag to resize · double-click to reset (${Math.round(scale * 100)}% `
        + `of ${Math.round(max * 100)}% max)`}
      {...handlers}
    >
      <svg viewBox="0 0 16 16" className="h-[11px] w-[11px]" stroke="currentColor" strokeWidth="1.4">
        <path d="M15 9 9 15M15 13l-2 2" strokeLinecap="round" />
      </svg>
    </span>
  )
}

/**
 * The wide handle the Resize button reveals.
 *
 * Same drag, a target the width of the card instead of fifteen pixels in a
 * corner, and it says what size it is at and how large this screen will let it
 * get. Everything scales with it because the mechanism is CSS `zoom`, which is
 * a layout scale -- the type grows with the boxes rather than being stretched.
 */
function ResizeBand({ scale, onScale, onStart, onEnd, max }) {
  const handlers = useScaleDrag({ scale, onScale, onStart, onEnd, max })
  const atMax = scale >= max - 0.001
  const atMin = scale <= SCALE_MIN + 0.001

  return (
    <div
      className="mx-3 mt-2 flex cursor-nwse-resize select-none items-center justify-center gap-2
                 rounded-[7px] border border-white/35 bg-white/[0.14] py-[5px]
                 transition duration-150 hover:bg-white/20"
      title="Drag anywhere along this bar to resize the widget · double-click to reset"
      {...handlers}
    >
      <SectionIcon name="resize" />
      <span className="glass-text text-[10px] font-medium tabular-nums text-ink-2">
        {`Drag to resize · ${Math.round(scale * 100)}%`}
      </span>
      <span className="glass-text text-[9.5px] tabular-nums text-faint">
        {atMax ? 'largest this screen fits'
          : atMin ? 'smallest usable'
            : `max ${Math.round(max * 100)}%`}
      </span>
    </div>
  )
}

function FooterCard({
  s, compact, locked, spinning, onToggleCompact, onToggleMini, onToggleLock, onRefresh,
  scale, maxScale, onScale, resizeMode, onToggleResize, onScaleStart, onScaleEnd,
}) {
  const rise = useRise()
  const { up, busy } = s.netstate ?? { up: true, busy: false }

  return (
    <Card id="foot" {...rise('190ms')}>
      <ResizeGrip
        scale={scale}
        onScale={onScale}
        max={maxScale}
        active={resizeMode}
        onStart={onScaleStart}
        onEnd={onScaleEnd}
      />
      {resizeMode && (
        <ResizeBand
          scale={scale}
          onScale={onScale}
          max={maxScale}
          onStart={onScaleStart}
          onEnd={onScaleEnd}
        />
      )}
      <div className="flex items-center gap-1.5 py-2 pl-3 pr-[18px]">
        {/* The dot reports the adapters, so it reads at the start of the row
            rather than crowded against the button that toggles them. */}
        <NetDot up={up} busy={busy} />
        <span className="glass-text text-[10px] text-muted">
          {busy ? 'working…' : up ? 'network up' : 'network cut'}
        </span>

        <span className="flex-1" />

        <IconButton
          icon="resize"
          label="Resize"
          active={resizeMode}
          onClick={onToggleResize}
        />
        <IconButton
          icon="mini"
          label="Mini bar"
          onClick={onToggleMini}
        />
        <IconButton
          icon={compact ? 'full' : 'compact'}
          label={compact ? 'Full view' : 'Compact view'}
          onClick={onToggleCompact}
        />
        <IconButton
          icon={locked ? 'lock' : 'unlock'}
          label={locked ? 'Unlock position' : 'Lock position'}
          active={locked}
          onClick={onToggleLock}
        />
        <IconButton
          icon="power"
          label={busy ? 'Working…' : up ? 'Cut network' : 'Restore network'}
          onClick={() => send({ cmd: 'net_toggle' })}
          spinning={busy}
          tone={busy ? 'var(--color-warn)' : up ? 'var(--color-good)' : 'var(--color-bad)'}
        />
        <IconButton icon="refresh" label="Refresh now" onClick={onRefresh} spinning={spinning} />
        <IconButton icon="close" label="Quit Net Watch" onClick={() => send({ cmd: 'quit' })} />
      </div>
    </Card>
  )
}

/* ── formatting helpers ────────────────────────────────────────────────────── */

function pingTone(text) {
  const ms = parseFloat(String(text ?? '').replace(/[^\d.]/g, ''))
  if (!Number.isFinite(ms)) return 'text-faint'
  if (ms >= 200) return 'text-bad'
  if (ms >= 90) return 'text-warn'
  return 'text-good'
}

function pickPing(net) {
  if (!net) return { text: '…', tone: 'text-faint' }
  const t1 = pingTone(net.ping1)
  return t1 === 'text-bad'
    ? { text: net.ping2, tone: pingTone(net.ping2) }
    : { text: net.ping1, tone: t1 }
}

/** A coarse age for a cached reading: minutes, then hours. */
function agoLabel(seconds) {
  const m = Math.round(seconds / 60)
  if (m < 60) return `${Math.max(1, m)}m`
  return `${Math.floor(m / 60)}h${m % 60 ? ` ${m % 60}m` : ''}`
}

function scoreTone(score) {
  if (score == null) return 'text-faint'
  if (score >= 55) return 'text-good'
  if (score >= 35) return 'text-warn'
  return 'text-bad'
}

function tzMatch(tz, net) {
  const ipCc = net?.code
  if (!tz?.tz || !ipCc || ipCc === '?') return { text: '…', tone: 'text-faint' }
  if (!tz.tz_code) return { text: 'TZ: region unknown', tone: 'text-faint' }
  if (tz.tz_code === ipCc) return { text: `TZ matches IP (${ipCc})`, tone: 'text-muted' }
  return { text: `⚠ TZ=${tz.tz_code} ≠ IP=${ipCc}`, tone: 'text-warn' }
}
