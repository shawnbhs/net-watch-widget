import { usePane } from '../panes.jsx'
import { SectionIcon } from './Icons.jsx'

/**
 * One frosted card.
 *
 * The frost itself is not here -- it is a DWM-acrylic window sitting behind
 * this element, positioned from the rect this div reports. What this draws is
 * only what glass looks like at its edges: a hairline border, and the inset
 * highlight along the top that reads as a lit rim.
 *
 * The two background states are in index.css, under `.nw-card`, because the
 * resting one and the in-motion one only make sense read together.
 */
export function Card({ id, className = '', children, ...rest }) {
  const ref = usePane(id)
  return (
    <div
      ref={ref}
      data-card={id}
      className={
        'nw-card relative rounded-[var(--radius-card)] border border-edge ' +
        'shadow-[inset_0_1px_0_var(--color-sheen)] ' +
        className
      }
      {...rest}
    >
      {children}
    </div>
  )
}

/** Two cards side by side. Each gets its own pane, so the gap stays empty. */
export function Pair({ children }) {
  return <div className="grid grid-cols-2 gap-2">{children}</div>
}

/** A card's heading: icon badge, small-caps label, optional right-hand slot. */
export function CardHead({ icon, children, right, tight }) {
  return (
    <div className={'flex items-center gap-2 px-3 ' + (tight ? 'pt-2 pb-1' : 'pt-2.5 pb-1.5')}>
      {icon && <SectionIcon name={icon} />}
      <span className="glass-text flex-1 text-[9.5px] font-semibold uppercase tracking-[0.13em] text-muted">
        {children}
      </span>
      {right}
    </div>
  )
}

/** The big copyable figure a card is built around -- an IP, usually. */
export function Figure({ value, onCopy, title }) {
  return (
    <div
      className={
        'glass-text truncate px-3 text-[15px] font-semibold tabular-nums ' +
        (onCopy ? 'cursor-pointer rounded-md hover:text-accent-2 ' : '')
      }
      onClick={onCopy}
      title={title ?? (onCopy ? 'Click to copy' : undefined)}
    >
      {value}
    </div>
  )
}

/**
 * A label/value row. `tone` colours the value; `onCopy` makes it copyable.
 *
 * `wrap` lets the value run to a second line instead of being cut. A quick-check
 * result is often longer than its column -- a full ASN description, a DNS
 * resolver list -- and silently clipping the tail of a security readout is worse
 * than spending another line on it.
 */
export function Row({ label, value, tone, mono, onCopy, title, wrap, w = 'w-[72px]' }) {
  return (
    <div
      className={
        'flex items-baseline gap-2 px-3 py-[2px] ' +
        (onCopy ? 'cursor-pointer rounded-md hover:bg-white/10 ' : '')
      }
      onClick={onCopy}
      title={title ?? (onCopy ? 'Click to copy' : undefined)}
    >
      <span className={'glass-text shrink-0 text-[10.5px] text-muted ' + w}>{label}</span>
      <span
        className={
          'glass-text min-w-0 flex-1 text-[11px] font-medium ' +
          (wrap ? 'break-words ' : 'truncate ') +
          (mono ? 'tabular-nums ' : '') +
          (tone ?? 'text-ink')
        }
      >
        {value}
      </span>
    </div>
  )
}

/** A small pill button that opens an external lookup. */
export function Pill({ children, onClick, title }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className="rounded-[7px] border border-edge-soft bg-white/[0.08] px-2.5 py-[4px]
                 text-[10px] font-medium text-ink-2 transition duration-150
                 hover:border-white/35 hover:bg-white/20 hover:text-ink
                 active:scale-[0.96]"
    >
      {children}
    </button>
  )
}

/** The one filled control on the panel. Everything else is a wash. */
export function PrimaryButton({ children, onClick, title }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className="rounded-[7px] border border-white/35 bg-gradient-to-b from-[#3d95ff] to-[#1f77f0]
                 px-3 py-[4px] text-[10px] font-semibold text-white
                 shadow-[inset_0_1px_0_rgba(255,255,255,.35)] transition duration-150
                 hover:brightness-110 active:scale-[0.96]"
    >
      {children}
    </button>
  )
}

/**
 * A quick-check cell: small-caps label above the value.
 *
 * Stacked rather than label-left because these sit in half-width columns. With
 * the label beside it a value like a full ASN description has about ninety
 * pixels to live in and wraps to five lines; above it, it has the whole column
 * and wraps to two. Nothing is ever truncated here -- clipping the tail of a
 * security readout hides exactly the part worth reading.
 */
export function Cell({ label, value, tone }) {
  return (
    <div className="px-3 py-[3px]">
      <div className="glass-text text-[8.5px] font-semibold uppercase tracking-[0.1em] text-faint">
        {label}
      </div>
      <div className={'glass-text break-words text-[10.5px] font-medium leading-snug '
        + (tone ?? 'text-ink')}>
        {value}
      </div>
    </div>
  )
}
