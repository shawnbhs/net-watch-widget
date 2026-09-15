/**
 * The exit country, as a two-letter chip.
 *
 * Not a flag emoji. Windows ships Segoe UI Emoji without regional-indicator
 * glyphs -- Microsoft omits country flags deliberately -- so 🇬🇧 renders as the
 * bare letters "GB" in Chromium on every Windows machine. The old Tk build hit
 * exactly this and showed the same two letters by accident. Drawing the code as
 * a chip on purpose looks composed instead of broken, and needs no font, no
 * image set and no network.
 */
export function CountryChip({ code, small }) {
  const cc = (code ?? '').toUpperCase()
  const valid = /^[A-Z]{2}$/.test(cc)
  return (
    <span
      className={
        'glass-text grid shrink-0 place-items-center rounded-[5px] border ' +
        'border-edge-soft font-bold tracking-[0.06em] ' +
        (small ? 'h-[15px] w-[20px] text-[8.5px] ' : 'h-[22px] w-[26px] text-[10px] ') +
        (valid ? 'bg-badge text-ink' : 'bg-white/10 text-faint')
      }
      title={valid ? `Exit country: ${cc}` : 'Country unknown'}
    >
      {valid ? cc : '··'}
    </span>
  )
}
