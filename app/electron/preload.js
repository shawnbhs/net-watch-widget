// The only bridge between the renderer and Node. contextIsolation is on and
// nodeIntegration is off, so this surface is the entire attack area: it exposes
// a fixed set of named verbs and no way to name an arbitrary channel or
// module.
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
  /**
   * The widget is now on a different monitor.
   *
   * Anything measured against the screen -- above all the largest scale that
   * still fits -- has to be measured again, and a plain window move gives the
   * page nothing else to go on.
   */
  onDisplay(fn) {
    const handler = (_e, info) => fn(info)
    ipcRenderer.on('display', handler)
    return () => ipcRenderer.off('display', handler)
  },

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
   * Suspend or resume the acrylic panes entirely.
   *
   * Below a certain hand-set scale a card is shorter than the ~39 device pixels
   * Windows will make a window, so the pane behind it cannot be the size of the
   * card it is meant to be the frost of. Rather than let the panes quietly stop
   * matching the layout, they are put away and the cards paint their own
   * background in CSS until the widget is grown again.
   *
   * This is deliberately not `hidePanes`, which hides until the next
   * measurement arrives and is undone by the very next batch. Suspension is a
   * latch: it has to survive every measurement in between, and the renderer has
   * to be able to say when it is over. One channel carrying the state rather
   * than a pair of verbs, because the receiving side then has a value to store
   * instead of two events to keep in agreement.
   *
   * `Boolean` here is the whole validation, and it is enough: the argument is a
   * single flag, so every value a renderer bug could produce -- a string, an
   * object, nothing at all -- has a defined reading, and the main process
   * receives a primitive it can store without checking. The same coercion is
   * applied again on the other side, as with `pets-interactive`; this side runs
   * in the renderer's world and is not a trust boundary by itself.
   */
  frostSuspend(on) { ipcRenderer.send('frost-suspend', Boolean(on)) },
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

  /**
   * The species list, built by scanning assets/pets in the main process.
   *
   * Asked for once, on mount. It is four hundred filenames' worth of structure
   * and it cannot change while the app is running, so there is nothing to
   * subscribe to.
   */
  petManifest() { return ipcRenderer.invoke('pet-manifest') },
  /**
   * Publish the pets that are loose on the desktop, and how they behave.
   *
   * The widget owns the whole roster; this is the slice of it that belongs to
   * the overlay. Sending an empty list is how the overlay window is closed --
   * see `syncOverlay`.
   */
  petsSync(state) { ipcRenderer.send('pets-sync', state) },
  /** A loose pet was sent home from the desktop. Returns an unsubscribe. */
  onPetHome(fn) {
    const handler = (_e, id) => fn(id)
    ipcRenderer.on('pet-home', handler)
    return () => ipcRenderer.off('pet-home', handler)
  },

  // ── AI accounts ─────────────────────────────────────────────────────────────
  //
  // Named verbs, one per command, rather than letting the pane build its own
  // message and hand it to `send`. `send` already exists and would have worked,
  // but these are the first commands that carry a string somebody typed, and a
  // named method is what keeps the set of things the renderer can ask for
  // closed: the argument is the only variable part, and its shape is fixed
  // here and checked again in the main process before it reaches the sidecar.
  //
  // Coercion happens here so a wrong type is a rejected command rather than an
  // exception thrown into whichever React handler called it. The main process
  // re-validates everything -- this side runs in the renderer's world and is
  // not a trust boundary by itself.
  //
  // There is no reply channel: every one of these is answered by the sidecar's
  // usual broadcast -- the `ai` message with its `accounts` array, or the
  // `ai_providers` registry -- which the pane is already subscribed to through
  // `onData`.

  /** List the vault's accounts. Answered by the next `ai` message. */
  aiAccounts() { ipcRenderer.send('cmd', { cmd: 'ai_accounts_list' }) },
  /** Ask for the vendor registry. Answered by an `ai_providers` message. */
  aiProviders() { ipcRenderer.send('cmd', { cmd: 'ai_providers' }) },
  /**
   * Register a new account for a vendor and sign it in.
   *
   * `label` is optional: omitted, the vault names the account after its
   * provider, so an empty box in the UI is not an error to report.
   */
  aiAccountAdd(provider, label) {
    ipcRenderer.send('cmd', {
      cmd: 'ai_account_add',
      provider: String(provider ?? ''),
      ...(label === undefined || label === null ? {} : { label: String(label) }),
    })
  },
  /** Forget an account and its stored credential copy. */
  aiAccountRemove(id) {
    ipcRenderer.send('cmd', { cmd: 'ai_account_remove', id: String(id ?? '') })
  },
  /** Change an account's display name. The credential is untouched. */
  aiAccountRename(id, label) {
    ipcRenderer.send('cmd', {
      cmd: 'ai_account_rename',
      id: String(id ?? ''),
      label: String(label ?? ''),
    })
  },
  /**
   * Re-authenticate one account.
   *
   * The point of the vault: the vendor CLI holds one credential at a time, so
   * this swaps the named account's copy in before the sign-in and keeps the
   * others intact. All of that is the Python side's -- here it is one id.
   */
  aiAccountLogin(id) {
    ipcRenderer.send('cmd', { cmd: 'ai_account_login', id: String(id ?? '') })
  },
  /** Poll one account's usage now, rather than waiting for the next cycle. */
  aiAccountRefresh(id) {
    ipcRenderer.send('cmd', { cmd: 'ai_account_refresh', id: String(id ?? '') })
  },
})
