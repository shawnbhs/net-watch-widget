// How long until an AI usage window refills, for the tooltips.
//
// Every view (tab rings, compact meters, full-size rows) shows the same
// sentence on hover -- "Claude · week · 32% · resets in 2h 30m" -- so the
// wording lives here once instead of drifting apart in three components.

/**
 * A reset marker, however the backend phrased it, as time remaining.
 *
 * The sidecar already converts these, so in practice a ready-made `2h 27m`
 * passes straight through. Epoch seconds and ISO instants are converted anyway,
 * because a provider adapter can be added without touching the sidecar.
 * Anything else is left alone; some adapters answer with a phrase.
 */
export function resetLabel(v, now = Date.now()) {
  if (v == null || v === '') return ''
  const s = String(v).trim().replace(/^in\s+/, '')
  let ms = null
  if (typeof v === 'number' || /^\d{9,}$/.test(s)) ms = Number(v) * 1000
  else if (/^\d{4}-\d{2}-\d{2}T/.test(s) && !Number.isNaN(Date.parse(s))) ms = Date.parse(s)
  if (ms == null || Number.isNaN(ms)) return s

  const left = ms - now
  if (left <= 0) return 'now'
  const mins = Math.floor(left / 60000)
  const d = Math.floor(mins / 1440)
  const h = Math.floor((mins % 1440) / 60)
  const m = mins % 60
  if (d) return `${d}d ${h}h`
  if (h) return `${h}h ${m}m`
  return `${m}m`
}

/** The reset field that belongs to a `*_pct` field, if the object has one. */
export function resetOf(obj, pctKey) {
  if (!obj || !pctKey || !pctKey.endsWith('_pct')) return ''
  const stem = pctKey.slice(0, -4)
  return resetLabel(obj[`${stem}_reset`] ?? obj[`${stem}_reset_ts`])
}

/** "resets in 2h 30m", "resetting now", or '' when unknown. */
export function resetPhrase(reset) {
  if (!reset) return ''
  return reset === 'now' ? 'resetting now' : `resets in ${reset}`
}

/** The whole hover line for one usage window. */
export function usageTip(label, pct, reset) {
  const parts = [label]
  if (pct != null && Number.isFinite(Number(pct))) parts.push(`${Math.round(Number(pct))}%`)
  const r = resetPhrase(reset)
  if (r) parts.push(r)
  return parts.join(' · ')
}
