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

const DEFAULT_OPTS = {
  /**
   * Drawn height in CSS px, before the widget's own scale.
   *
   * 26 rather than PetRail's 56, and the ceiling is 40, because a widget pet
   * stands on a card's top edge and its body reaches up over the card above it.
   * The thinnest cards here -- the title and the footer -- are 43px, so a pet
   * taller than that would stand clear of the topmost card and be clipped by
   * the window, which is sized to exactly its content.
   */
  size: 26,
  speed: 1,
  liveliness: 50,
  follow: true,
  dance: true,
  shadows: true,
  calm: false,
}

const EMPTY = { pets: [], defaultMode: 'widget', opts: DEFAULT_OPTS }

function load() {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return EMPTY
    const saved = JSON.parse(raw)
    return {
      pets: Array.isArray(saved.pets) ? saved.pets.filter((p) => p?.speciesId) : [],
      defaultMode: saved.defaultMode === 'screen' ? 'screen' : 'widget',
      opts: { ...DEFAULT_OPTS, ...(saved.opts || {}) },
    }
  } catch {
    return EMPTY
  }
}

const PetsCtx = createContext(null)

let seq = 0
const uid = () => `pet-${Date.now().toString(36)}-${++seq}`

export function PetsProvider({ children }) {
  const [manifest, setManifest] = useState([])
  const [state, setState] = useState(load)

  // The species list is four hundred filenames' worth of structure that cannot
  // change while the app runs, so it is asked for once and kept.
  useEffect(() => {
    let alive = true
    window.nw?.petManifest?.().then((list) => {
      if (alive && Array.isArray(list)) setManifest(list)
    }).catch(() => { /* no sprites is a widget without pets, not a crash */ })
    return () => { alive = false }
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

  useEffect(() => {
    try { localStorage.setItem(KEY, JSON.stringify(state)) } catch { /* private mode */ }
  }, [state])

  const actions = useMemo(() => ({
    add(speciesId, color, mode) {
      setState((s) => ({
        ...s,
        pets: [...s.pets, {
          id: uid(),
          speciesId,
          color,
          mode: mode ?? s.defaultMode,
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
    setDefaultMode(mode) { setState((s) => ({ ...s, defaultMode: mode })) },
    setOpts(patch) { setState((s) => ({ ...s, opts: { ...s.opts, ...patch } })) },
    clear() { setState((s) => ({ ...s, pets: [] })) },
  }), [])

  // A pet double-clicked out on the desktop is asking to come home. The overlay
  // cannot change the roster itself, so it says so and this does it.
  useEffect(() => window.nw?.onPetHome?.((id) => actions.setMode(id, 'widget')), [actions])

  const value = useMemo(() => ({
    manifest,
    ready: manifest.length > 0,
    ...state,
    resolve,
    ...actions,
  }), [manifest, state, resolve, actions])

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
  const { pets, opts, resolve, ready } = usePets()
  // Serialised before the comparison, not after: the resolved records carry the
  // full sprite manifest for each pet, and a fresh object identity every render
  // would push the same payload across IPC on every tick of the clock.
  const payload = useMemo(() => {
    if (!ready) return null
    const loose = pets
      .filter((p) => p.mode === 'screen')
      .map(resolve)
      .filter(Boolean)
      .map(({ id, species, variant, speciesId, color }) => ({
        id, speciesId, color, species, variant,
      }))
    return { pets: loose, opts }
  }, [pets, opts, resolve, ready])

  const last = useRef('')
  useEffect(() => {
    if (!payload) return
    const sig = JSON.stringify(payload)
    if (sig === last.current) return
    last.current = sig
    window.nw?.petsSync?.(payload)
  }, [payload])
}
