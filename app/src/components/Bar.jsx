import { useEffect, useRef, useState } from 'react'

/**
 * A percentage to draw, and a ref for the element that animates towards it.
 *
 * The one piece of behaviour worth stating: the first value a meter ever
 * receives is written without a transition, and every later one animates. A
 * meter that slides up from zero on its first paint is telling the user "this
 * just changed" when in fact the widget has only now learned what the value
 * always was. The Tk build had this exact bug, and it was DPI-dependent because
 * "first paint" and "first value" were conflated; here they are two separate
 * facts.
 *
 * Shared by the bar and the ring, which differ only in which CSS property
 * carries the value.
 */
export function useSeededValue(value) {
  const [pct, setPct] = useState(0)
  const seeded = useRef(false)
  const el = useRef(null)

  useEffect(() => {
    const next = Math.max(0, Math.min(100, Number(value) || 0))
    if (!seeded.current && value != null) {
      seeded.current = true
      const node = el.current
      if (node) {
        // Suppress the transition for exactly one commit, then restore it.
        node.style.transition = 'none'
        setPct(next)
        requestAnimationFrame(() => {
          if (node) node.style.transition = ''
        })
        return
      }
    }
    setPct(next)
  }, [value])

  return [pct, el]
}

/** A usage bar. */
export function Bar({ value, tone = 'linear-gradient(90deg,#8fc0ff,#d6e9ff)' }) {
  const [width, el] = useSeededValue(value)

  return (
    <div className="h-[6px] w-full overflow-hidden rounded-full bg-white/12">
      <div
        ref={el}
        className="h-full rounded-full transition-[width] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)]"
        style={{ width: `${width}%`, background: tone }}
      />
    </div>
  )
}

// Thresholds live here rather than in the sidecar on purpose: "85% is amber"
// is a presentation decision, and the sidecar sends numbers so the UI can make
// it without a round trip.
export function pctTone(pct) {
  if (pct == null) return 'text-faint'
  if (pct >= 90) return 'text-bad'
  if (pct >= 70) return 'text-warn'
  return 'text-ink'
}

function pctFill(pct) {
  if (pct == null) return 'rgba(255,255,255,.2)'
  if (pct >= 90) return 'linear-gradient(90deg,#ff7b7b,#ffb0b0)'
  if (pct >= 70) return 'linear-gradient(90deg,#ffc46b,#ffe0ad)'
  return 'linear-gradient(90deg,#8fc0ff,#d6e9ff)'
}
