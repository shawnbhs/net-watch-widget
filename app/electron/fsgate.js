// When to hide the widget for a full-screen app, and when to bring it back.
//
// The sidecar reports the foreground window every time it changes, plus a
// heartbeat. Acting on each report as it arrives makes the widget flicker:
// a window going full screen resizes in steps, and for a moment in between it
// does not cover the monitor, so a naive rule hides, shows and hides again
// inside a second. And Windows' own alt+tab / Task View surface is itself a
// full-monitor foreground window, which would hide the widget for the length
// of a key press.
//
// So a report is a wish, and a wish only counts once it has held for a while:
//   hide  -- full screen continuously for HIDE_MS
//   show  -- not full screen for SHOW_OTHER_MS when a different window took
//            the foreground (alt+tab: the user wants it back now), or for
//            SHOW_SAME_MS when the same window merely stopped covering the
//            screen (often a transition, so wait it out)
// Shell surfaces (`transient`) change nothing at all: they neither hide, nor
// show, nor re-raise, and they do not reset a wish already in progress.
//
// Pure: the caller owns the clock and the timers, which is what lets the
// regression test drive it by hand.

const HIDE_MS = 700
const SHOW_OTHER_MS = 250
const SHOW_SAME_MS = 1200

function initialGate() {
  return { hidden: false, wish: null, since: 0, wishHwnd: 0, hiddenHwnd: 0, lastHwnd: 0 }
}

/**
 * Feed one report (or `null` for a timer tick). Returns
 * { gate, action: 'hide' | 'show' | 'raise' | null, waitMs }
 * where `waitMs` > 0 means "call me again after this long".
 */
function stepGate(gate, msg, now, enabled = true) {
  const g = { ...gate }
  let action = null

  if (msg && !msg.transient) {
    const want = enabled && !!msg.full
    if (want === g.hidden) {
      g.wish = null                          // already in the wanted state
      if (!want && msg.hwnd && msg.hwnd !== g.lastHwnd) action = 'raise'
    } else if (g.wish !== want || (!want && msg.hwnd !== g.wishHwnd)) {
      g.wish = want
      g.since = now
      g.wishHwnd = msg.hwnd || 0
    }
    if (msg.hwnd) g.lastHwnd = msg.hwnd
  }

  let waitMs = 0
  if (g.wish !== null) {
    const need = g.wish ? HIDE_MS
      : (g.wishHwnd && g.wishHwnd !== g.hiddenHwnd ? SHOW_OTHER_MS : SHOW_SAME_MS)
    const held = now - g.since
    if (held >= need) {
      action = g.wish ? 'hide' : 'show'
      g.hidden = g.wish
      g.hiddenHwnd = g.wish ? g.wishHwnd : 0
      g.wish = null
    } else {
      waitMs = need - held
    }
  }
  return { gate: g, action, waitMs }
}

module.exports = { initialGate, stepGate, HIDE_MS, SHOW_OTHER_MS, SHOW_SAME_MS }
