import { useState } from 'react'

/* The Tk build drew these with a signed-distance-field rasteriser so the curves
   would antialias without PIL -- about 300 lines for 17 glyphs. Inline SVG on a
   GPU-composited surface is the same picture for free, and it scales with the
   window instead of needing a sprite cache keyed on DPI. */

const paths = {
  // An arrow going up into the edge it docks against.
  mini: 'M4 5h16M12 20V10M8 14l4-4 4 4',
  compact: 'M4 7h16M4 12h16M4 17h10',
  full: 'M4 6h16v5H4zM4 13h16v5H4z',
  lock: 'M7 11V8a5 5 0 0 1 10 0v3M5 11h14v9H5z',
  unlock: 'M7 11V8a5 5 0 0 1 9.5-2M5 11h14v9H5z',
  power: 'M12 3v9M7.5 6.5a7 7 0 1 0 9 0',
  refresh: 'M20 12a8 8 0 1 1-2.3-5.6M20 4v4h-4',
  close: 'M6 6l12 12M18 6L6 18',
}

/** Round glass button. `tone` overrides the icon colour (the power dot). */
export function IconButton({ icon, label, onClick, tone, spinning, active }) {
  const [hover, setHover] = useState(false)
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      className={
        'grid h-[26px] w-[26px] place-items-center rounded-full border transition ' +
        'duration-200 ease-out active:scale-[0.92] ' +
        (hover || active
          ? 'border-white/35 bg-white/22 '
          : 'border-edge-soft bg-white/[0.08] ')
      }
    >
      <svg
        viewBox="0 0 24 24"
        className={'h-[13px] w-[13px] ' + (spinning ? 'animate-[nw-spin_900ms_linear_infinite]' : '')}
        fill="none"
        stroke={tone ?? 'currentColor'}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{ color: 'var(--color-ink)' }}
      >
        <path d={paths[icon]} />
      </svg>
    </button>
  )
}

/** The status dot on the network button: green up, red cut, amber mid-toggle. */
export function NetDot({ up, busy }) {
  const colour = busy ? 'var(--color-warn)' : up ? 'var(--color-good)' : 'var(--color-bad)'
  return (
    <span
      className="inline-block h-[6px] w-[6px] rounded-full"
      style={{ background: colour, boxShadow: `0 0 6px ${colour}` }}
    />
  )
}

export { paths }
