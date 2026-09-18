/* Section icons, as inline SVG.
   The Tk build rasterised these with a signed-distance-field routine so the
   curves would antialias without PIL. On a GPU-composited surface the browser
   does that for free, and the glyph scales with the window instead of needing a
   sprite cache keyed on DPI. */

const paths = {
  globe: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M3 12h18M12 3c2.5 2.6 2.5 15.4 0 18M12 3c-2.5 2.6-2.5 15.4 0 18',
  signal: 'M8.5 8.5a5 5 0 0 0 0 7M15.5 8.5a5 5 0 0 1 0 7M5.5 5.5a9 9 0 0 0 0 13M18.5 5.5a9 9 0 0 1 0 13M12 11a1 1 0 1 0 .01 0',
  route: 'M6 20V9a3 3 0 0 1 3-3h6a3 3 0 0 0 3-3M6 20h.01M18 3h.01M6 20a2 2 0 1 1 0-.01M18 3a2 2 0 1 1 0-.01',
  chip: 'M8 8h8v8H8zM4 10V8a4 4 0 0 1 4-4h2M20 10V8a4 4 0 0 0-4-4h-2M4 14v2a4 4 0 0 0 4 4h2M20 14v2a4 4 0 0 1-4 4h-2',
  clock: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 7v5l3.5 2',
  spark: 'M12 3v18M3 12h18M6.5 6.5l11 11M17.5 6.5l-11 11',
  shield: 'M12 3l7 3v5.5c0 4.2-2.9 7.8-7 9.5-4.1-1.7-7-5.3-7-9.5V6z',
  // A double-headed diagonal arrow between two corner brackets: the drag
  // gesture itself, rather than a copy of the grip in the corner of the frame.
  resize: 'M20 4L4 20M14 4h6v6M10 20H4v-6',
  // Four toes and a pad. Drawn as outlines rather than filled, because every
  // other glyph here is a 2px stroke and a solid shape beside them reads as a
  // different weight of icon.
  paw: 'M8.6 7a1.5 2 0 1 0 0 .01M15.4 7a1.5 2 0 1 0 0 .01M4.8 11.6a1.4 1.7 0 1 0 0 .01M19.2 11.6a1.4 1.7 0 1 0 0 .01M12 12.2c-2.6 0-4.8 2.3-4.8 4.7 0 1.6 1.2 2.6 2.7 2.6.9 0 1.4-.3 2.1-.3s1.2.3 2.1.3c1.5 0 2.7-1 2.7-2.6 0-2.4-2.2-4.7-4.8-4.7Z',
}

/**
 * A section icon inside the small round badge the design puts beside a title.
 *
 * `size` is the badge's diameter. The default is the one the card headings use;
 * the tab asks for a larger one, where the badge is the widget's only piece of
 * identity and sits beside 32px dials rather than a line of small caps.
 */
export function SectionIcon({ name, size = 19 }) {
  return (
    <span
      className="grid shrink-0 place-items-center rounded-full border border-edge-soft bg-badge"
      style={{ width: size, height: size }}
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{
          width: Math.round(size * 0.58),
          height: Math.round(size * 0.58),
          color: 'var(--color-ink)',
        }}
      >
        <path d={paths[name] ?? paths.globe} />
      </svg>
    </span>
  )
}
