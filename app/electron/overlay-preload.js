// The bridge for the desktop pet overlay.
//
// Deliberately narrower than the widget's own preload: this window draws pets
// on the desktop and nothing else, so it can neither reach the sidecar nor move
// the widget. It receives a roster, reports where the mouse is being caught, and
// tells the widget when a pet has been sent home.
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('nwPets', {
  /**
   * The desktop this overlay covers: every display, not one.
   *
   * Pure passthrough, deliberately. Everything geometric in the payload --
   * `displays[]`, `worldWidth`/`worldHeight`, each screen's `floor` -- is
   * already in this window's CSS px, converted per monitor in the main
   * process because a mixed-DPI desktop has no single scale. Nothing here
   * may scale, offset or origin-shift it: a second monitor to the left has
   * negative DIP coordinates, and re-deriving anything on this side is how
   * the two ends stop agreeing.
   */
  bounds: () => ipcRenderer.invoke('pets-bounds'),

  /** The roster and tuning. Called again on every change in the widget. */
  onState(fn) {
    const handler = (_e, state) => fn(state)
    ipcRenderer.on('pets', handler)
    return () => ipcRenderer.off('pets', handler)
  },

  /**
   * The desktop changed shape -- resolution, layout, or which monitor's
   * scale factor this window's CSS px is built from. Same payload as
   * `bounds()`, same units, passed through untouched.
   */
  onBounds(fn) {
    const handler = (_e, b) => fn(b)
    ipcRenderer.on('bounds', handler)
    return () => ipcRenderer.off('bounds', handler)
  },

  /**
   * Make the window solid, or let clicks through to the desktop.
   *
   * Called only when the hit-test result actually changes -- this crosses a
   * process boundary and ends in a SetWindowLong, which is not something to do
   * on every mousemove.
   */
  setInteractive: (v) => ipcRenderer.send('pets-interactive', v),

  /** A pet was double-clicked on the desktop: send it back to the widget. */
  sendHome: (id) => ipcRenderer.send('pets-home', id),
})
