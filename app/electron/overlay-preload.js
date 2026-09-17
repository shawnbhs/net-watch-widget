// The bridge for the desktop pet overlay.
//
// Deliberately narrower than the widget's own preload: this window draws pets
// on the desktop and nothing else, so it can neither reach the sidecar nor move
// the widget. It receives a roster, reports where the mouse is being caught, and
// tells the widget when a pet has been sent home.
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('nwPets', {
  /** The display this overlay covers, including where the taskbar starts. */
  bounds: () => ipcRenderer.invoke('pets-bounds'),

  /** The roster and tuning. Called again on every change in the widget. */
  onState(fn) {
    const handler = (_e, state) => fn(state)
    ipcRenderer.on('pets', handler)
    return () => ipcRenderer.off('pets', handler)
  },

  /** The display changed shape; the pets need re-fitting to it. */
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
