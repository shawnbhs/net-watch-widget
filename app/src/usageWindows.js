// The usage windows a reading actually reports, and which of them a pick shows.
//
// Nothing here knows in advance which windows a provider has. A window exists
// when the reading carries a numeric `<stem>_pct`; when a provider drops one
// (Claude retiring its 5-hour limit, say) it simply stops being listed, and if
// it comes back it is listed again. Known stems get a short name and a fixed
// order; anything else is shown under its own stem, after them.
import { resetOf } from './resetText.js'

const ORDER = ['session_pct', 'sess_pct', 'week_pct', 'model_pct', 'month_pct']
const LABEL = { session_pct: '5h', sess_pct: '5h', week_pct: 'week', model_pct: 'model', month_pct: 'month' }

const rank = (k) => { const i = ORDER.indexOf(k); return i < 0 ? ORDER.length : i }

/** [{ key, label, value, reset }] for every window `u` reports, in display order. */
export function usageWindows(u) {
  if (!u || typeof u !== 'object') return []
  return Object.keys(u)
    .filter((k) => k.endsWith('_pct') && k.length > 4 && typeof u[k] === 'number' && Number.isFinite(u[k]))
    .sort((a, b) => rank(a) - rank(b))
    .map((key) => ({
      key,
      label: key === 'model_pct' && u.model_name
        ? String(u.model_name)
        : (LABEL[key] ?? key.slice(0, -4).replace(/_/g, ' ')),
      value: u[key],
      reset: resetOf(u, key),
    }))
}

/**
 * Which of `wins` one tab pick draws.
 *
 * `keys` is the user's own list. Keys that are not reported right now stay in
 * it (so the window returns by itself when the provider sends it again) but
 * draw nothing. A pick saved before windows were choosable has no `keys`: it
 * means the first window, plus the weekly one when its `week` tick was on.
 * A pick never draws nothing while its account reports something -- with no
 * chosen window present it falls back to the first one there is.
 */
export function shownWindows(pick, wins) {
  if (!Array.isArray(wins) || !wins.length) return []
  let out
  if (Array.isArray(pick?.keys)) out = wins.filter((w) => pick.keys.includes(w.key))
  else {
    out = [wins[0]]
    const week = pick?.week && wins.find((w) => w.key === 'week_pct')
    if (week && week !== wins[0]) out.push(week)
  }
  return out.length ? out : [wins[0]]
}

/** Turn window `key` on or off in a pick, keeping keys that are absent today. */
export function toggleWindow(pick, wins, key) {
  const cur = Array.isArray(pick.keys) ? pick.keys : shownWindows(pick, wins).map((w) => w.key)
  const keys = cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key]
  return { ...pick, keys }
}
