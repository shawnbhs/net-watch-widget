// Electron shell for the Net Watch widget.
//
// Two things here are not obvious and are both load-bearing.
//
// 1. `backdrop-filter` cannot frost the desktop. It blurs what is behind an
//    element *within the page*, and a transparent window has nothing there.
//    Probed directly: a card with blur(18px) over the desktop left the pixels
//    behind it perfectly sharp. Real frost comes from DWM, which Electron
//    exposes as setBackgroundMaterial('acrylic').
//
// 2. DWM applies that material to the whole window rectangle and will not clip
//    it to a region. So a single window cannot be "frosted cards floating over
//    nothing" -- it is a frosted rectangle, always. The way to get floating
//    cards is one acrylic window per card, sitting behind a fully transparent
//    window that draws the content. The gaps between cards are then genuinely
//    empty, because no window spans them.
//
// The renderer measures its own cards and reports their rectangles; `syncPanes`
// keeps one acrylic window under each.

const { app, BrowserWindow, ipcMain, screen, shell } = require('electron')
const { spawn } = require('node:child_process')
const path = require('node:path')
const fs = require('node:fs')
const pets = require('./pets')

const ROOT = path.join(__dirname, '..', '..')
const DEV = process.argv.includes('--dev')

// --trace timestamps the two events that race during a mode switch: the pane
// batch and the window resize. Their order and spacing is the difference
// between a clean swap and a tenth of a second of frost in the wrong place, and
// it is not visible from a screenshot.
const T0 = Date.now()
const TRACE = process.argv.includes('--trace')
const fakeIpAfter = Number(process.argv.find((a) => a.startsWith('--fake-ip='))?.slice(10) || 0)
let netSeen = 0
const trace = (...a) => { if (TRACE) console.error(`[t+${Date.now() - T0}ms]`, ...a) }

let win = null
let sidecar = null
/**
 * Pool of acrylic backing windows, matched to cards by position.
 *
 * A pool rather than a map keyed on card id, because creating a BrowserWindow
 * is slow enough to be seen: switching to full mode needs three more panes, and
 * building them on the spot put the frost about a tenth of a second behind the
 * content. The panes are anonymous rectangles -- nothing is ever drawn in one --
 * so any pane can back any card, and a mode switch becomes setBounds plus
 * show/hide on windows that already exist.
 */
const pool = []

// ── the transparent content window ────────────────────────────────────────────

function createWindow() {
  const saved = readSaved()
  tabbed = Boolean(saved?.tab)
  tabEdge = EDGES.includes(saved?.edge) ? saved.edge : 'top'
  tabAlong = ALONGS.includes(saved?.along) ? saved.along : 'end'
  freePos = saved ? clampToScreen(saved, FULL_SIZE) : defaultFree(FULL_SIZE)
  tabSize = saved?.tabW && saved?.tabH
    ? { width: saved.tabW, height: saved.tabH }
    : null
  scale = Number(saved?.scale) || 1

  // Both sizes are first guesses; the renderer resizes to its measured content
  // as soon as it has laid out. The tab's has to be close, though, because it
  // is positioned from its own size -- a wrong guess lands it visibly off its
  // corner for the frame before the first measurement arrives. The last real
  // size is remembered for exactly that frame; the fallbacks are only ever used
  // by a profile that has never been collapsed.
  const guess = tabSize
    ?? (HORIZONTAL_EDGES.includes(tabEdge)
      ? { width: 330, height: 62 }
      : { width: 66, height: 300 })
  const start = tabbed
    ? tabBounds(guess.width, guess.height, freePos)
    : { ...FULL_SIZE, ...freePos }

  win = new BrowserWindow({
    // Positioned in the constructor rather than moved after creation: a
    // transparent frameless window has no title bar to hide the hop, so a
    // create-then-setPosition shows the widget at the screen centre for a frame
    // before it lands where the user left it.
    ...start,
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // The renderer has to come up already in the right mode rather than
      // switch into it a frame later. It sizes the window from its own layout,
      // so a full-width first paint at the tab's position is a bar straight
      // across the top of the screen. The edge goes across whether the widget
      // starts collapsed or not, because it decides the tab's shape the moment
      // it is collapsed by hand.
      additionalArguments: [
        `--nw-edge=${tabEdge}`,
        `--nw-scale=${scale}`,
        ...(tabbed ? ['--nw-tab'] : []),
      ],
    },
  })

  // 'screen-saver' is the level that stays above a maximised window. Plain
  // alwaysOnTop sits below one, which for a status widget means invisible
  // exactly when you are working.
  win.setAlwaysOnTop(true, 'screen-saver')
  win.setVisibleOnAllWorkspaces(true)

  win.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  // The overlay goes with it. It is a window of its own, so leaving it open
  // would both keep `window-all-closed` from firing and leave pets wandering a
  // desktop whose widget no longer exists.
  win.on('closed', () => { win = null; destroyPanes(); pets.closeOverlay() })

  win.on('move', onWindowMove)

  win.webContents.on('did-fail-load', (_e, code, desc, url) =>
    console.error('[load failed]', code, desc, url))
  win.webContents.on('console-message', (_e, _lvl, msg) =>
    console.error('[renderer]', msg))

  // Diagnostic: dump the rendered page so a layout problem can be told apart
  // from a compositing one. The page is transparent and its text is white, so
  // a raw capture is white-on-nothing and unreadable; an opaque backdrop is
  // injected for the shot and removed again immediately.
  if (process.argv.includes('--shot')) {
    setTimeout(async () => {
      // capturePage keeps the page's alpha on a transparent window, so a raw
      // dump is white-on-nothing and unreadable. Compositing the bitmap over an
      // opaque colour here is the only way to see the layout without changing
      // what is being tested.
      // --click=<text> presses a real control before the capture. Clicking the
      // control rather than synthesising an event on the root matters: React
      // delegates from #root, which has no fiber of its own, so an event
      // dispatched there reaches no handler at all.
      const click = process.argv.includes('--compact')
        ? 'Compact view'
        : process.argv.includes('--mini')
          ? 'Mini bar'
          : (process.argv.find((a) => a.startsWith('--click='))?.slice(8) ?? '')
      // Comma-separated, and pressed in order, because some states are simply
      // not one click deep: adding a pet is "Add a pet" and then a species, and
      // a diagnostic that could only reach the first of those could never
      // photograph a pet at all.
      const wait = Number(process.argv.find((a) => a.startsWith('--wait='))?.slice(7) || 700)
      for (const step of click.split(',').filter(Boolean)) {
        await win.webContents.executeJavaScript(
          `(() => { const t = ${JSON.stringify(step)};`
          + ' const el = document.querySelector(`[aria-label="${t}"],[title^="${t}"]`)'
          + '   || [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === t);'
          + ' if (!el) return false;'
          // --dblclick reaches the controls that answer to one, which .click()
          // cannot synthesise: the resize grip's reset is the only way to test
          // that path without a hand on the mouse.
          + (process.argv.includes('--dblclick')
            ? ' el.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));'
            : ' el.click();')
          + ' return true })()')
        await new Promise((r) => setTimeout(r, wait))
      }
      // --flat forces the in-motion card background on, so the resting and
      // moving looks can be compared without a drag (and while the workstation
      // is locked, where a screen capture returns only the lock screen).
      if (process.argv.includes('--flat')) {
        await win.webContents.executeJavaScript(
          "document.querySelector('[data-flat]').setAttribute('data-flat','true')")
        await new Promise((r) => setTimeout(r, 260))
      }
      const img = await win.webContents.capturePage()
      const { width, height } = img.getSize()
      const px = img.toBitmap()          // BGRA, premultiplied
      const bg = [0x42, 0x34, 0x2b]      // #2b3442 as BGR
      for (let i = 0; i < px.length; i += 4) {
        const a = px[i + 3] / 255
        for (let c = 0; c < 3; c++) px[i + c] = Math.round(px[i + c] + bg[c] * (1 - a))
        px[i + 3] = 255
      }
      const flat = require('electron').nativeImage.createFromBitmap(px, { width, height })
      fs.writeFileSync(path.join(ROOT, 'page.png'), flat.toPNG())
      console.error('[shot] page.png', win.getBounds())
    }, 9000)
  }
  // Diagnostic: print each card's DOM rect next to the bounds of the acrylic
  // window behind it, so a drifting pane is visible without a screenshot.
  const pc = process.argv.find((a) => a.startsWith('--panecheck'))
  if (pc) {
    setTimeout(async () => {
      const dom = await win.webContents.executeJavaScript(
        "Array.from(document.querySelectorAll('[data-card]')).map("
        + "e=>{const r=e.getBoundingClientRect();"
        + "return {id:e.dataset.card,x:Math.round(r.left),y:Math.round(r.top),"
        + "w:Math.round(r.width),h:Math.round(r.height),"
        + "alert:e.dataset.alert==='true'}})")
      const o = win.getBounds()
      for (const d of dom) {
        const pane = pool[dom.indexOf(d)]
        const b = pane && !pane.isDestroyed() ? pane.getBounds() : null
        const want = { x: o.x + d.x, y: o.y + d.y, width: d.w, height: d.h }
        const ok = b && b.x === want.x && b.y === want.y
          && b.width === want.width && b.height === want.height
        // The rect as reported, unrounded, is the third number that matters: a
        // mismatch is either the page measuring something else or the rounding
        // on the way to a window, and only this tells the two apart.
        const sent = lastRects[dom.indexOf(d)]
        console.error(`[pane] ${ok ? 'OK  ' : 'DRIFT'} ${d.id.padEnd(6)}`
          + ` want ${want.x},${want.y} ${want.width}x${want.height}`
          + ` got ${b ? `${b.x},${b.y} ${b.width}x${b.height}` : 'none'}`
          + (ok ? '' : ` sent ${sent ? `${sent.id} ${sent.w.toFixed(2)}x${sent.h.toFixed(2)}` : 'none'}`))
      }
      const shown = pool.filter((p) => p && !p.isDestroyed() && p.isVisible()).length
      const flagged = dom.filter((d) => d.alert).map((d) => d.id)
      console.error(`[pane] ${dom.length} cards, ${shown} shown, ${pool.length} pooled`
        + `, flagged: ${flagged.length ? flagged.join(',') : 'none'}`)
    }, Number(pc.split('=')[1] || 10000))
  }
  const at = process.argv.find((a) => a.startsWith('--toggle-at='))
  if (at) {
    for (const ms of at.split('=')[1].split(',').map(Number)) {
      setTimeout(() => {
        trace('toggle click')
        win.webContents.executeJavaScript(
          "document.querySelector('[aria-label=\"Compact view\"],"
          + "[aria-label=\"Full view\"]').click()")
      }, ms)
    }
  }
  // Diagnostic: press, drag and release the tab the way a hand would, so the
  // snap to a side can be watched without one. Unlike --dragtest below, this
  // goes through the renderer's own handlers, which is the half that decides
  // whether a gesture was a drag or a click.
  //
  // Two details make it move rather than merely look like it should.
  //
  // `globalX/globalY` are not optional. A drag is reported as a delta between
  // screen positions, so a synthesised event without them reports every move as
  // zero pixels, never crosses the slop threshold, and the tab sits perfectly
  // still while the log insists twelve moves were sent.
  //
  // And the walk is driven in screen space, with the window-relative coordinate
  // derived from it each step. The window chases the pointer, so a fixed local
  // coordinate means something different every frame -- and one outside the
  // window is simply dropped, which a step longer than half a 66px-wide tab
  // manages on its own.
  const tabDrag = process.argv.find((a) => a.startsWith('--tabdrag='))
  if (tabDrag) {
    const STEP = 14
    const [dx, dy = 0, at = 9000] = tabDrag.split('=')[1].split(',').map(Number)
    setTimeout(async () => {
      const press = (type, x, y, gx, gy) => win.webContents.sendInputEvent({
        type, x, y, globalX: gx, globalY: gy, button: 'left', clickCount: 1,
      })
      // --drag-on=<label> grabs a named control instead of the tab itself, for
      // the gestures that are not "move the window": the resize grip in
      // particular, which cannot be exercised any other way.
      const onLabel = process.argv.find((a) => a.startsWith('--drag-on='))?.slice(10)
      if (onLabel) {
        const at = await win.webContents.executeJavaScript(
          `(() => { const t = ${JSON.stringify(onLabel)};`
          + ' const el = document.querySelector(`[aria-label="${t}"],[title^="${t}"]`);'
          + ' if (!el) return null; const r = el.getBoundingClientRect();'
          + ' return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) } })()')
        if (!at) { console.error('[tabdrag] no element', onLabel); return }
        const o = win.getBounds()
        let gx = o.x + at.x
        let gy = o.y + at.y
        const steps = Math.max(1, Math.ceil(Math.max(Math.abs(dx), Math.abs(dy)) / STEP))
        // Clamped for the same reason as the tab walk below, and even more
        // sharply here: the grip is *at* the corner, so the very first step
        // outward is already outside the window, and a dropped event means the
        // widget never grows, which keeps the pointer outside. One step in and
        // the gesture is dead.
        const local = () => {
          const c = win.getBounds()
          const clamp = (v, hi) => Math.max(2, Math.min(v, hi - 2))
          return [clamp(gx - c.x, c.width), clamp(gy - c.y, c.height)]
        }
        press('mouseDown', at.x, at.y, gx, gy)
        for (let i = 0; i < steps; i++) {
          gx += Math.round(dx / steps)
          gy += Math.round(dy / steps)
          press('mouseMove', ...local(), gx, gy)
          await new Promise((r) => setTimeout(r, 16))
        }
        press('mouseUp', ...local(), gx, gy)
        setTimeout(() => console.error('[tabdrag]', 'grip', JSON.stringify(win.getBounds())), 500)
        return
      }
      const b = win.getBounds()
      const steps = Math.max(1, Math.ceil(Math.max(Math.abs(dx), Math.abs(dy)) / STEP))
      const sx = Math.round(dx / steps)
      const sy = Math.round(dy / steps)
      let gx = b.x + Math.round(b.width / 2)
      let gy = b.y + Math.round(b.height / 2)
      // The window-relative coordinate is clamped inside the window, and that
      // is not a cosmetic detail. Chromium drops a synthesised event whose
      // local coordinate falls outside the window, and the window chases the
      // pointer one IPC hop late, so the two drift apart -- fatally fast on an
      // 82px-wide column, where there is only half a tab of slack. Clamping
      // costs nothing, because the drag delta is read from the *global*
      // coordinates: the local pair only has to be somewhere the event will be
      // delivered.
      const local = () => {
        const c = win.getBounds()
        const clamp = (v, hi) => Math.max(4, Math.min(v, hi - 4))
        return [clamp(gx - c.x, c.width), clamp(gy - c.y, c.height)]
      }
      press('mouseDown', ...local(), gx, gy)
      for (let i = 0; i < steps; i++) {
        gx += sx
        gy += sy
        press('mouseMove', ...local(), gx, gy)
        await new Promise((r) => setTimeout(r, 16))
      }
      press('mouseUp', ...local(), gx, gy)
      // Long enough to cover a change of edge, which lands only after the page
      // has relaid itself out and reported a new size.
      setTimeout(() => console.error('[tabdrag]', `${tabEdge}:${tabAlong}`, win.getBounds()), 900)
    }, at)
  }
  // Diagnostic: walk the window the way a drag does, so the panes can be
  // watched for lag without a human holding the mouse.
  const drag = process.argv.find((a) => a.startsWith('--dragtest='))
  if (drag) {
    const [stepMs, dx, steps] = drag.split('=')[1].split(',').map(Number)
    setTimeout(() => {
      let n = 0
      const id = setInterval(() => {
        if (!win || win.isDestroyed() || n++ >= steps) { clearInterval(id); return }
        const b = win.getBounds()
        win.setPosition(b.x - dx, b.y)
      }, stepMs)
    }, 9000)
  }
  if (DEV) win.webContents.openDevTools({ mode: 'detach' })
}

// ── acrylic backing windows ───────────────────────────────────────────────────

function makePane() {
  const pane = new BrowserWindow({
    width: 10,
    height: 10,
    show: false,
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: false,
    focusable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    acceptFirstMouse: false,
    backgroundColor: '#00000000',
    webPreferences: { offscreen: false },
  })
  pane.setAlwaysOnTop(true, 'screen-saver')
  pane.setIgnoreMouseEvents(true)
  stylePane(pane)
  // Nothing is ever drawn in a pane. Its entire job is to be a rectangle that
  // DWM frosts; the content window above it draws the border and the text.
  pane.loadURL('data:text/html,<style>html,body{margin:0;background:transparent}</style>')
  try {
    pane.setBackgroundMaterial('acrylic')
  } catch {
    // Pre-22H1 Windows, or transparency effects switched off. The card then
    // reads as a plain translucent panel, which is a reasonable degradation.
  }
  return pane
}

/**
 * @param {{id: string, x: number, y: number, w: number, h: number, r: number}[]} rects
 * Rectangles are CSS pixels relative to the content window's client area.
 */
/**
 * Card rectangles as last measured, in window-relative CSS pixels.
 *
 * Kept because the panes have to be repositioned on events the renderer knows
 * nothing about. Dragging the widget changes where the window is but not where
 * a card sits inside it, so the renderer has no new measurement to report -- and
 * without these the frost would stay behind on the desktop while the cards walk
 * away from it.
 */
let lastRects = []

/** Absolute bounds for one card rectangle, given the window's current origin. */
function paneBounds(origin, sf, r) {
  // Electron's setBounds takes DIPs, and the renderer measured in CSS px -- the
  // same unit -- so no scale conversion belongs here. sf is used only to round
  // to whole device pixels, which stops a card's frost from shimmering by a
  // pixel as the window moves on a fractional-scale display.
  const snap = (v) => Math.round(v * sf) / sf
  return {
    x: Math.round(origin.x + snap(r.x)),
    y: Math.round(origin.y + snap(r.y)),
    width: Math.max(1, Math.round(r.w)),
    height: Math.max(1, Math.round(r.h)),
  }
}

/** Re-place every visible pane against the window's current position. */
function repositionPanes() {
  if (!win || win.isDestroyed() || !lastRects.length) return
  const origin = win.getBounds()
  const sf = screen.getDisplayMatching(origin).scaleFactor || 1
  for (let i = 0; i < lastRects.length; i++) {
    const pane = pool[i]
    if (!pane || pane.isDestroyed() || !pane.isVisible()) continue
    pane.setBounds(paneBounds(origin, sf, lastRects[i]))
  }
}

/**
 * Hand a pane's window handle to the sidecar, which gives it its frost and its
 * rounded corners. See `style_pane` in sidecar.py for why neither can be done
 * from here: Electron's own acrylic goes flat on an inactive window, and it has
 * no binding for the only call that rounds a window's backdrop.
 *
 * Queued until the sidecar has announced itself, because the first panes are
 * created within a second of launch and can easily beat it.
 */
const styleQueue = []
let sidecarReady = false

function stylePane(w) {
  let hwnd = 0
  try {
    const buf = w.getNativeWindowHandle()
    hwnd = buf.length >= 8 ? Number(buf.readBigUInt64LE(0)) : buf.readUInt32LE(0)
  } catch {
    return
  }
  if (!hwnd) return
  if (sidecarReady) toSidecar({ cmd: 'style_pane', hwnd })
  else styleQueue.push(hwnd)
}

function flushStyleQueue() {
  sidecarReady = true
  while (styleQueue.length) toSidecar({ cmd: 'style_pane', hwnd: styleQueue.shift() })
}

function syncPanes(rects) {
  if (!win || win.isDestroyed()) return
  const origin = win.getBounds()
  const sf = screen.getDisplayMatching(origin).scaleFactor || 1
  lastRects = rects

  rects.forEach((r, i) => {
    let pane = pool[i]
    if (!pane || pane.isDestroyed()) {
      pane = makePane()
      pool[i] = pane
    }
    pane.setBounds(paneBounds(origin, sf, r))
    if (!pane.isVisible()) pane.showInactive()
  })

  // Surplus panes are hidden, not destroyed: the next switch back will want
  // them, and rebuilding a window is the slow part.
  for (let i = rects.length; i < pool.length; i++) {
    const pane = pool[i]
    if (pane && !pane.isDestroyed() && pane.isVisible()) pane.hide()
  }

  // Every showInactive above can land a pane on top of the content window, so
  // the content is raised once after the whole batch rather than per pane.
  if (!win.isDestroyed()) win.moveTop()
}

/**
 * Hide every pane at once.
 *
 * Called the instant the mode changes, before React has re-rendered. A pane is
 * an OS window, so moving one lands roughly a tenth of a second after the page
 * repaints -- long enough that the old frost is still sitting in the old place
 * while the new layout is already drawn, which reads as the widget breaking.
 * Hiding first trades that for a brief moment with no frost, which reads as the
 * frost catching up.
 */
/**
 * While the window is moving, show no frost at all.
 *
 * The panes do follow now -- repositioning all ten costs about 4ms, so the main
 * process keeps up easily. What cannot keep up is DWM: an acrylic window has to
 * re-sample the desktop behind its new position, and it does that on its own
 * schedule, several frames late. The visible result during a drag is a frosted
 * rectangle trailing well behind the card it belongs to.
 *
 * So the frost is hidden for the duration of the move and the renderer is told
 * to paint a flat panel behind each card instead. The card keeps a solid
 * background the whole way -- it just stops being see-through until the widget
 * is put down, at which point the panes are placed correctly and revealed.
 */
let moving = false
let moveIdle = null
/**
 * True while a hand is actually dragging the widget, as opposed to the window
 * being placed by code.
 *
 * The distinction is the whole point of the flag. Hiding the frost costs 140ms
 * of flat, see-through card, which is worth paying against a *continuous* drag
 * -- DWM re-samples the desktop several frames after a move, so a visible pane
 * trails the card the entire way across the screen. Against a single jump it is
 * a bad trade: docking the tab, or a resize that re-places it, is over in a
 * frame, and blinking the glass off for it is far more noticeable than the
 * frost being one frame late. So a programmatic move just takes the panes with
 * it.
 */
let dragging = false

function onWindowMove() {
  if (!win || win.isDestroyed()) return
  trace('move', JSON.stringify(win.getBounds()))
  if (!dragging) {
    repositionPanes()
    return
  }
  if (!moving) {
    moving = true
    hidePanes()
    win.webContents.send('moving', true)
  }
  clearTimeout(moveIdle)
  moveIdle = setTimeout(() => {
    moving = false
    if (!win || win.isDestroyed()) return
    win.webContents.send('moving', false)
    syncPanes(lastRects)
  }, 140)
}

function hidePanes() {
  for (const pane of pool) {
    if (pane && !pane.isDestroyed() && pane.isVisible()) pane.hide()
  }
}

function destroyPanes() {
  for (const pane of pool) if (pane && !pane.isDestroyed()) pane.destroy()
  pool.length = 0
}

// ── position memory ───────────────────────────────────────────────────────────

const POS_FILE = path.join(app.getPath('userData'), 'position.json')

const FULL_SIZE = { width: 380, height: 560 }

/**
 * How far the tab runs off the edge of the screen, and how far in from a corner.
 *
 * The bleed is not decoration. A card's frost is a DWM-acrylic window, and the
 * only rounding DWM will give one is its own fixed preference -- about 8px, with
 * no parameter (see the radius note in index.css). A tab that began exactly at
 * the screen edge would therefore show two rounded notches biting out of that
 * edge. Ten pixels of it are pushed past the edge instead, so those corners are
 * cut off by the screen and the tab reads as hanging from it rather than
 * floating near it. `MiniBar` pads the same amount back on, on whichever side
 * the bleed is -- the two numbers have to agree, and they are written down in
 * both places.
 */
const TAB_BLEED = 10
const TAB_INSET = 16

/**
 * A dock is an edge plus a place along it.
 *
 * The edge decides the tab's shape -- a row of dials on the top, a column on
 * either side -- which is the renderer's business; the place along it is pure
 * arithmetic and stays here. That split is why the main process tells the page
 * about an edge change and nothing else.
 *
 * The bottom edge was left out at first on the grounds that the taskbar lives
 * there and a tab overhanging it would sit on top of the Start button. That
 * reasoning was wrong: every dock is computed against `screen.workArea`, which
 * already has the taskbar subtracted from it, so the bottom of the work area is
 * the top of the taskbar and nothing ever overlaps it. The bleed is the only
 * part that crosses that line, and ten pixels of a tab tucked behind the
 * taskbar is exactly the effect the bleed exists to produce. Bottom is a real
 * edge, and it behaves as top does -- a horizontal row, with the same three
 * stops along it.
 */
const EDGES = ['top', 'bottom', 'left', 'right']
/**
 * The two edges a tab lies along as a row rather than a column.
 *
 * The shape is the renderer's business, but the arithmetic here has to agree
 * with it: which axis the tab is placed along, and which of its two dimensions
 * the stops are measured against, both follow from this and nothing else.
 */
const HORIZONTAL_EDGES = ['top', 'bottom']
/**
 * How much nearer another edge has to be before the tab leaves the one it is
 * on, as a fraction of the screen. The boundaries between three edges are
 * lines, and a drop that lands on one would otherwise be a coin toss; this
 * settles it in favour of not moving.
 */
const EDGE_STICK = 0.03
const ALONGS = ['start', 'center', 'end']

/** The expanded position, the mode, and where the tab is docked. */
let freePos = null
let tabbed = false
let tabEdge = 'top'
let tabAlong = 'end'
let tabSize = null
/**
 * The hand-set widget size. Held here only to be written down and handed back
 * at the next launch -- the scaling itself is entirely the page's business
 * (CSS `zoom` on the shell), and the window simply follows the content, which
 * is already the right size because zoom is a layout scale.
 */
let scale = 1

function readSaved() {
  try { return JSON.parse(fs.readFileSync(POS_FILE, 'utf8')) } catch { return null }
}

/** First run: the top right, where a status widget is least in the way. */
function defaultFree(size) {
  const area = screen.getPrimaryDisplay().workArea
  return { x: area.x + area.width - size.width - 24, y: area.y + 24 }
}

/**
 * A remembered position, forced back onto a screen that is actually attached --
 * otherwise a monitor unplugged since the last run parks the widget somewhere
 * with no way to drag it back.
 */
function clampToScreen(pos, size) {
  const area = screen.getDisplayNearestPoint(pos).workArea
  return {
    x: Math.min(Math.max(pos.x, area.x), area.x + area.width - size.width),
    y: Math.min(Math.max(pos.y, area.y), area.y + area.height - size.height),
  }
}

/** The point that decides which display the widget is currently on. */
function anchor() {
  if (!win || win.isDestroyed()) return null
  const b = win.getBounds()
  return { x: b.x + Math.round(b.width / 2), y: b.y + Math.round(b.height / 2) }
}

function workAreaAt(point) {
  const at = point ?? anchor()
  return (at ? screen.getDisplayNearestPoint(at) : screen.getPrimaryDisplay()).workArea
}

/** Where the tab starts along its edge: inset from one corner, or centred. */
function alongStop(origin, extent, size) {
  if (tabAlong === 'start') return origin + TAB_INSET
  if (tabAlong === 'end') return origin + extent - size - TAB_INSET
  return origin + Math.round((extent - size) / 2)
}

/**
 * Where the tab sits: hard against its edge, at its place along it.
 *
 * `at` is the point that decides *which display* this is all measured on, and
 * it is not optional on any path that follows a drag. Left to its default it
 * falls back to the window's own centre, which during and just after a gesture
 * is the one place that cannot be trusted -- see `workAreaAt`.
 *
 * Each edge pins one coordinate and lets `alongStop` choose the other. The
 * pinned one is pushed TAB_BLEED px past the work area, outwards: negative at
 * the top and left, past the far corner at the bottom and right, so the tab
 * hangs off the screen by the same amount whichever edge it is on.
 */
function tabBounds(width, height, at) {
  const area = workAreaAt(at)
  switch (tabEdge) {
    case 'top':
      return { x: alongStop(area.x, area.width, width), y: area.y - TAB_BLEED, width, height }
    case 'bottom':
      return {
        x: alongStop(area.x, area.width, width),
        y: area.y + area.height + TAB_BLEED - height,
        width,
        height,
      }
    case 'left':
      return { x: area.x - TAB_BLEED, y: alongStop(area.y, area.height, height), width, height }
    default:
      return {
        x: area.x + area.width + TAB_BLEED - width,
        y: alongStop(area.y, area.height, height),
        width,
        height,
      }
  }
}

/**
 * Which edge the tab was dropped nearest.
 *
 * A *fraction* of the screen, not a pixel distance: on a 2560x1440 display a
 * raw comparison makes the top edge win almost everywhere, because there is so
 * much less screen to cross vertically.
 */
function resolveEdge(point) {
  // The pointer's display, not the window's. The two disagree by half a tab
  // for the whole of every drag -- the hand is in the middle of the thing it is
  // carrying -- and on a multi-monitor desktop half a tab is enough to land the
  // two on opposite sides of a seam. When that happens the pointer is compared
  // against some *other* screen's rectangle, and the edge it picks is an edge
  // of a screen the user is not pointing at.
  const area = workAreaAt(point)
  const near = {
    top: (point.y - area.y) / area.height,
    bottom: (area.y + area.height - point.y) / area.height,
    left: (point.x - area.x) / area.width,
    right: (area.x + area.width - point.x) / area.width,
  }
  const best = EDGES.reduce((b, e) => (near[e] < near[b] ? e : b), 'top')
  // Ties break in favour of staying put, so that a nudge does not send the tab
  // to the far side of the screen. The margin is small on purpose: a drag that
  // really is heading for another edge has to win, and by the time the button
  // comes up the user has already aimed.
  if (best !== tabEdge && near[best] > near[tabEdge] - EDGE_STICK) return tabEdge
  return best
}

/** Which of the three places along that edge the pointer is nearest. */
function resolveAlong(point, width, height) {
  // The pointer's display, for the same reason as `resolveEdge`: the stop is
  // measured as an offset into a particular screen's work area, so getting the
  // screen wrong puts the tab at the right stop on the wrong monitor.
  const area = workAreaAt(point)
  const horizontal = HORIZONTAL_EDGES.includes(tabEdge)
  const pos = horizontal ? point.x : point.y
  const origin = horizontal ? area.x : area.y
  const extent = horizontal ? area.width : area.height
  const size = horizontal ? width : height
  const stops = {
    start: origin + TAB_INSET + size / 2,
    center: origin + extent / 2,
    end: origin + extent - TAB_INSET - size / 2,
  }
  tabAlong = ALONGS.reduce((best, k) =>
    (Math.abs(stops[k] - pos) < Math.abs(stops[best] - pos) ? k : best), 'center')
}

/**
 * The pointer, as of the last drag message, in screen coordinates.
 *
 * The hand is the thing being followed, not the window. The window is a poor
 * proxy for two reasons: its centre moves when the tab reshapes mid-drag, so
 * the edge decision shifts under itself; and a tab grabbed near one end sits
 * well off to the side of where the user is actually pointing.
 */
let pointer = null

/**
 * Where the pointer was released, while the tab is still the wrong shape for
 * its new edge. Only set when the edge somehow changed at the very last moment
 * -- normally the preview below has already reshaped it -- and it is what lets
 * that late reshape land on a dock rather than wherever the hand happened to be.
 */
let dropPoint = null

/**
 * Put the tab on a dock when the drag ends.
 *
 * Nine resting places, and no others: the tab belongs to a screen edge, not to
 * an arbitrary point near one, so dragging chooses a dock rather than a
 * position. The gesture itself is free, which is what lets the tab be carried
 * to another monitor -- the dock is resolved against whichever display the
 * pointer was over.
 *
 * The tab keeps its shape for the whole gesture and changes it here, on the
 * drop. Reshaping *during* the drag was tried -- it makes the destination
 * visible while there is still time to change it -- and it is worse to use: the
 * thing under the hand becomes a different thing mid-gesture, and it has to jump
 * to stay under the cursor when it does.
 *
 * Landing on a new edge is therefore two steps. A row cannot be placed on a side
 * edge, so the page is asked to reshape and the size it reports back is what
 * finishes the job -- measured against `dropPoint`, because by then the window
 * is no longer where the drop happened.
 */
function snapTab() {
  if (!win || win.isDestroyed()) return
  const b = win.getBounds()
  const point = pointer ?? { x: b.x + b.width / 2, y: b.y + b.height / 2 }
  const before = tabEdge
  tabEdge = resolveEdge(point)
  if (tabEdge !== before) {
    dropPoint = point
    win.webContents.send('dock', tabEdge)
    return
  }
  dropPoint = null
  resolveAlong(point, b.width, b.height)
  win.setBounds(tabBounds(b.width, b.height, point))
}

function savePosition() {
  if (!win || win.isDestroyed()) return
  const [x, y] = win.getPosition()
  // While tabbed, the window's own position is the dock, which is derived from
  // the screen anyway. What has to survive is where the expanded widget was, so
  // that opening the tab puts it back rather than leaving it at the edge.
  const free = tabbed ? (freePos ?? { x, y }) : { x, y }
  try {
    fs.mkdirSync(path.dirname(POS_FILE), { recursive: true })
    fs.writeFileSync(POS_FILE, JSON.stringify({
      ...free,
      tab: tabbed,
      edge: tabEdge,
      along: tabAlong,
      scale,
      // Remembered so the first frame after a tabbed launch is the right shape:
      // the window is positioned from this size, and a guess that is out by
      // fifty pixels puts the tab visibly off its corner until the page has
      // measured itself.
      ...(tabSize ? { tabW: tabSize.width, tabH: tabSize.height } : {}),
    }))
  } catch { /* a widget that cannot remember where it was is still a widget */ }
}

// ── the Python sidecar ────────────────────────────────────────────────────────

function startSidecar() {
  const py = process.env.NET_WATCH_PYTHON || 'pythonw.exe'
  sidecar = spawn(py, [path.join(ROOT, 'sidecar.py')], {
    cwd: ROOT,
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
  })

  let buf = ''
  sidecar.stdout.setEncoding('utf8')
  sidecar.stdout.on('data', (chunk) => {
    buf += chunk
    // A message can be split across chunks, so only whole lines are parsed and
    // the remainder is carried forward.
    let nl
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim()
      buf = buf.slice(nl + 1)
      if (!line) continue
      try {
        const msg = JSON.parse(line)
        if (msg.t === 'hello') flushStyleQueue()
        // Diagnostic: move the exit address after this many real readings, so
        // the changed-address warning can be seen without waiting for a tunnel
        // to drop. Every later reading carries the same fake value, so it is
        // one change rather than a flip-flop -- which is the thing being tested.
        //
        // Counted in readings, not milliseconds. A wall-clock trigger looks
        // equivalent and is not: the first network cycle can take ten seconds
        // when the geo lookups are slow, so the first reading the page ever saw
        // was already the fake one -- which is correctly *not* a change, and the
        // test failed while the feature worked.
        if (fakeIpAfter && msg.t === 'net' && ++netSeen > fakeIpAfter) {
          msg.ip = '203.0.113.9'          // TEST-NET-3, safe to print anywhere
        }
        if (win && !win.isDestroyed()) win.webContents.send('data', msg)
      } catch { /* a half-written line is not worth crashing over */ }
    }
  })

  sidecar.stderr.setEncoding('utf8')
  sidecar.stderr.on('data', (d) => console.error('[sidecar]', d.trim()))
  sidecar.on('exit', (code) => console.error('[sidecar] exited', code))
  sidecar.on('error', (err) => console.error('[sidecar] spawn failed', err.message))
}

function toSidecar(cmd) {
  if (!sidecar || sidecar.killed || !sidecar.stdin.writable) return
  try { sidecar.stdin.write(JSON.stringify(cmd) + '\n') } catch { /* gone */ }
}

// ── renderer arguments ────────────────────────────────────────────────────────
//
// Everything below this line arrives from the renderer, and the renderer is the
// one part of this app that can be wrong about a number. A pointer leaving the
// window mid-drag, a monitor whose scale factor changes under the gesture, a
// measurement taken while an element is still laying out -- each of those has
// produced a NaN delta here.
//
// Passing one on is not a glitch. Electron's geometry setters are native: they
// reject a non-integer argument by throwing out of the IPC dispatcher rather
// than out of the handler that called them, so there is nothing to catch, and
// an uncaught throw there takes down the entire main process behind a dialog.
// The widget dies on a stray mouse move. So a message that cannot be trusted is
// dropped -- noted in the trace, invisible on screen -- and the window keeps the
// geometry it already had.

const MAX_COORD = 1e6 // outside any real desktop, still comfortably finite

/** A finite number, or null. */
function finiteNum(v) {
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

/** A finite screen coordinate or delta, rounded, or null. */
function finiteCoord(v) {
  const n = finiteNum(v)
  return n === null || Math.abs(n) > MAX_COORD ? null : Math.round(n)
}

/** A finite, positive extent, rounded, or null. A window is never 0 wide. */
function finiteSize(v) {
  const n = finiteNum(v)
  return n === null || n < 0 || n > MAX_COORD ? null : Math.max(1, Math.round(n))
}

/** Schemes `open` may hand to the OS. See the handler for why this is closed. */
const OPEN_SCHEMES = new Set(['http:', 'https:'])

// ── renderer IPC ──────────────────────────────────────────────────────────────

ipcMain.on('panes', (_e, rects) => {
  // `rects` is read for .length and .forEach immediately, and every field flows
  // into a pane's setBounds. A non-array throws; a NaN field survives as far as
  // the native call and then throws there. Drop the bad rows, keep the rest --
  // one card failing to measure should not blank the frost behind all of them.
  if (!Array.isArray(rects)) { trace('panes', 'dropped: not an array'); return }
  const clean = rects.filter((r) => r
    && finiteCoord(r.x) !== null && finiteCoord(r.y) !== null
    && finiteSize(r.w) !== null && finiteSize(r.h) !== null)
  if (clean.length !== rects.length) {
    trace('panes', 'dropped', rects.length - clean.length, 'of', rects.length)
  }
  trace('panes', clean.length, clean.map((r) => `${r.id}@${r.y}+${r.h}`).join(' '))
  syncPanes(clean)
})
ipcMain.on('panes-hide', () => hidePanes())

// ── pets ──────────────────────────────────────────────────────────────────────
//
// The widget owns the roster; the overlay only ever draws the part of it that
// has been let loose on the desktop. That split is why every message here runs
// one way -- widget to overlay -- with the single exception of `pets-home`,
// which is a pet being handed back.

ipcMain.handle('pet-manifest', () => pets.getManifest())
ipcMain.handle('pets-bounds', () => pets.overlayBounds())
ipcMain.on('pets-sync', (_e, state) => pets.syncOverlay(state))
ipcMain.on('pets-interactive', (_e, v) => pets.setInteractive(Boolean(v)))
ipcMain.on('pets-home', (_e, id) => {
  if (win && !win.isDestroyed()) win.webContents.send('pet-home', id)
})

ipcMain.on('cmd', (_e, cmd) => {
  if (cmd?.cmd === 'quit') { app.quit(); return }
  if (cmd?.cmd === 'open' && cmd.url) {
    // openExternal hands the string to the OS, which will launch ANY registered
    // protocol handler -- ms-msdt:, search-ms:, smb:// (which leaks an NTLM hash
    // to whatever host it names), or any third-party app's own scheme. Every
    // real caller here opens an ordinary web lookup, so anything that is not
    // http(s) is either a bug or an attempt, and neither should reach the shell.
    let scheme = null
    try { scheme = new URL(String(cmd.url)).protocol } catch { /* unparseable */ }
    if (!OPEN_SCHEMES.has(scheme)) { trace('cmd', 'refused open', cmd.url); return }
    shell.openExternal(String(cmd.url))
    return
  }
  toSidecar(cmd)
})
/**
 * Size the window to the content, and move it if the mode has changed.
 *
 * `tab` is not a hint, it is the mode: a tab is positioned from its own width,
 * so the same message that reports a new size is the one that can place it
 * correctly. Doing the two as separate messages would show the tab at the old
 * width's offset for a frame, and the expanded widget at the screen edge for a
 * frame, which is the whole thing this shell exists to avoid.
 */
ipcMain.on('resize', (_e, msg) => {
  if (!win || win.isDestroyed()) return
  const tab = msg?.tab
  const w = finiteSize(msg?.width)
  const h = finiteSize(msg?.height)
  // A size that is not a number would be written to disk as the tab's remembered
  // geometry, so a single bad measurement would survive the restart that fixes
  // everything else. Drop it and keep the last good one.
  if (w === null || h === null) {
    trace('resize', 'dropped', String(msg?.width), String(msg?.height))
    return
  }
  trace('resize', `${win.getBounds().height} -> ${h}${tab ? ' (tab)' : ''}`)

  if (tab) {
    // Written down as soon as it settles, not at quit: this is what lets the
    // next launch open the window at the tab's real size, and so place it on
    // its dock correctly in the very first frame. Saving it only on the way out
    // means a crash -- or being killed -- loses it, and the tab starts at a
    // guessed size and visibly corrects itself.
    const grew = !tabSize || tabSize.width !== w || tabSize.height !== h
    tabSize = { width: w, height: h }
    if (!tabbed) {
      const [x, y] = win.getPosition()
      freePos = { x, y }
      tabbed = true
    }
    if (grew && !dragging) savePosition()
    // A reshape finishing a drag onto a new edge: this is the first size that
    // means anything on that edge, so it is the one that chooses the place
    // along it, measured against where the tab was actually let go of.
    if (dropPoint) {
      resolveAlong(dropPoint, w, h)
      const at = dropPoint
      dropPoint = null
      // `at`, not the default. By the time this arrives the window has been
      // reshaped where it lay -- a 480px row has become an 82px column -- so
      // its centre has moved, and it is the one coordinate in the whole
      // gesture guaranteed not to be where the drop happened.
      win.setBounds(tabBounds(w, h, at))
      savePosition()
      return
    }
    // A tab being dragged is not at its dock, and a content change mid-gesture
    // must not yank it back there under the hand holding it. It lands when the
    // button comes up.
    if (moving) win.setBounds({ ...win.getBounds(), width: w, height: h })
    else win.setBounds(tabBounds(w, h))
    return
  }
  if (tabbed) {
    tabbed = false
    const size = { width: w, height: h }
    win.setBounds({ ...clampToScreen(freePos ?? win.getBounds(), size), ...size })
    savePosition()
    return
  }
  win.setBounds({ ...win.getBounds(), width: w, height: h })
})
ipcMain.on('drag', (_e, msg) => {
  if (!win || win.isDestroyed()) return
  const dx = finiteCoord(msg?.dx)
  const dy = finiteCoord(msg?.dy)
  // This is the one that killed the app: setPosition is native and rejects a
  // NaN by throwing past this handler, so the crash arrived as a main-process
  // dialog in the middle of an ordinary drag. A frame with no usable delta is
  // simply not a move.
  if (dx === null || dy === null) {
    trace('drag', 'dropped', String(msg?.dx), String(msg?.dy))
    return
  }
  dragging = true
  // The pointer position is optional and independent: a bad one must not cost
  // us the move, it only means this frame does not update the edge guess.
  const px = finiteCoord(msg?.x)
  const py = finiteCoord(msg?.y)
  if (px !== null && py !== null) pointer = { x: px, y: py }
  const [x, y] = win.getPosition()
  win.setPosition(x + dx, y + dy)
})
ipcMain.on('scale', (_e, value) => {
  const n = Number(value)
  if (!Number.isFinite(n)) return
  scale = n
  savePosition()
})
ipcMain.on('drag-end', () => {
  // Cleared after the snap, not before: landing the tab on its dock is the last
  // move of the gesture, and it should finish the same way the rest of it ran --
  // frost hidden until the widget has been still for a moment.
  if (tabbed) snapTab()
  dragging = false
  savePosition()
})

// ── lifecycle ─────────────────────────────────────────────────────────────────

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => win?.show())
  app.whenReady().then(() => {
    createWindow()
    startSidecar()
  })
  app.on('window-all-closed', () => app.quit())
  app.on('before-quit', () => {
    savePosition()
    destroyPanes()
    pets.closeOverlay()
    toSidecar({ cmd: 'quit' })
    if (sidecar && !sidecar.killed) setTimeout(() => sidecar.kill(), 250)
  })
}
