import { pctTone, useSeededValue } from './Bar.jsx'

/* Geometry of the ring, in the SVG's own box, which is also its pixel size.
   A 3.5-unit stroke on a 12.25 radius leaves a 21px hole, enough for three
   digits at 11px -- which is the reason the label lives in the tooltip rather
   than under the ring, and the reason the dial is not smaller than this. */
const SIZE = 32
const MID = SIZE / 2
const R = 12.25
const STROKE = 3.5
const CIRCUMFERENCE = 2 * Math.PI * R

/**
 * Below the warning thresholds, the hue says which family a ring belongs to:
 * the machine's own load or an AI allowance. Above them it says the thing that
 * matters more, which is that the number is high -- a red ring is a red ring
 * whatever it is measuring.
 */
function ringColour(pct, hue) {
  if (pct >= 90) return 'var(--color-bad)'
  if (pct >= 70) return 'var(--color-warn)'
  return hue === 'ai' ? 'var(--color-accent-ai)' : 'var(--color-accent)'
}

/**
 * A percentage as a small dial: the arc and the number, nothing else.
 *
 * Drawn for the tab, where there is room for the value but not for its name.
 * `note` explains an absent reading in the tooltip rather than in two
 * characters inside the ring, which is all the space there is.
 */
export function Ring({ value, label, hue = 'sys', note, stale }) {
  const has = value != null && Number.isFinite(Number(value))
  const [pct, el] = useSeededValue(value)

  // A stale figure is the last real reading, kept through a failed poll rather
  // than replaced by a dash -- the number is still true, it is just no longer
  // fresh, so it is dimmed and the tooltip says why.
  return (
    <span
      className="relative grid shrink-0 place-items-center"
      style={{ width: SIZE, height: SIZE }}
      title={has
        ? `${label} · ${Math.round(pct)}%${stale ? ` · last known (${note})` : ''}`
        : `${label} · ${note ?? 'no reading yet'}`}
    >
      <svg
        viewBox={`0 0 ${SIZE} ${SIZE}`}
        className="absolute inset-0 h-full w-full -rotate-90"
      >
        <circle
          cx={MID}
          cy={MID}
          r={R}
          fill="none"
          stroke="rgba(255,255,255,0.14)"
          strokeWidth={STROKE}
        />
        <circle
          ref={el}
          cx={MID}
          cy={MID}
          r={R}
          fill="none"
          stroke={ringColour(pct, hue)}
          strokeWidth={STROKE}
          strokeLinecap="round"
          strokeDasharray={CIRCUMFERENCE}
          strokeDashoffset={has ? CIRCUMFERENCE * (1 - pct / 100) : CIRCUMFERENCE}
          className="transition-[stroke-dashoffset] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)]"
        />
      </svg>
      <span
        className={'glass-text relative text-[11px] font-semibold leading-none tabular-nums '
          + (stale ? 'opacity-60 ' : '')
          + pctTone(has ? pct : null)}
      >
        {has ? Math.round(pct) : '—'}
      </span>
    </span>
  )
}
