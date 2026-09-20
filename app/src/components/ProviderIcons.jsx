/* Provider marks for the AI-usage account list, as inline SVG.

   Twelve vendors share one row template, so the mark is the only thing telling
   them apart at a glance. Everything here is drawn in code: the repo is public
   and bundling real logo files would import a trademark problem for no visual
   gain at 16px. Where a vendor's mark is a shape that survives being redrawn as
   a 2px stroke (a sail, a crescent, a caret) it gets a drawn glyph; where it is
   a bespoke illustration it gets an honest lettermark in a tinted rounded
   square, which reads far better than a freehand guess at someone's logo.

   Colours are the vendor's hue where that hue is unambiguous, and a deliberate
   slot on the wheel otherwise, so a list of twelve rows separates by colour
   before you read a word of it. */

/* Each entry carries the display name for the accessible label, the accent, and
   how to draw it. `kind` is 'stroke' (2px outline, matching Icons.jsx), 'fill'
   (a solid silhouette) or 'letter' (the lettermark fallback). */
const providers = {
  claude: {
    label: 'Claude Code',
    accent: '#D97757',
    kind: 'stroke',
    // A radial burst. Geometric rather than a copy of the Anthropic asterisk,
    // but it lands in the same place visually at widget scale.
    d: 'M12 3.2v5.2M12 15.6v5.2M3.2 12h5.2M15.6 12h5.2M5.8 5.8l3.7 3.7M14.5 14.5l3.7 3.7M18.2 5.8l-3.7 3.7M9.5 14.5l-3.7 3.7',
  },
  codex: {
    label: 'OpenAI Codex',
    accent: '#10A37F',
    kind: 'stroke',
    // A shell prompt. Codex is a CLI first, and the knot in OpenAI's mark turns
    // to mush below 20px.
    d: 'M6.5 8l4 4-4 4M13 16.5h4.5',
  },
  cursor: {
    label: 'Cursor',
    accent: '#8E8EA0',
    kind: 'fill',
    // The pointer the product is named after. Monochrome brand, so the shape
    // carries the identity and the accent stays graphite.
    d: 'M7 3.6l11.2 8.4-5 .7 2.7 5.2-2.6 1.3-2.7-5.2-3.6 3.1z',
  },
  copilot: {
    label: 'GitHub Copilot',
    accent: '#58A6FF',
    kind: 'stroke',
    // Goggles and a brow: the co-pilot reading, not the GitHub cat.
    d: 'M8.5 16.5a3 3 0 1 0 0-6 3 3 0 0 0 0 6M15.5 16.5a3 3 0 1 0 0-6 3 3 0 0 0 0 6M11.2 13.5h1.6M4.5 10.2C6 6.8 18 6.8 19.5 10.2',
  },
  windsurf: {
    label: 'Windsurf',
    accent: '#19C6B0',
    kind: 'stroke',
    // Mast, sail and a wave. Literal, and the only triangle in the set.
    d: 'M12 3.5v13M12 6c3.1 1.6 4.9 4.3 5.5 7.8H12M4 19.6c1.6-1.2 3.2-1.2 4.8 0s3.2 1.2 4.8 0 3.2-1.2 4.8 0',
  },
  devin: {
    label: 'Devin',
    accent: '#6366F1',
    // Cognition's mark is an illustrated face; a lettermark is the honest call.
    kind: 'letter',
    text: 'D',
  },
  replit: {
    label: 'Replit',
    accent: '#F26207',
    kind: 'stroke',
    // Three offset blocks, which is the arrangement the brand reads as without
    // reproducing the artwork itself.
    d: 'M5.5 4.5h6v5h-6zM12.5 9.5h6v5h-6zM5.5 14.5h6v5h-6z',
  },
  kimi: {
    label: 'Kimi Code',
    accent: '#EC4899',
    kind: 'fill',
    // A crescent, for Moonshot. Solid, because a hairline crescent disappears.
    d: 'M15.4 3.4a9 9 0 1 0 5 12.5 7.3 7.3 0 0 1-5-12.5Z',
  },
  glm: {
    label: 'GLM Coding Plan',
    accent: '#0EA5E9',
    kind: 'letter',
    text: 'GL',
  },
  cline: {
    label: 'Cline',
    accent: '#84CC16',
    kind: 'letter',
    text: 'CL',
  },
  antigravity: {
    label: 'Google Antigravity',
    accent: '#FBBC05',
    kind: 'stroke',
    // An arrow leaving a horizon: the name, drawn.
    d: 'M4 18.8a9 9 0 0 1 16 0M12 15.5V4.4M12 4.4L8.8 7.9M12 4.4l3.2 3.5',
  },
  railway: {
    label: 'Railway',
    accent: '#B24BF3',
    kind: 'stroke',
    // Rails and sleepers.
    d: 'M8.5 3.8v16.4M15.5 3.8v16.4M4.2 8.6h15.6M4.2 15.4h15.6',
  },
}

/* The fallback. The registry is expected to grow, and an unknown id must still
   produce something with the right footprint rather than a hole in the row. */
const unknown = {
  label: 'Unknown provider',
  accent: '#94A3B8',
  kind: 'stroke',
  d: 'M12 3.4l7.4 4.3v8.6L12 20.6 4.6 16.3V7.7zM12 11.1a.9.9 0 1 0 .01 0',
}

function entry(id) {
  return providers[id] ?? unknown
}

/**
 * The accent a provider is tinted with, as a CSS colour.
 *
 * Exported on its own so a row, a usage bar or a legend swatch can match the
 * icon without pulling in the whole glyph table. Unknown ids get the fallback
 * slate rather than undefined, so callers never have to guard it.
 */
export function providerAccent(id) {
  return entry(id).accent
}

/** The provider's display name, for labels and tooltips. */
export function providerLabel(id) {
  return entry(id).label
}

/**
 * A provider mark.
 *
 * `size` is the box the glyph is drawn into. It is a number rather than a
 * Tailwind size class because the widget scales the whole UI with CSS zoom and
 * the caller decides how big a row is; the SVG itself is a viewBox, so it stays
 * crisp wherever that lands.
 *
 * `muted` is for providers registered but not yet wired up. It dims and
 * desaturates the same glyph instead of swapping in a placeholder, so an
 * unsupported vendor is still recognisably itself - nothing silently degrades
 * into an anonymous grey square.
 */
export function ProviderIcon({ id, size = 16, muted = false, className = '' }) {
  const mark = entry(id)
  const accent = mark.accent

  return (
    <svg
      viewBox="0 0 24 24"
      role="img"
      aria-label={mark.label}
      className={`shrink-0 ${className}`}
      style={{
        width: size,
        height: size,
        color: accent,
        opacity: muted ? 0.45 : 1,
        filter: muted ? 'saturate(0.6)' : 'none',
      }}
    >
      <title>{mark.label}</title>
      {mark.kind === 'letter' ? (
        <g>
          {/* A tinted tile so the letters sit on something, and read as a mark
              rather than as stray text in the middle of a row. */}
          <rect
            x="2.5"
            y="2.5"
            width="19"
            height="19"
            rx="5"
            fill="currentColor"
            fillOpacity="0.16"
            stroke="currentColor"
            strokeOpacity="0.55"
            strokeWidth="1.5"
          />
          <text
            x="12"
            y="12.2"
            textAnchor="middle"
            dominantBaseline="central"
            fill="currentColor"
            fontFamily="inherit"
            fontWeight="700"
            fontSize={mark.text.length > 1 ? 9 : 12}
            letterSpacing={mark.text.length > 1 ? 0.2 : 0}
          >
            {mark.text}
          </text>
        </g>
      ) : mark.kind === 'fill' ? (
        <path d={mark.d} fill="currentColor" />
      ) : (
        <path
          d={mark.d}
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      )}
    </svg>
  )
}

export default ProviderIcon
