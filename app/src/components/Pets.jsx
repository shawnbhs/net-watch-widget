import { useCallback, useEffect, useState } from 'react'
import { Card, CardHead } from './Glass.jsx'
import {
  PET_NAME_MAX, PET_SIZE_MAX, PET_SIZE_MIN, PET_SIZE_STEP,
  PET_TASTE_MAX, PET_TASTE_MIN, PET_TASTE_STEP,
  displayTasteKey, usePets,
} from '../pets/store.jsx'
import { ASSET_BASE, OVERLAY_SIZE_FACTOR } from '../pets/engine.js'

/**
 * The pets row.
 *
 * Three things in one card, which is more than any other card here carries, and
 * the reason is that they are the same thing looked at from three distances:
 * what species to add, which pets exist, and where each one of them lives. The
 * picker and the tuning panel are both collapsed by default so the resting card
 * is just the roster -- which in the ordinary case is two or three lines.
 *
 * "Where it lives" is per pet rather than global. One dog on the widget and one
 * fox loose on the desktop is the interesting configuration, and a single switch
 * could not express it.
 */

const MODES = [
  ['widget', 'Widget', 'Walks the cards and hops between them'],
  ['screen', 'Screen', 'Roams the whole desktop'],
]

/** The two-position mode switch, at two sizes: the default, and a roster row. */
function ModeSwitch({ value, onChange, small }) {
  return (
    <span
      className={'inline-flex shrink-0 overflow-hidden rounded-[6px] border border-edge-soft '
        + 'bg-white/[0.06]'}
      role="group"
    >
      {MODES.map(([key, label, hint]) => (
        <button
          key={key}
          type="button"
          title={hint}
          aria-pressed={value === key}
          onClick={() => onChange(key)}
          className={'transition duration-150 '
            + (small ? 'px-1.5 py-[2px] text-[9px] ' : 'px-2 py-[3px] text-[9.5px] ')
            + (value === key
              ? 'bg-gradient-to-b from-[#3d95ff] to-[#1f77f0] font-semibold text-white '
              : 'text-muted hover:bg-white/12 hover:text-ink-2 ')}
        >
          {label}
        </button>
      ))}
    </span>
  )
}

/**
 * THE ONE MIRROR OF THE ENGINE'S SIZE FORMULA. Keep it the only one.
 *
 * This reimplements `applySize()` (engine.js:233). The engine cannot simply be
 * asked for the number: this card is drawn before the pet exists, a screen pet
 * lives in a different renderer process altogether, and the slider has to quote
 * a height for a value the user is still dragging and has not committed. So the
 * formula exists twice by necessity -- and every read-out on this card goes
 * through this single function so that "twice" never becomes "three times".
 *
 * What breaks when it stops mirroring: nothing throws and nothing looks broken.
 * The card just states a height the pet is not drawn at. That is exactly the
 * failure that went unnoticed the last time the chain grew a term, so treat a
 * change to engine.js's size chain as a change to this function.
 *
 * The chain, in the engine's own order (engine.js:233, `applySize`):
 *
 *   opts.size  x OVERLAY_SIZE_FACTOR   screen pets only (overlay.js:257)
 *              x species.scale         manifest; a cat and a crab at the same
 *                                      nominal height do not occupy the space
 *              x sizeFactor            this pet's own multiplier
 *              x world.scale           OMITTED on purpose -- see below
 *              x M(display)            per-display density, NOT knowable here
 *              x K(display)            per-display taste,   NOT knowable here
 *   clamped to  PET_MIN_H..PET_MAX_H   engine.js:86-87, mirrored below
 *
 * `world.scale` is the widget's own zoom. It scales the entire interface, the
 * global slider's "26 px" ignores it too, and a figure that moved every
 * time the window was resized would be unreadable. So every height quoted from
 * here is the one at 100% widget zoom, and so is the clamp.
 *
 * `M` and `K` are 1 EXACTLY for a widget pet, which is a fact and not an
 * assumption: `PetLayer` never calls `world.setDisplays()`, so `displayAt()`
 * returns null (engine.js:1557), `governingDisplay()` is null, and both factors
 * take their `if (!d) return 1` path. A widget height can therefore be stated
 * outright. A screen pet is sized against a display this renderer cannot see at
 * all, which is what `petReadout` below is mostly about.
 *
 * `petBaseHeight` is the chain WITHOUT the clamp, and deliberately NOT rounded:
 * rounding belongs to whoever formats it, and rounding here once cost the
 * millimetre read-out its precision. The clamp is a separate function because
 * both read-outs need the unclamped number too -- it is the only thing that
 * tells them the clamp bit at all.
 */
function petBaseHeight(factor, size, full, mode) {
  const base = mode === 'screen' ? Math.round(size * OVERLAY_SIZE_FACTOR) : size
  return base * factor * (full?.species.scale ?? 1)
}

/**
 * The band `applySize` holds a finished sprite height inside, in CSS px.
 *
 * Copied from `PET_MIN_H`/`PET_MAX_H` (engine.js:86-87), which are module-
 * private there and so cannot be imported. They are part of the size formula
 * this file exists to mirror, and leaving them out is what let the card quote
 * heights above 1300 px for a pet the engine will only ever draw at 512.
 */
const PET_MIN_H = 16
const PET_MAX_H = 512

/** The chain WITH the engine's clamp. This IS the drawn height for a widget pet. */
function petDrawnHeight(base) {
  return base < PET_MIN_H ? PET_MIN_H : base > PET_MAX_H ? PET_MAX_H : base
}

/**
 * What a size read-out is entitled to claim, now that size is per display.
 *
 * A widget pet lives inside this window, on whatever single screen this window
 * is on, and is sized against no display at all (see `petBaseHeight`: M and K
 * are provably 1 there). Its height in this window's pixels is a fact and is
 * quoted as one, cap included.
 *
 * A screen pet is not on one screen. It is sized per display precisely so that
 * it stays the same real size everywhere, which means it is a DIFFERENT number
 * of pixels on each monitor and this card has no display list to choose one
 * from. Printing either monitor's pixel count would be publishing one screen's
 * answer as though it were everybody's -- the ambiguity that made the old
 * "N px" wrong rather than merely imprecise. So it prints the quantity that is
 * meant to be single-valued: the physical one, `base / 96` inches.
 *
 * WHY THAT NUMBER IS APPROXIMATE, AND SAYS SO. The millimetre identity is a
 * property of ONE branch of `engine.js: displayScale()` -- `M = ppi/(96*S)`,
 * where the `ppi` cancels and the real height falls out exactly. The other two
 * branches are not physical at all:
 *
 *   - `M = sf/S` when there is no EDID reading. Windows' scale percentage is a
 *     user-facing setting, not the panel's dot pitch, so this recovers most of
 *     the error and nothing like all of it.
 *   - `M = 1` when there is neither, which is no correction whatsoever.
 *
 * And the fallbacks are the COMMON case, not the exotic one: `ppi.js` is
 * Windows-only, spawns PowerShell for the EDID, and returns 0 for VMs, for
 * projectors, for anything outside 30-800 PPI, for two identical monitors
 * (ambiguous on purpose), and for the whole window between process start and
 * the first async probe landing. On every one of those paths the millimetre
 * figure is the intent rather than the measurement.
 *
 * Two further terms are unknowable here and are NOT silently folded in:
 *
 *   - `K`, the per-screen taste multiplier, is 0.5x..2x and belongs to a screen
 *     this renderer cannot enumerate. The "This screen" slider sets it for the
 *     ONE monitor this window is on; a pet on any other keeps that screen's.
 *   - `M` is itself clamped to 0.25..4, so a wild reading is bounded but the
 *     figure is then bounded with it.
 *
 * What IS knowable is the engine's own `clamp(..., PET_MIN_H, PET_MAX_H)`, and
 * it is knowable because the band is in plain CSS px, with no display in it. At
 * either end the pet stops responding to the slider, and the millimetre claim
 * collapses with it: the band is in pixels, so a pet held at 512 px is a
 * DIFFERENT real size on every monitor -- the exact property the mm figure
 * exists to promise. Quoting millimetres there would state a size the pet is
 * not drawn at, so the limit is reported instead, in the pixels it is written
 * in, because that is the term actually in force.
 *
 * `\u2248` rather than a bare figure, on every uncapped screen read-out. The
 * card cannot know which branch produced `M` for any given monitor, so it
 * cannot promise the exact case even when the exact case is what happens.
 */
function petReadout(factor, size, full, mode) {
  const base = petBaseHeight(factor, size, full, mode)
  const drawn = petDrawnHeight(base)
  // Named separately rather than as one \u201cclamped\u201d flag: \u201cthe slider stopped
  // having an effect\u201d is the thing being reported, and at which END it stopped
  // is what tells the user whether to turn it down or give up on going smaller.
  const limit = drawn > base ? 'min' : drawn < base ? 'max' : null
  const value = `${factor.toFixed(1)}x`
  if (mode !== 'screen') {
    return `${value} \u00b7 ${Math.round(drawn)} px${limit ? ` (${limit})` : ''}`
  }
  // No millimetres at either limit: the band is in pixels, so a pet held at it
  // is a DIFFERENT real size on every monitor, which is the one thing the mm
  // figure exists to promise. The pixel limit is the term actually in force.
  if (limit) return `${value} \u00b7 ${limit === 'max' ? 'capped' : 'floored'} at ${Math.round(drawn)} px`
  return `${value} \u00b7 \u2248${(base / 96 * 25.4).toFixed(1)} mm`
}

/** The caveats behind a size read-out, spelled out rather than implied. */
const SIZE_HINT = {
  widget: 'Height on the card, in this window\u2019s own pixels. Exact: a pet in '
    + 'the widget is not sized against a monitor. Pets are never drawn smaller '
    + 'than 16 px or larger than 512 px, and the read-out says \u201cmin\u201d or '
    + '\u201cmax\u201d when that limit, rather than the slider, is what you are seeing.',
  screen: 'Roughly the real height on the glass. A desktop pet is drawn at a '
    + 'different number of pixels on each screen so that it comes out about '
    + 'the same real size on all of them, which is why this is stated in '
    + 'millimetres and not in pixels. Treat it as the intended size rather '
    + 'than a measurement: it is exact only on monitors whose true pixel '
    + 'density could be read, and on the rest the display\u2019s scale setting '
    + 'is used as an approximation, or no correction is made at all. The '
    + 'per-monitor adjustment under \u201cThis screen\u201d is a separate number on '
    + 'every screen and is not counted here. Pets are also never drawn larger '
    + 'than 512 px or smaller than 16 px on any one screen; at that limit the '
    + 'real size stops matching across monitors, so the read-out shows the '
    + 'pixel limit instead of a millimetre figure it could not keep.',
}

/**
 * The display this window is on, in the shape `displayTasteKey()` documents.
 *
 * The widget renderer is handed no display list. Of the three things it could
 * key a per-screen setting on, two are wrong in ways that fail silently:
 *
 * - `nw.onDisplay` carries the *work area* and no scale factor at all, so the
 *   key it produces (`panel:<workW>x<workH>@1`) is a different string from the
 *   one anybody holding a real Electron `Display` derives for the same panel.
 *   The setting would be stored under a key nothing else ever reads.
 * - The overlay payload's display entries carry CSS px already cut to the
 *   window frame, which is not the panel's own rectangle either.
 *
 * So it is built from `window.screen` (the DIP size of the display this window
 * is on) and `devicePixelRatio` (that display's scale factor), shaped as
 * `{ bounds, scaleFactor }` -- exactly an Electron `Display`, so the key comes
 * out of the *same* branch of `displayTasteKey()` as any other caller's.
 *
 * Returns `null` when neither number is usable. A null display is not an error
 * to report up: `displayTaste(null)` is already 1.0, which is what an
 * unidentifiable screen means.
 */
function readThisDisplay() {
  const w = Number(window.screen?.width)
  const h = Number(window.screen?.height)
  const sf = Number(window.devicePixelRatio)
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return null
  return {
    bounds: { width: w, height: h },
    scaleFactor: Number.isFinite(sf) && sf > 0 ? sf : 1,
  }
}

/**
 * That display, kept current.
 *
 * Three events, because no one of them covers the other two: the widget walking
 * onto another monitor (`nw.onDisplay`, the only announcement of it there is), a
 * resolution change under a stationary window (`resize`), and a scale-factor
 * change, which fires neither of those -- `devicePixelRatio` has no event, so it
 * is watched through a media query pinned to its current value, re-armed
 * whenever that value moves.
 *
 * The state is replaced only when the derived KEY changes, not whenever a new
 * object is built. Every one of these events fires in bursts, and a fresh object
 * identity per event would re-render the card, re-arm the query and re-subscribe
 * for a screen that never changed.
 */
function useThisDisplay() {
  const [display, setDisplay] = useState(readThisDisplay)

  const refresh = useCallback(() => {
    setDisplay((prev) => {
      const next = readThisDisplay()
      return displayTasteKey(next) === displayTasteKey(prev) ? prev : next
    })
  }, [])

  useEffect(() => {
    const off = window.nw?.onDisplay?.(refresh)
    window.addEventListener('resize', refresh)
    const dpr = window.devicePixelRatio
    const mq = Number.isFinite(dpr) && dpr > 0
      ? window.matchMedia?.(`(resolution: ${dpr}dppx)`)
      : null
    mq?.addEventListener?.('change', refresh)
    return () => {
      off?.()
      window.removeEventListener('resize', refresh)
      mq?.removeEventListener?.('change', refresh)
    }
    // `display` is a dependency so the media query is re-pinned to the new
    // devicePixelRatio after a scale change; without it the query stays bound to
    // the old value and the second change in a row is never heard.
  }, [refresh, display])

  return display
}

/**
 * The per-screen size adjustment, K(d).
 *
 * Why this control exists at all, and why it is not simply another global size
 * slider: the engine already corrects every desktop pet for the density of the
 * panel it is standing on, so a pet is the same number of millimetres of glass
 * on every monitor. That is provably right and can still look wrong, because the
 * eye receives a visual ANGLE -- a monitor at arm's length subtends less of one
 * than a laptop panel half as far away. Viewing distance cannot be measured, so
 * the angular term is left to the person doing the viewing, per screen. A single
 * global number cannot express it: n screens at n distances have n degrees of
 * freedom, and turning one global knob down to fix the near screen re-breaks the
 * far one, forever.
 *
 * It says "desktop pets" because that is the truth: the correction is applied
 * per display by the desktop overlay. A widget pet lives inside this window and
 * is not sized against a display at all, so a screen adjustment would not move
 * it, and a control that claims to resize everything while resizing half of it
 * is worse than one that states its scope.
 */
function ScreenTaste({ display, value, tuned, others, onChange, onReset, onResetAll }) {
  // No durable key means nothing can be stored -- not an error, but it must be
  // said, because the alternative is a slider that moves and silently forgets.
  if (!display) {
    return (
      <p className="glass-text px-3 pt-1 text-[9.5px] leading-snug text-faint">
        This screen cannot be identified well enough to remember a setting for
        it, so desktop pets use the standard size here. Every other screen is
        unaffected.
      </p>
    )
  }
  return (
    <>
      {/* `label` is a JS expression, not a bare attribute string: JSX does NOT
          process escape sequences inside attribute quotes, so "\u00b7" written
          there renders as six literal characters. */}
      <Slider
        label={'Size \u00b7 this screen'}
        value={value}
        min={PET_TASTE_MIN}
        max={PET_TASTE_MAX}
        step={PET_TASTE_STEP}
        onChange={onChange}
        hint={'Nudge desktop pets on THIS monitor only, for how far away you sit '
          + 'from it. Pets are already held at the same real size on every '
          + 'screen; this is the part that measurement cannot do -- a monitor '
          + 'further away looks smaller at the same real size. Other screens '
          + 'keep their own setting, and the widget\u2019s own pets are not '
          + 'affected.'}
        // The word matters: 1.00x is "no opinion", which is a different
        // statement from a number somebody chose, and the row below says which
        // of the two this screen is in.
        format={(v) => `${v.toFixed(2)}x`}
      />
      <div className="flex flex-wrap items-baseline gap-x-2 px-3 pb-[2px]">
        <span className="glass-text flex-1 text-[9px] leading-snug text-faint">
          {tuned
            ? 'Set for this screen. Remembered per monitor, so unplugging and '
              + 'plugging it back in keeps it.'
            : 'Standard size on this screen.'}
        </span>
        {tuned && (
          <button
            type="button"
            onClick={onReset}
            className="shrink-0 text-[9px] text-faint transition hover:text-accent"
            title={'Forget this screen\u2019s adjustment and use the standard size'}
          >
            Reset
          </button>
        )}
        {others > 0 && (
          <button
            type="button"
            onClick={onResetAll}
            className="shrink-0 text-[9px] text-faint transition hover:text-accent"
            title={`Forget the adjustment on every screen, including ${others} `
              + 'not in use right now'}
          >
            Reset all screens
          </button>
        )}
      </div>
    </>
  )
}

/**
 * A labelled slider. `format` turns the raw value into the read-out.
 *
 * The header wraps rather than clipping. A read-out that says "2.0x \u00b7 14.7 mm"
 * is longer than the old "2.0x \u00b7 56 px", and a unit silently cut off the end
 * of a line is worse than no read-out at all -- the number would still look
 * complete and mean something else.
 */
function Slider({ label, value, min, max, step, onChange, format, hint }) {
  return (
    <div className="px-3 py-[3px]" title={hint}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-2">
        <span className="glass-text text-[9.5px] uppercase tracking-wide text-muted">
          {label}
        </span>
        <span className="glass-text text-[10px] tabular-nums text-ink-2">
          {format(value)}
        </span>
      </div>
      <input
        type="range"
        className="nw-range mt-1 w-full"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  )
}

/** A small checkbox row, in the card's own type rather than the browser's. */
function Check({ label, checked, onChange, hint }) {
  return (
    <label
      className="flex cursor-pointer items-center gap-2 px-3 py-[2px]"
      title={hint}
    >
      <input
        type="checkbox"
        className="nw-check"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="glass-text text-[10px] text-ink-2">{label}</span>
    </label>
  )
}

/**
 * What the card says when there is no catalogue to pick a pet from.
 *
 * It used to say "no sprites found" on a dead button, which was one guess at a
 * cause presented as the only one, with nothing to do about it -- the state the
 * user actually hit was four healthy rows and no pets, and the card explained
 * none of it. The situations the store can be in look identical from the
 * outside and need entirely different things from the user, so each is named:
 * still looking (wait), and settled with a reason (press this). The wording of
 * the reason comes from the store, because the store is the half that knows
 * whether the catalogue failed to arrive or arrived with nothing in it.
 */
function CatalogueNotice({ error, settled, onRetry }) {
  const looking = !settled
  return (
    <div className="px-3 pt-1.5">
      <div className="rounded-[7px] border border-edge-soft bg-white/[0.06] px-2 py-[5px]">
        <p className="glass-text text-[10px] leading-snug text-ink-2">
          {looking
            ? 'Looking for the pets\u2026'
            : (error || 'No pets are available, and no reason was given.')}
        </p>
        {/* A failure that is still being retried is not the same as one that
            has stopped, and only the second one wants a button pressed. */}
        {looking && error && (
          <p className="glass-text mt-[3px] text-[9.5px] leading-snug text-faint">
            The first try did not work. Trying again on its own -- this can take
            a few seconds.
          </p>
        )}
        {!looking && (
          <button
            type="button"
            onClick={onRetry}
            className={'mt-[5px] w-full rounded-[6px] border border-edge-soft bg-white/[0.08] '
              + 'py-[4px] text-[10px] font-medium text-ink-2 transition duration-150 '
              + 'hover:border-white/35 hover:bg-white/20 hover:text-ink active:scale-[0.99]'}
          >
            Look again
          </button>
        )}
      </div>
    </div>
  )
}

/**
 * The pets that are in the roster but have no sprite in the catalogue.
 *
 * Named one by one rather than counted, because "one pet is unavailable" and
 * "Bramble (fox) is unavailable" are answerable questions of very different
 * difficulty -- and this state is the one that is genuinely per-pet, as opposed
 * to the whole catalogue being gone. The rows are kept rather than pruned, so
 * the message says so: the pet is waiting, not lost.
 */
function MissingSpeciesNotice({ rows }) {
  if (rows.length === 0) return null
  const one = rows.length === 1
  const named = rows
    .map((r) => (r.name ? `${r.name} (${r.speciesId})` : r.speciesId))
    .join(', ')
  return (
    <p className="glass-text mt-1.5 px-3 text-[9.5px] leading-snug text-faint">
      {one ? 'This pet is ' : 'These pets are '}
      in your roster, but {one ? 'its' : 'their'} artwork is not in the
      catalogue, so {one ? 'it cannot' : 'they cannot'} be shown anywhere:
      {' '}{named}. Nothing has been deleted -- putting that sprite pack back
      brings {one ? 'it' : 'them'} back with the same name and size.
    </p>
  )
}

export function PetsCard({ rise }) {
  const {
    manifest, ready, manifestError, manifestSettled, missingSpecies, retryManifest,
    pets, defaultMode, opts, taste,
    add, remove, setMode, setPetSize, setPetName, setDefaultMode, setOpts, clear, resolve,
    displayTaste, hasDisplayTaste, setDisplayTaste, resetDisplayTaste, resetAllDisplayTaste,
  } = usePets()

  // The screen this window is on, and what has been set for it. `displayTaste`
  // answers for a null display too (1.0), so none of these three need a guard.
  const thisDisplay = useThisDisplay()
  const thisKey = displayTasteKey(thisDisplay)
  // Counted rather than listed: the other screens are identified by a key, not
  // by a name anybody would recognise, and "3 other screens" is the most this
  // card can honestly say about monitors it cannot see.
  const otherScreens = Object.keys(taste ?? {}).filter((k) => k !== thisKey).length

  const [picking, setPicking] = useState(false)
  const [tuning, setTuning] = useState(false)
  const [chosen, setChosen] = useState(null)     // species id, while picking
  const [colour, setColour] = useState(null)
  const [sizing, setSizing] = useState(null)     // pet id, while resizing one
  // Typed before the species is picked, because one of the two add paths does
  // not pause for a second click: a species with a single variant is added the
  // moment its tile is clicked. A name box that only appeared next to the Add
  // button would be unreachable for exactly those pets.
  const [naming, setNaming] = useState('')

  const species = manifest.find((s) => s.id === chosen) || null

  const choose = (sp) => {
    // Picking the species already open again closes it, so a mis-click is one
    // click to undo rather than a hunt for a close button.
    if (chosen === sp.id) { setChosen(null); setColour(null); return }
    setChosen(sp.id)
    setColour(sp.variants[0]?.color ?? null)
    // One variant is not a choice, so adding it is one click, not two.
    if (sp.variants.length === 1) {
      add(sp.id, sp.variants[0].color, undefined, naming)
      setChosen(null)
      setNaming('')
      setPicking(false)
    }
  }

  const place = () => {
    if (!species || !colour) return
    add(species.id, colour, undefined, naming)
    setChosen(null)
    setColour(null)
    setNaming('')
    setPicking(false)
  }

  return (
    <Card id="pets" {...rise('175ms', 'pb-2')}>
      <CardHead
        icon="paw"
        right={(
          <span className="flex items-center gap-2">
            {pets.length > 0 && (
              <button
                type="button"
                onClick={clear}
                className="text-[9.5px] text-faint transition hover:text-accent"
                title="Remove every pet"
              >
                Clear
              </button>
            )}
            <button
              type="button"
              onClick={() => setTuning((t) => !t)}
              aria-pressed={tuning}
              title="How the pets behave"
              className={'text-[13px] leading-none transition '
                + (tuning ? 'text-accent' : 'text-muted hover:text-accent')}
            >
              &#8943;
            </button>
          </span>
        )}
      >
        Pets
      </CardHead>

      {/* ── where new pets go ─────────────────────────────────────────────── */}
      <div className="flex items-center gap-2 px-3 py-[2px]">
        <span className="glass-text flex-1 text-[10px] text-muted">New pets live in the</span>
        <ModeSwitch value={defaultMode} onChange={setDefaultMode} />
      </div>

      {/* ── the picker ────────────────────────────────────────────────────── */}
      {/* No catalogue is not a disabled Add button -- a greyed-out control is a
          thing the user is being refused, with no way to find out why. It is
          replaced outright by the explanation and whatever action fits it. */}
      {ready ? (
        <div className="px-3 pt-1.5">
          <button
            type="button"
            onClick={() => { setPicking((p) => !p); setChosen(null) }}
            className={'w-full rounded-[7px] border border-edge-soft bg-white/[0.08] py-[5px] '
              + 'text-[10px] font-medium text-ink-2 transition duration-150 '
              + 'hover:border-white/35 hover:bg-white/20 hover:text-ink active:scale-[0.99]'}
          >
            {picking ? 'Close' : '+  Add a pet'}
          </button>
        </div>
      ) : (
        <CatalogueNotice
          error={manifestError}
          settled={manifestSettled}
          onRetry={retryManifest}
        />
      )}

      {picking && ready && (
        <>
          <div className="mt-1.5 px-3">
            <input
              type="text"
              value={naming}
              maxLength={PET_NAME_MAX}
              spellCheck={false}
              onChange={(e) => setNaming(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && species && colour) place() }}
              placeholder="Name this pet (optional)"
              aria-label="Name for the pet you are about to add"
              className="glass-text w-full rounded-[6px] border border-edge-soft bg-white/[0.06]
                         px-2 py-[3px] text-[10px] text-ink outline-none transition
                         placeholder:text-faint focus:border-white/35"
            />
          </div>
          <div
            className="mt-1.5 grid max-h-[104px] grid-cols-7 gap-1 overflow-y-auto px-3"
            role="listbox"
            aria-label="Pet species"
          >
            {manifest.map((sp) => (
              <button
                key={sp.id}
                type="button"
                role="option"
                aria-selected={chosen === sp.id}
                title={sp.label}
                onClick={() => choose(sp)}
                className={'grid aspect-square place-items-center rounded-[6px] border '
                  + 'transition duration-150 active:scale-[0.94] '
                  + (chosen === sp.id
                    ? 'border-white/45 bg-white/25 '
                    : 'border-edge-soft bg-white/[0.06] hover:border-white/30 hover:bg-white/15 ')}
              >
                <img
                  src={ASSET_BASE + (sp.variants[0]?.icon ?? '')}
                  alt=""
                  className="h-[20px] w-[20px] object-contain"
                  style={{ imageRendering: 'pixelated' }}
                />
              </button>
            ))}
          </div>

          {species && species.variants.length > 1 && (
            <div className="mt-1.5 flex flex-wrap items-center gap-1 px-3">
              {species.variants.map((v) => (
                <button
                  key={v.color}
                  type="button"
                  title={`${species.label} · ${v.label}`}
                  onClick={() => setColour(v.color)}
                  className={'rounded-[6px] border px-1.5 py-[2px] text-[9px] transition '
                    + (colour === v.color
                      ? 'border-white/45 bg-white/25 text-ink '
                      : 'border-edge-soft bg-white/[0.06] text-muted hover:bg-white/15 ')}
                >
                  {v.label}
                </button>
              ))}
              <button
                type="button"
                onClick={place}
                className="ml-auto rounded-[7px] border border-white/35 bg-gradient-to-b
                           from-[#3d95ff] to-[#1f77f0] px-2.5 py-[3px] text-[9.5px]
                           font-semibold text-white transition hover:brightness-110
                           active:scale-[0.96]"
              >
                Add {species.label}
              </button>
            </div>
          )}
        </>
      )}

      {/* ── the roster ────────────────────────────────────────────────────── */}
      {pets.length === 0 ? (
        <div className="glass-text mt-1.5 px-3 text-[10px] text-faint">
          No pets yet. One in the widget walks the cards; one on the screen roams
          the desktop.
        </div>
      ) : (
        <ul className="mt-1.5">
          {pets.map((row) => {
            const full = resolve(row)
            return (
              <li key={row.id}>
                <div className="flex items-center gap-2 px-3 py-[2px]">
                  {/* Nothing rather than an empty `src`, which a file:// page
                      resolves back to the document itself. */}
                  {full ? (
                    <img
                      src={ASSET_BASE + full.variant.icon}
                      alt=""
                      className="h-[16px] w-[16px] shrink-0 object-contain"
                      style={{ imageRendering: 'pixelated' }}
                    />
                  ) : <span className="h-[16px] w-[16px] shrink-0" />}
                  {/* The name is the disclosure. A row carries three controls
                      already, and a fourth button for something adjusted once
                      and then left alone would cost every row the width. */}
                  <button
                    type="button"
                    onClick={() => setSizing((id) => (id === row.id ? null : row.id))}
                    title={full
                      ? `${full.species.label} · ${full.variant.label}`
                        + `${row.name ? ` — named ${row.name}` : ''}`
                        + ' · click to rename or resize'
                      : `No artwork in the catalogue for "${row.speciesId}", so this `
                        + 'pet cannot be shown · click to rename or resize'}
                    aria-expanded={sizing === row.id}
                    className="glass-text min-w-0 flex-1 truncate text-left text-[10.5px]
                               text-ink-2 transition hover:text-ink"
                  >
                    {row.name || (full ? full.species.label : row.speciesId)}
                    {full && full.species.variants.length > 1 && (
                      <span className="text-faint"> · {full.variant.label}</span>
                    )}
                    {row.size !== 1 && (
                      <span className="text-faint tabular-nums"> · {row.size}x</span>
                    )}
                  </button>
                  <ModeSwitch
                    small
                    value={row.mode}
                    onChange={(m) => setMode(row.id, m)}
                  />
                  <button
                    type="button"
                    onClick={() => remove(row.id)}
                    title="Remove this pet"
                    aria-label={`Remove ${full?.species.label ?? 'pet'}`}
                    className="shrink-0 px-[2px] text-[12px] leading-none text-faint
                               transition hover:text-bad"
                  >
                    &#10005;
                  </button>
                </div>
                {sizing === row.id && (
                  <div className="pl-[18px]">
                    {/* Rename lives here rather than behind a click on the name
                        itself: that click already opens this panel, and one
                        control cannot mean two things. */}
                    <div className="px-3 pt-[3px]">
                      <input
                        type="text"
                        value={row.name}
                        maxLength={PET_NAME_MAX}
                        spellCheck={false}
                        onChange={(e) => setPetName(row.id, e.target.value)}
                        onBlur={(e) => setPetName(row.id, e.target.value.trim())}
                        placeholder={full ? full.species.label : 'Name'}
                        aria-label={`Name for this ${full?.species.label ?? 'pet'}`}
                        className="glass-text w-full rounded-[6px] border border-edge-soft
                                   bg-white/[0.06] px-2 py-[2px] text-[10px] text-ink
                                   outline-none transition placeholder:text-faint
                                   focus:border-white/35"
                      />
                    </div>
                    {/* PET_SIZE_MIN..PET_SIZE_MAX must equal the interval the
                        engine's petFactor() accepts (engine.js:88, 0.1..8).
                        They disagreed: this control ran 0.5..4, so the top and
                        bottom of the supported range were unreachable, and a
                        value restored from storage outside 0.5..4 was legal to
                        the engine while this slider could neither show nor
                        undo it. The constants are store.jsx's, and its
                        petSize() sanitiser is what actually gates writes, so
                        widening here alone would only make the control lie in
                        the other direction. */}
                    <Slider
                      label="Size"
                      value={row.size}
                      min={PET_SIZE_MIN}
                      max={PET_SIZE_MAX}
                      step={PET_SIZE_STEP}
                      onChange={(v) => setPetSize(row.id, v)}
                      hint={SIZE_HINT[row.mode] ?? SIZE_HINT.widget}
                      // Both numbers, because neither answers the question on
                      // its own: the multiplier is what is being set, and the
                      // size is what will actually be there.
                      format={(v) => petReadout(v, opts.size, full, row.mode)}
                    />
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}

      <MissingSpeciesNotice rows={missingSpecies} />

      {/* ── tuning ────────────────────────────────────────────────────────── */}
      {tuning && (
        <div className="mt-1.5 border-t border-edge-soft pt-1.5">
          {/* "base" rather than a bare "px": this is the reference height the
              whole chain starts from, not the height of any pet on any screen.
              Every pet then differs from it by its species scale and its own
              multiplier, a desktop pet by 1.7x again, and each monitor by its
              own correction -- so the plain "N px" this used to print was the
              height of nothing that is actually drawn. */}
          <Slider
            label="Size · all pets"
            value={opts.size}
            min={16}
            max={100}
            step={1}
            onChange={(size) => setOpts({ size })}
            hint={'The reference height every pet is measured from, before its '
              + 'species scale, its own multiplier, the desktop enlargement and '
              + 'each monitor\u2019s own correction. Each pet\u2019s resulting size is '
              + 'shown on its own row.'}
            format={(v) => `${v} px base`}
          />
          <ScreenTaste
            display={thisDisplay}
            value={displayTaste(thisDisplay)}
            tuned={hasDisplayTaste(thisDisplay)}
            others={otherScreens}
            onChange={(v) => setDisplayTaste(thisDisplay, v)}
            onReset={() => resetDisplayTaste(thisDisplay)}
            onResetAll={resetAllDisplayTaste}
          />
          <Slider
            label="Speed"
            value={opts.speed}
            min={0.2}
            max={2.5}
            step={0.1}
            onChange={(speed) => setOpts({ speed })}
            format={(v) => `${v.toFixed(1)}x`}
          />
          <Slider
            label="Liveliness"
            value={opts.liveliness}
            min={0}
            max={100}
            step={5}
            onChange={(liveliness) => setOpts({ liveliness })}
            format={(v) => (v < 30 ? 'lazy' : v > 70 ? 'hyper' : 'balanced')}
          />
          <Check
            label="React to the cursor"
            checked={opts.follow}
            onChange={(follow) => setOpts({ follow })}
            hint="Pets notice the pointer and sometimes chase it"
          />
          <Check
            label="Dance breaks"
            checked={opts.dance}
            onChange={(dance) => setOpts({ dance })}
            hint="A pet stops where it is and has a shake"
          />
          <Check
            label="Contact shadows"
            checked={opts.shadows}
            onChange={(shadows) => setOpts({ shadows })}
          />
          <Check
            label="Reduced motion"
            checked={opts.calm}
            onChange={(calm) => setOpts({ calm })}
            hint="Stop the wandering and keep the pets still"
          />
          <p className="glass-text mt-1 px-3 text-[9.5px] leading-snug text-faint">
            Drag a pet to pick it up. Double-click one to send it between the
            widget and the desktop. Right-click a widget pet to hop it up a card.
          </p>
        </div>
      )}
    </Card>
  )
}
