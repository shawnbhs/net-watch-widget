# Net Watch — React + Tailwind frontend

The widget's UI, as a React + Tailwind page in an Electron shell. The Python
core is unchanged and still does all the work; it just speaks JSON now instead
of writing into Tk labels.

```
ip_bar.py  ──┐
             ├─ sidecar.py ──stdout JSON lines──▶ electron/main.js ──IPC──▶ React
  (unchanged)┘                ◀──stdin commands──
```

---

## Running it

```powershell
cd app
npm install
npm run build
npm start
```

`npm start` goes through `scripts/launch.js`, which strips
`ELECTRON_RUN_AS_NODE` from the environment before spawning Electron. Some
terminals — and anything itself running inside Electron — export that variable,
and inherited it makes `electron.exe` behave as a plain Node binary: the app
dies with `app is undefined` and a stack trace that points nowhere near the
cause.

While developing the page, `npm run dev` rebuilds on change; relaunch Electron
to pick it up.

Set `NET_WATCH_PYTHON` if `pythonw.exe` is not the interpreter you want the
sidecar to run under.

---

## Why the glass works the way it does

This is the part that is not obvious, and it is the reason the shell is more
than a `loadFile` call. Both points below were established by direct
measurement, not from documentation.

**`backdrop-filter` cannot frost the desktop.** It blurs what sits behind an
element *within the page's own compositing stack*, and a transparent window has
nothing there. A card with `backdrop-filter: blur(18px)` over a striped desktop
leaves those stripes perfectly sharp — the browser has no access to the pixels
behind its own window. Real desktop blur comes from DWM, which Electron exposes
as `setBackgroundMaterial('acrylic')`.

**DWM will not clip that material to a region.** It frosts the whole window
rectangle, and `SetWindowRgn` does not constrain it. So one window can only ever
be a frosted rectangle — never "frosted cards floating over nothing".

Hence the architecture: **a fully transparent content window, with one small
acrylic window parked behind each card.** The renderer measures its own cards
and reports their rectangles (`src/panes.jsx`); the main process keeps one
backing window under each (`syncPanes`). The gaps between cards are genuinely
empty, because no window spans them.

The page draws only what glass looks like at its *edges* — the hairline border
and the inset top highlight. The frost itself is never a CSS property.

### Why the cards are 8px round, not 20px

The design asks for a 20px corner radius. The cards are drawn at 8px, and that
is a platform limit rather than a preference.

A pane is a window, and the only rounding DWM will apply to one is its own
corner preference — a fixed radius with no parameter, measured here at about
**8px**. `SetWindowRgn`, the obvious alternative, does not clip an acrylic
backdrop at all (measured earlier in this project, on the Tk side). So a 20px
card over an 8px-capable frost leaves bare square corners poking out behind
every box. The frost cannot be made rounder, so the card is drawn at the radius
the frost can actually reach.

Electron has no binding for `DwmSetWindowAttribute`, so the pane's window handle
goes to the Python sidecar, which already has that exact call in `chrome.py` —
cheaper than a native Node module for one API call.

### The consequence: switching modes

A pane is an OS window, and moving one lands roughly **100 ms** after the page
has already repainted. That gap is the whole difficulty of the mode toggle, and
three things keep it from showing:

1. **The panes are hidden before the new layout renders**, not after. Left up,
   the old frost sits under the new layout for that whole 100 ms — a column of
   lit rectangles with nothing in them, which reads as the widget coming apart.
   Hiding first costs one frame with no frost, which reads as it catching up.
2. **The panes are pooled, never destroyed.** Building a `BrowserWindow` is slow
   enough to see, and switching to full mode needs three more.
3. **The entrance animation does not replay on a switch.** Every card is a fresh
   mount when the mode changes, so an unconditional entrance started them all at
   opacity 0 — the widget showed nothing but frost for most of a second.

Measured with `scripts/shot.py`-style high-rate capture: the switch went from
roughly 200 ms of visibly broken state to three frames, about 21 ms.

### The same consequence: moving the widget

Dragging has the identical shape of problem, and it needed a different answer.
The panes do follow the window now — `win.on('move')` repositions all ten, which
costs about **4 ms**, so the main process keeps up easily. What cannot keep up is
DWM: an acrylic window re-samples the desktop behind its new position on its own
schedule, several frames late, so a visible pane trails well behind the card it
belongs to.

So the frost is **hidden for the duration of a drag** and the renderer paints a
flat panel behind each card instead (`[data-moving]` in `index.css`). A card
keeps a solid background the whole way across the screen; it just stops being
see-through until the widget is put down, 140 ms after the last move event, at
which point the panes are placed correctly and revealed. The stand-in is
near-opaque on purpose: acrylic is translucent too, but it *blurs* what it lets
through, and a flat panel at the same alpha shows the desktop sharply behind the
text — which reads as a card that has lost its background rather than one
carrying it.

A card cannot be animated *into* position for the same reason. Its frost would
have to be moved per frame, per pane, which is neither cheap nor smooth — so the
entrance is opacity-only and the mode switch is instant.

**A drag, though — not every move.** That distinction was missing at first and
it was the tab that exposed it. `win.on('move')` cannot tell a hand from a
`setBounds`, so *docking* the tab hid the frost too, and a dock is one jump
rather than a continuous slide. Paying 140 ms of flat card to spare a single
frame of late frost is a bad trade, and it read as the glass blinking. A
programmatic move now just takes the panes with it (`repositionPanes`, which had
been written and never called); only a real drag — tracked from the `drag` IPC
the renderer sends — still hides them.

### The third mode: a tab on the screen edge

The mini bar is not a smaller compact. Compact and full are *placed* — they live
wherever the widget was dragged to — and the tab is *docked*: it belongs to the
top edge of a screen, at one of three points along it. So it is the one mode
whose position the page does not control, and that changes where the decisions
live.

**The mode travels with the size.** `nw.resize` carries a `tab` flag, and the
main process resizes and re-docks in a single `setBounds`. Sending the two as
separate messages leaves one frame showing the tab at the old width's offset, or
the expanded widget still pinned to the screen edge — and one frame is all this
shell has ever been fighting over. The same message is what restores the
remembered free position on the way back out; the tab's own coordinates are
never saved, because they are derived from the screen.

**Its size does not depend on its data.** Every dial is always drawn, showing a
dash until it has a reading, and the ping and the VPN chip sit in slots of a
fixed width. That is not tidiness. A docked tab is *positioned* from its own
size, so anything that changes that size moves the window — and drawing only the
dials that had data meant the tab came up 166px wide and jumped 153px sideways a
second later, when the AI readings landed. The same trap has a two-pixel
version: the VPN chip is the only state of that chip with a visible border, so
the chip grew by 2px when the first lookup answered, which in the column layout
is 2px of window. Every state carries a border now, transparent where the design
has none.

The remembered tab size (`tabW`/`tabH` in `position.json`) is the same argument
one launch earlier: it is written as soon as the size settles rather than at
quit, so the next launch opens the window at the real size and lands on its dock
in the first frame instead of correcting itself in the second.

**It overhangs its edge by ten pixels.** A card's frost is a DWM window with an
unaskable ~8px corner radius, so a tab that began exactly at the screen edge
would show two rounded notches biting out of it. Pushing the card past the edge
puts that rounding out of sight; `MiniBar` pads the same ten pixels back on, on
whichever side the bleed is, so the dials stay centred in the part that can be
seen. The two numbers have to agree, and they are written down in `main.js` and
`App.jsx` both.

**A dock is an edge plus a place along it, and only the edge reaches the page.**
The edge decides the tab's shape — a row of dials on the top, a column on a side
— which is the renderer's business. Where it sits along that edge is arithmetic
against the work area and never leaves the main process.

That split is also what makes a drag onto a *different* edge a two-step landing.
The size the main process holds at the moment of the drop belongs to the old
shape, so it can place neither the tab nor the stops it should snap between; it
sends the new edge to the page and keeps the drop point (`dropCentre`). The page
relays out, and the resize that comes back is measured against that remembered
point — which is also why that one resize has to ignore the "still moving" guard
that otherwise stops a drag being interrupted by a content change.

**It comes up in the right mode rather than switching into one.** The page sizes
the window from its own layout, so a renderer that started full and corrected
itself would paint a 372px bar across the top of the screen first. The mode is
passed as a renderer argument (`additionalArguments`, read back as
`nw.startTab`) so the first render is already the tab.

**A drag chooses a dock, not a position.** The window follows the pointer
freely, which is what lets it be thrown to another monitor; `drag-end` then
resolves it to the nearest of the nine on whatever display it was dropped on.
Two details decide whether that lands where the user meant:

- **It follows the pointer, not the window.** The window is a poor proxy: a tab
  grabbed near one end sits a long way from where the user is actually pointing,
  and a 363px bar is more than a tenth of the screen wide. The pointer's screen
  position rides along with the drag delta.
- **Ties break in favour of staying put.** The boundaries between three edges
  are lines, and a drop that lands on one is otherwise a coin toss. Another edge
  has to be nearer by `EDGE_STICK` to take the tab off the one it is on. The
  margin is deliberately small: by the time the button comes up the user has
  aimed, and blocking a deliberate move is the worse failure.

The edge is picked by *fraction* of the screen rather than by pixels: on a
2560x1440 display a raw distance comparison hands the top edge almost the whole
screen, because there is so much less of it to cross vertically. Press and click
are the same gesture until the pointer has moved a few pixels, so the tab does
not open under a hand that was trying to slide it.

**The shape changes on the drop, not during the drag.** Reshaping live was
built and then taken out. It is the more informative behaviour — you can see
which edge the tab is heading for while there is still time to change your mind
— but it is the worse one to use: the thing under your hand turns into a
different thing mid-gesture, and because a row becoming a column keeps its
top-left corner, it has to jump to stay under the cursor when it does. Keeping
the shape makes a drag feel like moving one object.

### Flagging a changed address

`useChanged` holds a card red for a minute after its address moves. Three
decisions in it are worth stating, because each is a way the warning would
otherwise be wrong rather than merely ugly:

- **The first value is not a change.** The widget has only now learned what the
  address always was; flagging that paints the panel red on every launch. Same
  argument as the bars not animating up from zero on first paint.
- **Nothing involving a placeholder is a change.** `…` before the first lookup,
  `?` or `Error` when one fails. A failed lookup is not a move, and treating one
  as a move cries wolf exactly when the network is misbehaving — which is when a
  real warning most needs to be believed.
- **The timer is its own effect.** Keyed on the alert rather than on the value,
  so the flag clears on time even if no further readings arrive. That is
  precisely the case where the address stopped changing, and the one where a
  stuck red border would be worst.

The pulse is a `box-shadow` ring, not a border that grows. A card that changed
size by a pixel would drag its frost pane out of register with it, and the pane
has no idea any of this happened — it is a separate OS window.

### Resizing by hand

The corner grip sets one number, `zoom` on the shell. Zoom rather than a
transform because it is a *layout* scale: every measured box grows with it, so
the window still follows the content and the frost panes still match their
cards, without either one being told the factor. A transform would change what
the widget looks like and nothing about what it measures.

Two things fell out of building it, both measured rather than assumed:

**`offsetLeft` and `getBoundingClientRect` disagree under zoom.** The first
keeps reporting the unscaled layout; the second reports what was painted.
`panes.jsx` measured with the offset walk, deliberately — offsets are immune to
a transform, and the entrance animation used to travel — so every card's frost
came out at the unscaled size. Multiplying the offsets back up is *not* the same
number, either: at 1.4 that was a pixel out on half the cards. The panes measure
the visual box now, which is safe because the entrance is opacity-only, and the
comment there says what would make it unsafe again.

**Windows will not make a window shorter than 39px.** Not with `minHeight: 1`,
not by making it resizable — measured all three ways. A pane is a window, so
that is a floor on how small a *card* can be backed by frost. The title and
footer cards are 43px, which puts the smallest honest scale at 0.92; below that
their frost is visibly taller than they are. Growing has no such limit.

Cards are also watched individually by the ResizeObserver now, not just the
document root. Watching only the root misses a card that changes height without
the column doing so — invisible at scale 1, because the numbers happened to
round the same way.

---

## What the full view shows

Title · **IP address** and **Runflare** side by side, each with its own exit
country · **Local / gateway** and both **ping targets** · **Hardware** and
**Timezone** · **AI usage**: Claude 5h/week/model and GPT 5h/week, each with a
countdown, plus the plan line · **Quick checks**: ASN, ISP, proxy/VPN,
datacenter, hostname, DNS leak and a connection score, with five external
lookups · a footer carrying the local and Tehran clocks, the VPN chip and the
controls.

The two Runflare and IP-address cards can disagree about the country -- that is
the point of the second lookup, and an Iranian exit is detected from the address
ranges rather than from the geo-IP reply.

**Quick checks run on an IP change, and on demand — never on the network
cycle.** Two of the three lookups behind them are free-tier enrichment
endpoints, and the network cycle runs every three seconds, so wiring them
together would mean twelve hundred requests an hour to services doing us a
favour. An address change is the only event that can actually change the
answer, and it is rare — so the panel fills itself in a second or two after
launch, and again whenever the VPN moves you, without ever polling.

---

## What moved, and what did not

| | Before (Tk) | Now |
|---|---|---|
| Data, geo checks, AI tokens, adapters | `ip_bar.py` | **unchanged**, via `sidecar.py` |
| Icons | `glass.py`, ~300 lines of SDF rasterising | inline SVG |
| Rounded panels, bars, badges | `glass.py`, ~600 more lines | CSS |
| Layout and scaling | hand-measured, `_fit_cards` / `_scale_pads` | flexbox |
| Desktop blur | `chrome.py` | `setBackgroundMaterial` — *same DWM call* |

`glass.py` and the Tk UI are not deleted: `ip_bar.py` still runs standalone.
`sidecar.py` imports it without creating a Tk root, so neither build is a fork
of the other.

---

## Files

| Path | What it is |
|---|---|
| `electron/main.js` | Window, acrylic panes, sidecar process, IPC |
| `electron/preload.js` | The whole renderer↔Node surface: four verbs |
| `src/panes.jsx` | Measures cards, batches rectangles to the main process |
| `src/useSidecar.js` | Folds the JSON-line stream into one state object |
| `src/App.jsx` | Layout: the tab, and the compact and full modes |
| `src/components/` | Card, bar, ring, icon button, country chip |
| `scripts/shot.py` | Screenshot tool — see its docstring before using it |

---

## Screenshotting it

Photographing a transparent always-on-top window has three traps, all of which
`scripts/shot.py` handles:

- a plain screen `BitBlt` **omits layered windows**, so the capture must pass
  `CAPTUREBLT`;
- a PowerShell capture process is not per-monitor DPI aware, so on a scaled
  display it photographs the wrong monitor;
- **while the workstation is locked, every screen capture returns the lock
  screen image** — a run that produces bare wallpaper usually means that, not a
  broken window.

`--shot` sidesteps all three by rendering the page directly to `page.png`,
composited over an opaque colour so white-on-transparent text is readable.

`--compact` and `--mini` press the corresponding footer button before the
capture, and `--click=<label>` presses anything else by its `aria-label`.
`--tabdrag=<dx>,<dy>[,<ms>]` drags the tab and lets go, so the snap to a dock --
including onto another edge, where the tab changes shape -- can be watched
without a hand on the mouse. It drives the renderer's own handlers, which the
older `--dragtest` (a bare `setPosition` loop) skips entirely, and it must send
`globalX`/`globalY`: a drag is a delta between *screen* positions, so a
synthesised move without them reports zero pixels every time and the tab never
budges.
