// The only bridge between the renderer and Node. contextIsolation is on and
// nodeIntegration is off, so this surface is the entire attack area: it exposes
// four verbs and no way to name an arbitrary channel or module.
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('nw', {
  /**
   * True when the widget was last closed collapsed to its tab.
   *
   * Passed as a renderer argument rather than read back over IPC because the
   * first render has to be in the right mode: the page sizes the window from
   * its own layout, so a full-width first paint at the tab's position is a bar
   * across the top of the screen.
   */
  startTab: process.argv.includes('--nw-tab'),
  /**
   * The edge the tab is docked to: 'top', 'left' or 'right'.
   *
   * Sent even when the widget starts expanded, because it decides the tab's
   * shape -- a row of dials or a column of them -- the moment it is collapsed.
   */
  startEdge: process.argv.find((a) => a.startsWith('--nw-edge='))?.slice(10) ?? 'top',
  /**
   * The size the widget was last set to by hand, as a multiplier.
   *
   * A renderer argument for the same reason as the two above: the page sizes
   * the window from its own layout, so it has to be drawn at the right scale in
   * the first frame rather than snap to it in the second.
   */
  startScale: Number(
    process.argv.find((a) => a.startsWith('--nw-scale='))?.slice(11)) || 1,
  /** Subscribe to sidecar messages. Returns an unsubscribe function. */
  onData(fn) {
    const handler = (_e, msg) => fn(msg)
    ipcRenderer.on('data', handler)
    return () => ipcRenderer.off('data', handler)
  },
  /**
   * The tab was dragged onto a different edge, and has to change shape.
   *
   * Only the edge crosses: where the tab sits *along* an edge is arithmetic the
   * main process does on its own, and the page would do nothing differently
   * knowing it.
   */
  onDock(fn) {
    const handler = (_e, edge) => fn(edge)
    ipcRenderer.on('dock', handler)
    return () => ipcRenderer.off('dock', handler)
  },
  /** True while the window is being moved and the frost is hidden. */
  onMoving(fn) {
    const handler = (_e, value) => fn(value)
    ipcRenderer.on('moving', handler)
    return () => ipcRenderer.off('moving', handler)
  },
  /** Send a command to the Python sidecar (refresh, net_toggle, copy, …). */
  send(cmd) { ipcRenderer.send('cmd', cmd) },
  /** Report measured card rectangles so the acrylic panes can follow them. */
  panes(rects) { ipcRenderer.send('panes', rects) },
  hidePanes() { ipcRenderer.send('panes-hide') },
  /**
   * Resize the shell to the content's measured size.
   *
   * `{ width, height, tab }` -- `tab` is the mode, and carrying it here rather
   * than on a message of its own is what lets the main process resize and
   * re-dock in one step. See the handler in main.js.
   */
  resize(size) { ipcRenderer.send('resize', size) },
  /** Frameless drag, by delta so the window never jumps to the cursor. */
  drag(delta) { ipcRenderer.send('drag', delta) },
  /** End of a drag: saves the position, and snaps the tab to a side. */
  dragEnd() { ipcRenderer.send('drag-end') },
  /** Remember the hand-set widget size. Sent when the grip is released. */
  scale(value) { ipcRenderer.send('scale', value) },
})
