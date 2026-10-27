// Which part of the widget window lets clicks fall through to what is behind.
//
// The window is a transparent rectangle; only the cards are drawn. Empty space
// is layout glue: the page itself, or an element that holds cards but is not
// inside one -- the gutter around them and the gaps between them. Anything
// else (a card and all its controls, a pet, a picker) keeps the mouse.

export function overNothing(t, doc = globalThis.document) {
  if (!t) return true
  if (doc && (t === doc.documentElement || t === doc.body)) return true
  if (typeof t.closest !== 'function') return false
  return !t.closest('[data-card]') && !!t.querySelector?.('[data-card]')
}
