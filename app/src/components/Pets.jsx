import { useState } from 'react'
import { Card, CardHead } from './Glass.jsx'
import {
  PET_NAME_MAX, PET_SIZE_MAX, PET_SIZE_MIN, PET_SIZE_STEP, usePets,
} from '../pets/store.jsx'
import { ASSET_BASE, OVERLAY_SIZE_FACTOR } from '../pets/engine.js'

/**
 * The pets row.
 *
 * Three things in one card, which is more than any other card here carries, and
 * the reason is that they are the same thing looked at from three distances:
 * what species to add, which pets exist, and where each one of them lives. The
 * picker and the tuning panel are both collapsed by default so the resting card
 * is just the roster -- which in the ordinary case is two or three lines.
 *
 * "Where it lives" is per pet rather than global. One dog on the widget and one
 * fox loose on the desktop is the interesting configuration, and a single switch
 * could not express it.
 */

const MODES = [
  ['widget', 'Widget', 'Walks the cards and hops between them'],
  ['screen', 'Screen', 'Roams the whole desktop'],
]

/** The two-position mode switch, at two sizes: the default, and a roster row. */
function ModeSwitch({ value, onChange, small }) {
  return (
    <span
      className={'inline-flex shrink-0 overflow-hidden rounded-[6px] border border-edge-soft '
        + 'bg-white/[0.06]'}
      role="group"
    >
      {MODES.map(([key, label, hint]) => (
        <button
          key={key}
          type="button"
          title={hint}
          aria-pressed={value === key}
          onClick={() => onChange(key)}
          className={'transition duration-150 '
            + (small ? 'px-1.5 py-[2px] text-[9px] ' : 'px-2 py-[3px] text-[9.5px] ')
            + (value === key
              ? 'bg-gradient-to-b from-[#3d95ff] to-[#1f77f0] font-semibold text-white '
              : 'text-muted hover:bg-white/12 hover:text-ink-2 ')}
        >
          {label}
        </button>
      ))}
    </span>
  )
}

/**
 * The height a pet will actually be drawn at, in CSS px.
 *
 * Three things multiply together and only one of them is on screen, so the
 * slider quotes the answer: the global size, this pet's own factor, and the
 * species' scale -- a cat and a crab drawn at the same nominal height do not
 * occupy the same space, and the manifest corrects for that. A pet out on the
 * desktop is larger again by OVERLAY_SIZE_FACTOR.
 *
 * The widget's own zoom is deliberately not in here. It scales the whole
 * interface, the global slider's own "26 px" ignores it too, and a figure that
 * moved every time the window was resized would be unreadable.
 */
function petPx(factor, size, full, mode) {
  const base = mode === 'screen' ? Math.round(size * OVERLAY_SIZE_FACTOR) : size
  return Math.round(base * factor * (full?.species.scale ?? 1))
}

/** A labelled slider. `format` turns the raw value into the read-out. */
function Slider({ label, value, min, max, step, onChange, format }) {
  return (
    <div className="px-3 py-[3px]">
      <div className="flex items-baseline justify-between">
        <span className="glass-text text-[9.5px] uppercase tracking-wide text-muted">
          {label}
        </span>
        <span className="glass-text text-[10px] tabular-nums text-ink-2">
          {format(value)}
        </span>
      </div>
      <input
        type="range"
        className="nw-range mt-1 w-full"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  )
}

/** A small checkbox row, in the card's own type rather than the browser's. */
function Check({ label, checked, onChange, hint }) {
  return (
    <label
      className="flex cursor-pointer items-center gap-2 px-3 py-[2px]"
      title={hint}
    >
      <input
        type="checkbox"
        className="nw-check"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="glass-text text-[10px] text-ink-2">{label}</span>
    </label>
  )
}

export function PetsCard({ rise }) {
  const {
    manifest, ready, pets, defaultMode, opts,
    add, remove, setMode, setPetSize, setPetName, setDefaultMode, setOpts, clear, resolve,
  } = usePets()

  const [picking, setPicking] = useState(false)
  const [tuning, setTuning] = useState(false)
  const [chosen, setChosen] = useState(null)     // species id, while picking
  const [colour, setColour] = useState(null)
  const [sizing, setSizing] = useState(null)     // pet id, while resizing one
  // Typed before the species is picked, because one of the two add paths does
  // not pause for a second click: a species with a single variant is added the
  // moment its tile is clicked. A name box that only appeared next to the Add
  // button would be unreachable for exactly those pets.
  const [naming, setNaming] = useState('')

  const species = manifest.find((s) => s.id === chosen) || null

  const choose = (sp) => {
    // Picking the species already open again closes it, so a mis-click is one
    // click to undo rather than a hunt for a close button.
    if (chosen === sp.id) { setChosen(null); setColour(null); return }
    setChosen(sp.id)
    setColour(sp.variants[0]?.color ?? null)
    // One variant is not a choice, so adding it is one click, not two.
    if (sp.variants.length === 1) {
      add(sp.id, sp.variants[0].color, undefined, naming)
      setChosen(null)
      setNaming('')
      setPicking(false)
    }
  }

  const place = () => {
    if (!species || !colour) return
    add(species.id, colour, undefined, naming)
    setChosen(null)
    setColour(null)
    setNaming('')
    setPicking(false)
  }

  return (
    <Card id="pets" {...rise('175ms', 'pb-2')}>
      <CardHead
        icon="paw"
        right={(
          <span className="flex items-center gap-2">
            {pets.length > 0 && (
              <button
                type="button"
                onClick={clear}
                className="text-[9.5px] text-faint transition hover:text-accent"
                title="Remove every pet"
              >
                Clear
              </button>
            )}
            <button
              type="button"
              onClick={() => setTuning((t) => !t)}
              aria-pressed={tuning}
              title="How the pets behave"
              className={'text-[13px] leading-none transition '
                + (tuning ? 'text-accent' : 'text-muted hover:text-accent')}
            >
              &#8943;
            </button>
          </span>
        )}
      >
        Pets
      </CardHead>

      {/* ── where new pets go ─────────────────────────────────────────────── */}
      <div className="flex items-center gap-2 px-3 py-[2px]">
        <span className="glass-text flex-1 text-[10px] text-muted">New pets live in the</span>
        <ModeSwitch value={defaultMode} onChange={setDefaultMode} />
      </div>

      {/* ── the picker ────────────────────────────────────────────────────── */}
      <div className="px-3 pt-1.5">
        <button
          type="button"
          onClick={() => { setPicking((p) => !p); setChosen(null) }}
          disabled={!ready}
          className={'w-full rounded-[7px] border border-edge-soft bg-white/[0.08] py-[5px] '
            + 'text-[10px] font-medium text-ink-2 transition duration-150 '
            + (ready
              ? 'hover:border-white/35 hover:bg-white/20 hover:text-ink active:scale-[0.99]'
              : 'cursor-default opacity-50')}
        >
          {!ready ? 'no sprites found' : picking ? 'Close' : '+  Add a pet'}
        </button>
      </div>

      {picking && ready && (
        <>
          <div className="mt-1.5 px-3">
            <input
              type="text"
              value={naming}
              maxLength={PET_NAME_MAX}
              spellCheck={false}
              onChange={(e) => setNaming(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && species && colour) place() }}
              placeholder="Name this pet (optional)"
              aria-label="Name for the pet you are about to add"
              className="glass-text w-full rounded-[6px] border border-edge-soft bg-white/[0.06]
                         px-2 py-[3px] text-[10px] text-ink outline-none transition
                         placeholder:text-faint focus:border-white/35"
            />
          </div>
          <div
            className="mt-1.5 grid max-h-[104px] grid-cols-7 gap-1 overflow-y-auto px-3"
            role="listbox"
            aria-label="Pet species"
          >
            {manifest.map((sp) => (
              <button
                key={sp.id}
                type="button"
                role="option"
                aria-selected={chosen === sp.id}
                title={sp.label}
                onClick={() => choose(sp)}
                className={'grid aspect-square place-items-center rounded-[6px] border '
                  + 'transition duration-150 active:scale-[0.94] '
                  + (chosen === sp.id
                    ? 'border-white/45 bg-white/25 '
                    : 'border-edge-soft bg-white/[0.06] hover:border-white/30 hover:bg-white/15 ')}
              >
                <img
                  src={ASSET_BASE + (sp.variants[0]?.icon ?? '')}
                  alt=""
                  className="h-[20px] w-[20px] object-contain"
                  style={{ imageRendering: 'pixelated' }}
                />
              </button>
            ))}
          </div>

          {species && species.variants.length > 1 && (
            <div className="mt-1.5 flex flex-wrap items-center gap-1 px-3">
              {species.variants.map((v) => (
                <button
                  key={v.color}
                  type="button"
                  title={`${species.label} · ${v.label}`}
                  onClick={() => setColour(v.color)}
                  className={'rounded-[6px] border px-1.5 py-[2px] text-[9px] transition '
                    + (colour === v.color
                      ? 'border-white/45 bg-white/25 text-ink '
                      : 'border-edge-soft bg-white/[0.06] text-muted hover:bg-white/15 ')}
                >
                  {v.label}
                </button>
              ))}
              <button
                type="button"
                onClick={place}
                className="ml-auto rounded-[7px] border border-white/35 bg-gradient-to-b
                           from-[#3d95ff] to-[#1f77f0] px-2.5 py-[3px] text-[9.5px]
                           font-semibold text-white transition hover:brightness-110
                           active:scale-[0.96]"
              >
                Add {species.label}
              </button>
            </div>
          )}
        </>
      )}

      {/* ── the roster ────────────────────────────────────────────────────── */}
      {pets.length === 0 ? (
        <div className="glass-text mt-1.5 px-3 text-[10px] text-faint">
          No pets yet. One in the widget walks the cards; one on the screen roams
          the desktop.
        </div>
      ) : (
        <ul className="mt-1.5">
          {pets.map((row) => {
            const full = resolve(row)
            return (
              <li key={row.id}>
                <div className="flex items-center gap-2 px-3 py-[2px]">
                  {/* Nothing rather than an empty `src`, which a file:// page
                      resolves back to the document itself. */}
                  {full ? (
                    <img
                      src={ASSET_BASE + full.variant.icon}
                      alt=""
                      className="h-[16px] w-[16px] shrink-0 object-contain"
                      style={{ imageRendering: 'pixelated' }}
                    />
                  ) : <span className="h-[16px] w-[16px] shrink-0" />}
                  {/* The name is the disclosure. A row carries three controls
                      already, and a fourth button for something adjusted once
                      and then left alone would cost every row the width. */}
                  <button
                    type="button"
                    onClick={() => setSizing((id) => (id === row.id ? null : row.id))}
                    title={full
                      ? `${full.species.label} · ${full.variant.label}`
                        + `${row.name ? ` — named ${row.name}` : ''}`
                        + ' · click to rename or resize'
                      : 'Click to rename or resize'}
                    aria-expanded={sizing === row.id}
                    className="glass-text min-w-0 flex-1 truncate text-left text-[10.5px]
                               text-ink-2 transition hover:text-ink"
                  >
                    {row.name || (full ? full.species.label : row.speciesId)}
                    {full && full.species.variants.length > 1 && (
                      <span className="text-faint"> · {full.variant.label}</span>
                    )}
                    {row.size !== 1 && (
                      <span className="text-faint tabular-nums"> · {row.size}x</span>
                    )}
                  </button>
                  <ModeSwitch
                    small
                    value={row.mode}
                    onChange={(m) => setMode(row.id, m)}
                  />
                  <button
                    type="button"
                    onClick={() => remove(row.id)}
                    title="Remove this pet"
                    aria-label={`Remove ${full?.species.label ?? 'pet'}`}
                    className="shrink-0 px-[2px] text-[12px] leading-none text-faint
                               transition hover:text-bad"
                  >
                    &#10005;
                  </button>
                </div>
                {sizing === row.id && (
                  <div className="pl-[18px]">
                    {/* Rename lives here rather than behind a click on the name
                        itself: that click already opens this panel, and one
                        control cannot mean two things. */}
                    <div className="px-3 pt-[3px]">
                      <input
                        type="text"
                        value={row.name}
                        maxLength={PET_NAME_MAX}
                        spellCheck={false}
                        onChange={(e) => setPetName(row.id, e.target.value)}
                        onBlur={(e) => setPetName(row.id, e.target.value.trim())}
                        placeholder={full ? full.species.label : 'Name'}
                        aria-label={`Name for this ${full?.species.label ?? 'pet'}`}
                        className="glass-text w-full rounded-[6px] border border-edge-soft
                                   bg-white/[0.06] px-2 py-[2px] text-[10px] text-ink
                                   outline-none transition placeholder:text-faint
                                   focus:border-white/35"
                      />
                    </div>
                    <Slider
                      label="Size"
                      value={row.size}
                      min={PET_SIZE_MIN}
                      max={PET_SIZE_MAX}
                      step={PET_SIZE_STEP}
                      onChange={(v) => setPetSize(row.id, v)}
                      // Both numbers, because neither answers the question on
                      // its own: the multiplier is what is being set, and the
                      // pixels are what will actually be on the card.
                      format={(v) => `${v.toFixed(1)}x · ${petPx(v, opts.size, full, row.mode)} px`}
                    />
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}

      {/* ── tuning ────────────────────────────────────────────────────────── */}
      {tuning && (
        <div className="mt-1.5 border-t border-edge-soft pt-1.5">
          <Slider
            label="Size · all pets"
            value={opts.size}
            min={16}
            max={100}
            step={1}
            onChange={(size) => setOpts({ size })}
            format={(v) => `${v} px`}
          />
          <Slider
            label="Speed"
            value={opts.speed}
            min={0.2}
            max={2.5}
            step={0.1}
            onChange={(speed) => setOpts({ speed })}
            format={(v) => `${v.toFixed(1)}x`}
          />
          <Slider
            label="Liveliness"
            value={opts.liveliness}
            min={0}
            max={100}
            step={5}
            onChange={(liveliness) => setOpts({ liveliness })}
            format={(v) => (v < 30 ? 'lazy' : v > 70 ? 'hyper' : 'balanced')}
          />
          <Check
            label="React to the cursor"
            checked={opts.follow}
            onChange={(follow) => setOpts({ follow })}
            hint="Pets notice the pointer and sometimes chase it"
          />
          <Check
            label="Dance breaks"
            checked={opts.dance}
            onChange={(dance) => setOpts({ dance })}
            hint="A pet stops where it is and has a shake"
          />
          <Check
            label="Contact shadows"
            checked={opts.shadows}
            onChange={(shadows) => setOpts({ shadows })}
          />
          <Check
            label="Reduced motion"
            checked={opts.calm}
            onChange={(calm) => setOpts({ calm })}
            hint="Stop the wandering and keep the pets still"
          />
          <p className="glass-text mt-1 px-3 text-[9.5px] leading-snug text-faint">
            Drag a pet to pick it up. Double-click one to send it between the
            widget and the desktop. Right-click a widget pet to hop it up a card.
          </p>
        </div>
      )}
    </Card>
  )
}
