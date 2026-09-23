import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
} from 'react'

/**
 * The pet roster, and the one place that knows about both halves of it.
 *
 * A pet is a row here whether it is walking on the cards or loose on the
 * desktop -- the mode is a property of the pet, not a separate list. That is
 * what makes "in the widget" and "all over the screen" a per-pet switch rather
 * than a global one: flipping a row's mode moves it between two renderers, and
 * the roster does not change shape.
 *
 * The widget owns this. The overlay in the other process is a *view* of the
 * `screen` rows, pushed to it whenever they change; it never adds or removes
 * anything, and the single thing it sends back is "this one was double-clicked,
 * send it home".
 */

const KEY = 'netwatch.pets.v1'

/**
 * The range of a single pet's size multiplier.
 *
 * A multiplier rather than a height, so the global Size slider stays the master
 * control: moving it still resizes the whole roster and keeps whatever relative
 * differences have been set. The range is wide enough that one pet can be taken
 * to roughly the global ceiling, or shrunk well below the rest of the roster, on
 * its own -- without having to drag the global slider and everyone else with it.
 *
 * These MUST equal the band the engine's petFactor() accepts (engine.js, 0.1..8),
 * because that is the other end of the same value: petSize() below gates every
 * write, petFactor() re-clamps on read, and the per-pet Size slider is bound to
 * these constants. While they disagreed (this ran 0.5..4) the top and bottom of
 * the supported range were unreachable from the UI, and a value restored from
 * storage outside 0.5..4 was legal to the engine while the slider could neither
 * show nor undo it. Widen in all three places or none.
 *
 * The extremes stay contained downstream: the engine holds the finished sprite
 * height inside PET_MIN_H..PET_MAX_H however this multiplier and the global size
 * multiply out.
 */
export const PET_SIZE_MIN = 0.1
export const PET_SIZE_MAX = 8
export const PET_SIZE_STEP = 0.1

/**
 * The longest name a pet may carry.
 *
 * The roster row is one line in a card barely 300px wide, and the name shares
 * it with a mode switch and a remove button. Past this the name is not a name
 * any more, it is a paragraph that pushes the controls off the row.
 */
export const PET_NAME_MAX = 24

/**
 * A pet's own name: length-capped, and '' when it has none.
 *
 * Deliberately does NOT trim. This runs on every keystroke of the rename
 * field, and trimming there makes a space impossible to type: the value goes
 * back to the input with the trailing space already gone, so `Sir ` becomes
 * `Sir` and the next letter lands against it -- you get `SirHops` and cannot
 * see why. Trimming belongs at the points where a name is committed once:
 * when a pet is added, and when the rename field is left.
 */
export function petName(v) {
  if (typeof v !== 'string') return ''
  return v.slice(0, PET_NAME_MAX)
}

/** One pet's multiplier, clamped to the range and defaulting to 1. */
export function petSize(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return 1
  return Math.min(PET_SIZE_MAX, Math.max(PET_SIZE_MIN, Math.round(n * 10) / 10))
}

/**
 * The range of a per-display TASTE multiplier, K(d).
 *
 * K is the second and last factor in `H = base * M(d) * K(d)`. `M(d)` is the
 * physical correction -- it makes a pet the same number of millimetres of glass
 * on every panel, which is provably right and can still look wrong, because the
 * eye receives a visual ANGLE and a desk monitor at arm's length subtends less
 * of one than a laptop panel at 500mm. Viewing distance cannot be measured, so
 * the angular term is left to the person who is doing the viewing.
 *
 * Bounded at half and double because the knob exists to correct an apparent-size
 * error of tens of percent, not to be a second global size slider: a wider range
 * only lets a mis-set screen hide the fact that `M(d)` itself is broken, and the
 * two failures then look identical in a bug report.
 */
export const PET_TASTE_MIN = 0.5
export const PET_TASTE_MAX = 2
export const PET_TASTE_STEP = 0.05
export const PET_TASTE_DEFAULT = 1

/**
 * One display's taste multiplier, clamped, with EVERY bad input becoming 1.
 *
 * `1` rather than `0` or a pass-through of the input for the reason the whole
 * fallback chain exists: this number multiplies a sprite height, so `0`, `NaN`
 * and `undefined` all render a pet that is present, hit-testable at zero size
 * and invisible -- the one outcome that reads as "the pets feature is broken"
 * rather than as "this setting is wrong". An absent entry is therefore not a
 * missing value to be reported; it is 1.0, which is exactly what it means.
 *
 * Negatives are rejected rather than clamped up: a negative multiplier is a
 * sign error somewhere upstream, and silently turning it into 0.5 would hide it.
 */
export function petTaste(v) {
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) return PET_TASTE_DEFAULT
  return Math.min(PET_TASTE_MAX, Math.max(PET_TASTE_MIN, Math.round(n * 100) / 100))
}

/**
 * The persistence key for one display's taste value.
 *
 * Deliberately NOT `display.id`. Electron's id is a Win32 handle-derived number
 * that is not stable across a replug or a reboot on Windows, so keying on it
 * means the user unplugs a monitor, plugs it back in, and their tuning is gone
 * with no event to explain it -- a silent reset of an authored setting, which is
 * indistinguishable from the app forgetting on purpose.
 *
 * The key is therefore built from what the PANEL is, in order of durability:
 *
 * 1. `edid` / `monitorId` -- the panel's own identity string. Survives replug,
 *    reboot, rearrangement, and a resolution or scaling change, and tells two
 *    identical monitors apart. Used whenever the platform hands one over.
 * 2. native pixel count plus scale factor -- `2560x1600@1.65`. Survives replug
 *    and rearrangement, which are the common events.
 *
 * Failure modes, both accepted knowingly:
 *
 * - Two IDENTICAL panels with no EDID share one key, so they share one taste
 *   value. The fallback is a wrong-but-harmless 1.0-class value on one screen,
 *   never someone else's tuning leaking in from an unrelated panel.
 * - Changing a panel's resolution or its Windows scaling changes its key, so
 *   the old taste value is orphaned and the display reverts to 1.0. That is the
 *   right direction to fail: a resolution change moves apparent size anyway, so
 *   re-tuning is expected, and the stale entry is inert rather than misapplied.
 *
 * Note what is NOT used: the device-pixel origin. It is the correct key for
 * MATCHING a Win32 monitor to an Electron display within one frame, but it moves
 * whenever the monitors are rearranged in display settings, which is a more
 * frequent event than owning two indistinguishable panels.
 *
 * Accepts an Electron `Display` (DIP `bounds` + `scaleFactor`) or a flattened
 * payload entry (`width`/`height` + `sf`). Returns `null` when the object
 * carries nothing durable, and a null key must be read as "use the default",
 * never stored.
 *
 * There is deliberately ONE derivation of the pixel count, even though a caller
 * might already know the true native figure: Electron reports `bounds` as
 * rounded integer DIPs, so `1552 * 1.65` is 2561 where the panel is really 2560.
 * That is harmless while every caller rounds the same way and fatal the moment
 * two do not, because the same monitor would then have two keys and the user's
 * tuning would appear to depend on which code path asked. The number here is an
 * identity, not a measurement, so it only has to be reproducible.
 */
export function displayTasteKey(d) {
  if (!d || typeof d !== 'object') return null

  const ident = [d.edid, d.monitorId, d.deviceId]
    .find((s) => typeof s === 'string' && s.trim() !== '')
  if (ident) return `edid:${ident.trim().replace(/\s+/g, ' ').slice(0, 96)}`

  const rawSf = Number(d.scaleFactor ?? d.sf)
  const sf = Number.isFinite(rawSf) && rawSf > 0 ? rawSf : 1

  // DIP (or CSS px) times the panel's own scale factor approximates its native
  // pixel count -- the figure that describes the panel rather than the window it
  // happens to be shown through.
  const box = (d.bounds && typeof d.bounds === 'object') ? d.bounds : d
  const w = Number(box.width) * sf
  const h = Number(box.height) * sf
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return null

  return `panel:${Math.round(w)}x${Math.round(h)}@${Math.round(sf * 100) / 100}`
}

/**
 * A whole taste map, sanitised entry by entry.
 *
 * One unreadable entry drops only itself. A stored settings blob is the only
 * copy of work the user did by hand, and this project has already lost user data
 * once by treating a partly-unreadable store as an entirely unreadable one.
 */
export function petTasteMap(v) {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return {}
  const out = {}
  for (const [key, raw] of Object.entries(v)) {
    if (typeof key !== 'string' || key === '') continue
    const n = Number(raw)
    // Unreadable entries are dropped rather than written back as 1.0: absent and
    // 1.0 behave identically when read, and not writing keeps the file honest
    // about which displays the user has actually touched.
    if (!Number.isFinite(n) || n <= 0) continue
    out[key] = petTaste(n)
  }
  return out
}

const DEFAULT_OPTS = {
  /**
   * Drawn height in CSS px, before the widget's own scale.
   *
   * 26 rather than PetRail's 56, because a widget pet stands on a card's top
   * edge and its body reaches up over the card above it. The thinnest cards
   * here -- the title and the footer -- are 43px, so past roughly that height a
   * widget pet stands clear of the topmost card and the window, which is sized
   * to exactly its content, cuts off whatever rises above it.
   *
   * The slider nevertheless runs to 100. The clipping is a real effect but it
   * is not a malfunction, and it only applies to the half of the roster that
   * lives on the cards: a pet set loose on the desktop has a whole screen over
   * its head and nothing to be clipped by, and the overlay scales this figure
   * up by 1.7 besides. Capping the control at what the widget can show would
   * hold the desktop pets to a limit that is none of their business, so the
   * ceiling is left where the bigger of the two views can use it and the
   * default stays at a size the smaller one displays whole.
   */
  size: 26,
  speed: 1,
  liveliness: 50,
  follow: true,
  dance: true,
  shadows: true,
  calm: false,
}

/**
 * `taste` is a map, not a list, and it is a sibling of `opts` rather than a
 * field inside it: `opts` is pushed wholesale to the overlay and spread over
 * `DEFAULT_OPTS`, and a shallow spread would silently replace the whole map with
 * whichever partial copy arrived last.
 *
 * It stores K(d) ONLY. The physical correction M(d) is never stored anywhere,
 * here or otherwise -- it is derived from the live display list on every
 * `display-metrics-changed`. Storing the product instead would fuse a volatile
 * derived number into an authored one: swap a monitor and the user's taste
 * setting is corrupted by a stale scale factor, with no way afterwards to tell
 * which half of the number was theirs.
 */
const EMPTY = { pets: [], defaultMode: 'widget', opts: DEFAULT_OPTS, taste: {} }

/**
 * Salvage as much of the stored blob as parses.
 *
 * Each field is recovered inside its own try, so a single unreadable one costs
 * only itself. The alternative -- one try around the whole object -- means a
 * corrupt `taste` map throws away the roster, the names and the sizes too, which
 * is how this project's secrets store lost data once already. A partially
 * readable settings file is the normal case after a bad write or a hand edit,
 * not an exceptional one.
 */
function load() {
  let saved = null
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return EMPTY
    saved = JSON.parse(raw)
  } catch {
    return EMPTY
  }
  if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return EMPTY

  const next = { ...EMPTY }

  try {
    // `size` is normalised on the way in rather than on the way out: a roster
    // saved before per-pet sizing existed has no such field, and one hand-
    // edited in localStorage can have anything in it.
    //
    // Only the shape can be checked here. Whether the species still exists
    // is a question for the manifest, which is asked for asynchronously and
    // has not arrived yet -- so that check lives in the provider, and a row
    // naming a departed species is reported there rather than dropped here.
    next.pets = Array.isArray(saved.pets)
      ? saved.pets
        .filter((p) => typeof p?.speciesId === 'string' && p.speciesId !== '')
        .map((p) => ({ ...p, size: petSize(p.size), name: petName(p.name).trim() }))
      : []
  } catch (err) { console.error('[pets] stored roster unreadable, starting empty', err) }

  try {
    next.defaultMode = saved.defaultMode === 'screen' ? 'screen' : 'widget'
  } catch { /* falls back to EMPTY's 'widget' */ }

  try {
    const o = saved.opts
    next.opts = { ...DEFAULT_OPTS, ...((o && typeof o === 'object' && !Array.isArray(o)) ? o : {}) }
  } catch (err) { console.error('[pets] stored options unreadable, using defaults', err) }

  try {
    // A blob saved before per-display taste existed has no `taste` at all, and
    // that is not a migration step: an empty map reads as 1.0 for every display,
    // which is precisely the behaviour it had before the field existed. There is
    // nothing to convert and nothing to write back.
    next.taste = petTasteMap(saved.taste)
  } catch (err) { console.error('[pets] stored per-display sizes unreadable, using 1.0', err) }

  return next
}

/**
 * How long to wait before each retry of the species manifest, in ms.
 *
 * A manifest read is the main process walking a sprite directory, so an early
 * failure is usually a slow or momentarily busy disk rather than a permanent
 * absence -- and the cost of not retrying is total: no manifest means no pet is
 * ever resolved, in the widget or on the desktop. The schedule is bounded so
 * that a genuinely missing sprite pack ends in a message the user can act on
 * instead of retrying silently for the life of the session.
 */
const MANIFEST_RETRY_MS = [400, 1200, 3000, 8000]

/**
 * What the user is told when the catalogue does not arrive.
 *
 * Shown rather than swallowed because the failure is otherwise invisible: the
 * roster keeps rendering from localStorage, so the app looks healthy and the
 * pets simply are not there. Each says what failed and what can be done.
 */
const MANIFEST_ERR = {
  failed: 'The pet catalogue could not be loaded, so no pets can be shown. '
    + 'Retry, or restart the widget.',
  empty: 'The pet catalogue loaded but listed no species, so no pets can be shown. '
    + 'Check that the sprite pack is installed, then retry or restart the widget.',
}

const PetsCtx = createContext(null)

let seq = 0
const uid = () => `pet-${Date.now().toString(36)}-${++seq}`

export function PetsProvider({ children }) {
  const [manifest, setManifest] = useState([])
  const [manifestError, setManifestError] = useState(null)
  /**
   * Whether the manifest question has an answer yet -- success or failure.
   *
   * Separate from `ready` because the two callers want different things. The
   * picker wants a usable catalogue (`ready`); the overlay sync wants to know
   * that waiting is over, so that a failed catalogue still produces a real,
   * empty payload instead of a null one that is never sent.
   */
  const [manifestSettled, setManifestSettled] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)
  const [state, setState] = useState(load)

  // The species list is four hundred filenames' worth of structure that cannot
  // change while the app runs, so it is asked for once and kept -- but "once"
  // has to mean once *successfully*. A single rejected call used to be
  // swallowed here, which left the manifest empty for the rest of the session
  // and, through it, both renderers switched off with nothing said anywhere.
  useEffect(() => {
    let alive = true
    let timer = null
    let attempt = 0

    const settle = (err) => {
      if (!alive) return
      setManifestError(err)
      setManifestSettled(true)
    }

    const again = (err) => {
      if (!alive) return
      const wait = MANIFEST_RETRY_MS[attempt]
      attempt += 1
      if (wait === undefined) { settle(err); return }
      // Reported while the retries are still in flight, so a slow load explains
      // itself rather than looking like an empty world; cleared on success.
      setManifestError(err)
      timer = setTimeout(run, wait)
    }

    const run = () => {
      let pending
      try {
        pending = window.nw?.petManifest?.()
      } catch (err) {
        console.error('[pets] pet manifest request threw', err)
        again(MANIFEST_ERR.failed)
        return
      }
      if (!pending) { again(MANIFEST_ERR.failed); return }
      Promise.resolve(pending).then((list) => {
        if (!alive) return
        if (Array.isArray(list) && list.length > 0) {
          setManifest(list)
          setManifestError(null)
          setManifestSettled(true)
          return
        }
        // An empty list is as fatal as a rejection and arrives the same way, so
        // it is retried the same way.
        console.error('[pets] pet manifest came back empty', list)
        again(MANIFEST_ERR.empty)
      }, (err) => {
        if (!alive) return
        console.error('[pets] pet manifest failed to load', err)
        again(MANIFEST_ERR.failed)
      })
    }

    run()
    return () => { alive = false; if (timer) clearTimeout(timer) }
  }, [reloadKey])

  /**
   * Ask for the catalogue again, from the top of the backoff schedule.
   *
   * The automatic retries are bounded, so there has to be a way back from a
   * failure that outlasted them short of restarting the app.
   */
  const retryManifest = useCallback(() => {
    setManifestError(null)
    setManifestSettled(false)
    setReloadKey((k) => k + 1)
  }, [])

  const byId = useMemo(() => {
    const m = new Map()
    for (const sp of manifest) m.set(sp.id, sp)
    return m
  }, [manifest])

  /** Fill a roster row out into the species and variant records it names. */
  const resolve = useCallback((row) => {
    const species = byId.get(row.speciesId)
    if (!species) return null
    const variant = species.variants.find((v) => v.color === row.color) || species.variants[0]
    if (!variant) return null
    return { ...row, species, variant }
  }, [byId])

  /**
   * Roster rows whose species is not in the loaded catalogue.
   *
   * Membership, not truthiness: a roster saved while a sprite pack was
   * installed still names those species after it is removed or renamed, and
   * such a row resolves to null -- filtered out of the overlay payload below
   * and dropped by the widget world's resolve guard. It would exist only as a
   * roster row that draws nothing anywhere, with no explanation.
   *
   * The rows are kept rather than pruned: deleting them would throw away pets,
   * names and sizes the user never asked to lose, and putting the sprite pack
   * back restores them exactly. They are named here instead, so the roster can
   * show which pets are unavailable and why.
   *
   * Empty while the catalogue itself is missing -- then every pet is
   * "missing", which is the catalogue's failure to report, not each pet's.
   */
  const missingSpecies = useMemo(() => {
    if (!manifestSettled || byId.size === 0) return []
    return state.pets
      .filter((p) => !byId.has(p.speciesId))
      .map((p) => ({ id: p.id, name: p.name, speciesId: p.speciesId }))
  }, [state.pets, byId, manifestSettled])

  useEffect(() => {
    try { localStorage.setItem(KEY, JSON.stringify(state)) } catch { /* private mode */ }
  }, [state])

  const actions = useMemo(() => ({
    add(speciesId, color, mode, name) {
      setState((s) => ({
        ...s,
        pets: [...s.pets, {
          id: uid(),
          speciesId,
          color,
          mode: mode ?? s.defaultMode,
          size: 1,
          name: petName(name).trim(),
        }],
      }))
    },
    remove(id) {
      setState((s) => ({ ...s, pets: s.pets.filter((p) => p.id !== id) }))
    },
    setMode(id, mode) {
      setState((s) => ({
        ...s,
        pets: s.pets.map((p) => (p.id === id ? { ...p, mode } : p)),
      }))
    },
    swapMode(id) {
      setState((s) => ({
        ...s,
        pets: s.pets.map((p) => (
          p.id === id ? { ...p, mode: p.mode === 'screen' ? 'widget' : 'screen' } : p
        )),
      }))
    },
    setPetSize(id, size) {
      const next = petSize(size)
      setState((s) => ({
        ...s,
        pets: s.pets.map((p) => (p.id === id ? { ...p, size: next } : p)),
      }))
    },
    setPetName(id, name) {
      const next = petName(name)
      setState((s) => ({
        ...s,
        pets: s.pets.map((p) => (p.id === id ? { ...p, name: next } : p)),
      }))
    },
    setDefaultMode(mode) { setState((s) => ({ ...s, defaultMode: mode })) },
    setOpts(patch) { setState((s) => ({ ...s, opts: { ...s.opts, ...patch } })) },
    /**
     * Set one display's taste multiplier K(d).
     *
     * Takes the display OBJECT, not a key, so that the caller never has to know
     * how a display is identified -- the keying rule is a persistence concern
     * and has already changed once (`display.id` was not stable enough). A
     * display with nothing durable to key on is a no-op rather than an error: it
     * already reads as 1.0, so refusing to store is the same answer as storing.
     */
    setDisplayTaste(display, value) {
      const key = displayTasteKey(display)
      if (!key) {
        console.warn('[pets] display has no stable key, per-screen size not saved', display)
        return
      }
      const next = petTaste(value)
      setState((s) => {
        // The default is stored explicitly rather than deleted, because "the
        // user checked this screen and 1.0 was right" and "the user has never
        // touched this screen" are different facts, and only the second should
        // move if the default ever changes.
        if (s.taste?.[key] === next) return s
        return { ...s, taste: { ...s.taste, [key]: next } }
      })
    },
    /** Forget one display's taste value, returning it to the 1.0 default. */
    resetDisplayTaste(display) {
      const key = displayTasteKey(display)
      if (!key) return
      setState((s) => {
        if (!(key in (s.taste || {}))) return s
        const taste = { ...s.taste }
        delete taste[key]
        return { ...s, taste }
      })
    },
    /** Forget every display's taste value. */
    resetAllDisplayTaste() { setState((s) => ({ ...s, taste: {} })) },
    // Only the roster is cleared. Taste is a property of the user's desk, not of
    // any pet, so removing every pet must not discard screen tuning that still
    // applies to whatever is added next.
    clear() { setState((s) => ({ ...s, pets: [] })) },
  }), [])

  /**
   * Read one display's taste multiplier. Always a usable number.
   *
   * Never returns undefined and never throws, because the caller multiplies a
   * sprite height by the result and a falsy one makes the pet vanish.
   */
  const displayTaste = useCallback((display) => {
    const key = displayTasteKey(display)
    if (!key) return PET_TASTE_DEFAULT
    const stored = state.taste?.[key]
    return stored === undefined ? PET_TASTE_DEFAULT : petTaste(stored)
  }, [state.taste])

  /** Whether this display has a value of its own, as opposed to the default. */
  const hasDisplayTaste = useCallback((display) => {
    const key = displayTasteKey(display)
    return Boolean(key) && state.taste?.[key] !== undefined
  }, [state.taste])

  // A pet double-clicked out on the desktop is asking to come home. The overlay
  // cannot change the roster itself, so it says so and this does it.
  useEffect(() => window.nw?.onPetHome?.((id) => actions.setMode(id, 'widget')), [actions])

  const value = useMemo(() => ({
    manifest,
    ready: manifest.length > 0,
    manifestError,
    manifestSettled,
    missingSpecies,
    retryManifest,
    ...state,
    resolve,
    displayTaste,
    hasDisplayTaste,
    ...actions,
  }), [
    manifest, manifestError, manifestSettled, missingSpecies, retryManifest,
    state, resolve, displayTaste, hasDisplayTaste, actions,
  ])

  return <PetsCtx.Provider value={value}>{children}</PetsCtx.Provider>
}

export function usePets() {
  const ctx = useContext(PetsCtx)
  if (!ctx) throw new Error('usePets outside PetsProvider')
  return ctx
}

/**
 * Keep the desktop overlay in step with the `screen` half of the roster.
 *
 * Sent as a whole state rather than as add/remove messages: the overlay is a
 * view, and a view that is handed the complete answer every time cannot drift
 * out of sync with the thing it is viewing. An empty list is meaningful -- it is
 * how the overlay window gets closed.
 */
export function useOverlaySync() {
  const {
    pets, opts, taste, resolve, manifestSettled,
  } = usePets()
  // Serialised before the comparison, not after: the resolved records carry the
  // full sprite manifest for each pet, and a fresh object identity every render
  // would push the same payload across IPC on every tick of the clock.
  const payload = useMemo(() => {
    // Gated on the question being answered, not on the answer being good. A
    // null payload is never sent, so gating on a usable catalogue meant one
    // failed manifest read stopped the overlay from ever being told anything --
    // including that it should close. Waiting is the only reason to say
    // nothing; a settled failure still deserves a real, empty answer, and a
    // later successful retry sends the full one.
    if (!manifestSettled) return null
    const loose = pets
      .filter((p) => p.mode === 'screen')
      .map(resolve)
      .filter(Boolean)
      .map(({ id, species, variant, speciesId, color, size }) => ({
        id, speciesId, color, species, variant, size: petSize(size),
      }))
    // The whole map goes across, not the value for one display: the overlay spans
    // every monitor and resolves a pet's display per frame, so it needs to look
    // K up itself. Keyed by the same durable panel key, so the overlay never has
    // to reason about `display.id` either. `M(d)` is NOT in here -- the overlay
    // derives it from the `sf`/`ppi` fields on its own display list, which keeps
    // the derived factor and the authored one visibly separate on the wire.
    return { pets: loose, opts, taste: petTasteMap(taste) }
  }, [pets, opts, taste, resolve, manifestSettled])

  const last = useRef('')
  useEffect(() => {
    if (!payload) return
    const sig = JSON.stringify(payload)
    if (sig === last.current) return
    last.current = sig
    window.nw?.petsSync?.(payload)
  }, [payload])
}
