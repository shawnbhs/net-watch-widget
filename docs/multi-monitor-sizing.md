# Multi-monitor pet sizing

How a sprite is given a constant *physical* size across monitors of different pixel
density, why the obvious approaches fail, and how to tell whether a change actually
worked.

This exists so nobody has to rediscover it. It is written against the pet engine
(`app/src/pets/engine.js`), the overlay payload producer (`app/electron/pets.js`) and
the renderer glue (`app/src/pets/overlay.js`), and it cites `file:line` throughout.

Measurements in this document come from one real two-monitor Windows desktop: a
~167 PPI laptop panel and a ~102 PPI external monitor. They are illustrative of the
size of the effect, not universal constants. **Every scale factor must be read live at
the moment of use; none of the numbers here may be hardcoded.**

---

## 1. The problem

The pet overlay is **one** `BrowserWindow` stretched across the whole desktop. A
browser window has **exactly one CSS pixel grid**. Windows gives a window the DPI of
the monitor it overlaps most (by device-pixel area) and applies that single scale
factor to the entire window, including the part hanging over a differently-scaled
screen (`app/electron/pets.js:352-358`).

So the renderer has one number, call it `S`, relating CSS px to device px, and it is
the same number over both panels:

```
device px = CSS px * S          # one S, window-wide
```

The size chain has no per-display term anywhere in it:

```
target = opts.size * world.scale * species.scale * pet.sizeFactor    engine.js:183
k      = target / variant.baseH                                      engine.js:184
img.style.width/height = canvas_metrics * k                          engine.js:226-227
```

`petHeight()` is `opts.size * this.scale` (`engine.js:1161`) — three globals and a
per-pet factor. The pet's position is not an input. Neither `applySize()`
(`engine.js:182-186`) nor `layoutClip()` (`engine.js:210-233`) reads `this.displays`,
`displayAt()`, `screenSpan()` or `devicePixelRatio`. A pet therefore occupies the same
number of CSS px, and hence the same number of *device* px, on every monitor.

Device pixels are not a physical unit. What the user sees is millimetres of glass:

```
mm = device px / ppi * 25.4
```

With `S = 1.10`, a sprite authored at **100 CSS px** is **110 device px** on both
panels:

| panel | real PPI | device px | physical height |
|---|---|---|---|
| external monitor | ~102 | 110 | **27.39 mm** |
| laptop panel | ~167 | 110 | **16.73 mm** |

**The same sprite is 63.7% taller on one monitor than the other.** It is not a
rounding artefact or a rendering nicety; the pet genuinely is a different physical
object on each screen, and nothing in the pipeline notices.

Two things follow that are easy to get wrong:

- **Geometry is already handled; size is not.** `overlayBounds()` converts each
  display's DIP rect to device px with *that display's own* scale factor
  (`dipRectToPhysical`, `pets.js:325-349`) and only then divides the whole desktop by
  the single window scale (`pets.js:390-391`). Screen rectangles are consistent. The
  same care was never applied to sprite dimensions.
- **`S` is not a constant.** It is chosen by `windowScale()` (`pets.js:359-369`) as the
  scale factor of the display with the largest device-pixel overlap with the window's
  own frame — not the primary, not a fixed monitor. On the measured desktop it is
  1.65 when the window spans both screens and 1.10 when Windows clamps the window onto
  the smaller one. Both values are *correct for their frame*. Code that caches `S`, or
  that treats a change in `S` as drift to be suppressed, is wrong.

---

## 2. The three units

There are exactly three length units in play. Most of the confusion in this codebase
traced back to values that were numerically right but **labelled with the wrong unit**.

| unit | definition | who speaks it |
|---|---|---|
| **device px** | A real pixel on real glass. The one globally consistent space: monitors tile it without gaps or rescaling. Physically meaningful only once you know that panel's PPI. | `screen.dipToScreenRect()` (`pets.js:287-294`); `p.phys` / `p.work` / `px`; the Windows `EnumDisplaySettings` device rects; EDID native mode |
| **DIP** | `device / scaleFactor(d)` — a notional 96-PPI pixel. **Per-display**: each monitor has its own `scaleFactor`, so the DIP desktop is a *piecewise* scaled copy of the physical one and a DIP is a different physical length on each panel. | `screen.getAllDisplays().bounds` / `.workArea`; `BrowserWindow.getBounds()` / `setBounds()` (`pets.js:426-430`, `:512`, `:602`) |
| **renderer CSS px** | What the overlay page lays out in. One window, one grid, related to device px by **one** number `S`, including over the parts of the window on a differently-scaled monitor. | everything the payload calls "world"; `displays[]` entries; `floor`; `worldWidth` / `worldHeight`; every inline `style.width` the engine writes |

Conversions:

```
device(d) = dip(d) * scaleFactor(d)     # per-display, varies
dip(d)    = device(d) / scaleFactor(d)
css       = device / S                  # window-global, ONE S
device    = css * S
mm(d)     = device(d) / ppi(d) * 25.4   # the only physical truth
```

### The mislabelled comment

`app/electron/pets.js:434` reads *"Everything below is in the renderer's own CSS px"*
and sits directly above:

```js
deviceWidth:  px.width,     // pets.js:436
deviceHeight: px.height,    // pets.js:437
```

Those two are **device px**. On the measured desktop `deviceWidth` is 4480.8 device px
while the adjacent `worldWidth` is 2715.6 CSS px — a reader who trusts the comment
instead of the field name is off by a factor of **1.65**. The values are correct; the
label is not. Either move the comment below line 437 or rename the fields
(`frameDevicePx`). Until then, treat the field names as authoritative and the comment
as false for those two lines.

A second, latent unit mix-up lives in `dipRectToPhysical`'s no-intersection fallback
(`pets.js:347`): it returns the input **DIP** rect where every caller
(`pets.js:389`, `:399-405`, `:408-409`, `:436-437`) treats the result as device px and
divides it by `S` a second time. Reachable if the window is ever positioned entirely
off every display, e.g. a hot-unplug race.

**Rule:** when you touch any of these numbers, say the unit in the identifier or in a
comment on the same line. A bare `width` in this codebase is not self-describing, and
DIP and CSS px happen to coincide numerically whenever the window sits on the monitor
whose scale factor won the `windowScale()` contest — which makes the confusion
invisible exactly half the time.

---

## 3. The maths

Let `S` be the window's scale factor, `ppi(d)` the true pixel density of display `d`,
and `H0` a base authored size in CSS px understood as "correct at 96 PPI".

A sprite drawn `H_css` CSS px tall on display `d` measures
`mm = H_css * S / ppi(d) * 25.4`. Setting that equal to a target physical height and
solving for the multiplier on `H0`:

```
                ppi(d)
  M_phys(d) = ──────────            H_css(d) = H0 * M_phys(d)
               96 * S
```

`M_phys` is 1 exactly when `ppi(d) == 96 * S`, i.e. the single-monitor,
honest-EDID, no-user-override case. It contains `S` because the CSS grid is global, and
`ppi(d)` because the glass is not.

### The scaleFactor fallback

True PPI requires physical panel dimensions (EDID). When those are unavailable the only
density proxy is the OS scale factor, which encodes the user's *chosen* scaling rather
than true density: `ppi(d) ≈ 96 * scaleFactor(d)`. Substituting:

```
               scaleFactor(d)
  M_sf(d) =  ─────────────────
                     S
```

Scale each sprite by the ratio of its display's scale factor to the window's. This is
exact whenever Windows' scaling recommendation matches true density and approximately
right otherwise — a good default, because Windows picks `scaleFactor` precisely to
normalise apparent size, which is the same goal.

### The residual error, measured

For `H0 = 100` CSS px, `S = 1.10`, on the measured pair of panels:

| approach | ~102 PPI panel | ~167 PPI panel | spread |
|---|---|---|---|
| uncorrected | 27.39 mm | 16.73 mm | **63.7%** |
| `M_sf` (scaleFactor) | 27.39 mm | 25.10 mm | **9.2%** |
| `M_phys` (real PPI) | 26.46 mm | 26.46 mm | **0%** by construction |

The fallback recovers about 86% of the error using data Electron already hands you for
free. The residual 9.2% is entirely the gap between *chosen* scaling and *true*
density: honest scale factors for these panels would be `102/96 = 1.06` and
`167/96 = 1.74`, but the OS offers quantised 1.10 and 1.65 and the user may have nudged
them. Shipping `M_sf` alone would not be a defect.

**Do not mix the two across displays within one frame.** A pet crossing from a
PPI-derived panel to a scaleFactor-derived one would visibly jump. If *any* display
lacks credible EDID, use `M_sf` for **all** of them. Consistency beats accuracy: the
eye compares two pets to each other, never a pet to a ruler.

### K, the taste multiplier, must stay a separate factor forever

Equal physical size is not equal *apparent* size. The eye receives a visual angle,
`θ ≈ mm / viewing_distance`. A laptop panel sits ~500 mm away, a desk monitor ~700 mm,
so a pet that is physically 26.5 mm on both subtends roughly 40% less on the desk
monitor despite being dimensionally identical. That is not an error in the maths; it is
a missing second term, and viewing distance is not measurable. So it is exposed as a
human-set knob:

```
  H_css(d) = H0 * M_phys(d) * K(d)
                  ─────────   ────
                  computed    user taste, per display, persisted
```

`M_phys(d)` is never user-visible and never hand-tuned; it is recomputed from scratch on
every `display-metrics-changed`. `K(d)` defaults to **1.0**, clamps to **[0.5, 2.0]**,
and survives every display event untouched.

Four reasons they must never be fused into one stored number or folded into a global
size constant:

1. **Different domains.** `M_phys` is *derived* and volatile — it must be recomputed
   whenever a monitor is plugged, unplugged or rescaled, or the window migrates and `S`
   changes. `K` is *authored* and persistent. Fusing them means every display change
   either destroys the user's preference or freezes a stale scale factor into it.
2. **A global constant has one degree of freedom for an n-screen problem.** Lowering a
   single global target-PPI or sprite size to fix "too big on the left screen" also
   shrinks the right screen. The user then fixes the right screen, which re-breaks the
   left. There is no fixed point. `n` independent per-display knobs is the number of
   degrees of freedom the problem actually has.
3. **They fail differently.** `M_phys` degrades to a known-safe fallback when EDID lies.
   `K` is never in an unknown state. Fusing them lets a bad EDID read silently corrupt a
   user preference.
4. **Debuggability.** Separated, you can log
   `pet 210 css = base 100 x phys 1.58 x taste 1.33` and see immediately which half is
   wrong. Fused, you are guessing.

### Clamps and degenerate inputs

The rule everywhere: **an unknown display gets `M = 1.0`, never 0 and never unbounded.**
`M = 1.0` reproduces today's behaviour, which is imperfect but shipped and visible.

```js
const M_MIN = 0.35, M_MAX = 3.0
const clampM = (m) => (Number.isFinite(m) && m > 0) ? Math.min(M_MAX, Math.max(M_MIN, m)) : 1.0
```

Clamp the **product** too — `clamp(H0 * M * K, 16, 512)` CSS px — and log once when the
outer clamp fires. Below ~16 CSS px a pet is unclickable; above ~512 it is a full-screen
intrusion.

| case | detection | fallback |
|---|---|---|
| EDID missing | field absent | `M_sf` for **all** displays this frame |
| EDID reports 0 mm | `physW <= 0 \|\| physH <= 0` | treat as missing → `M_sf` (raw division would give `Infinity`) |
| non-finite anywhere | `!Number.isFinite(m)` | `M = 1.0` — `NaN` propagates through layout and yields an *invisible* element, the one outcome that must never happen |
| absurd PPI (<30 or >800) | range check before use | `M_sf` — a panel claiming 3 PPI is a cm/mm unit-confusion bug, not a monitor |
| `scaleFactor` 0 or missing | `!(sf > 0)` | `sf = 1` |
| `S` is 0 / NaN | `!(S > 0)` | `S = 1` and skip the physical correction entirely — if the shared denominator is wrong, do nothing rather than something arbitrary |
| pet on no display (dead space between mismatched monitors, mid-unplug) | no rect contains the pet's centre | nearest display by centre-to-rect distance; returning `null` forces every caller to invent a default |
| pet straddles a seam | centre picks one, edges another | size by the display under the pet's **centre**, and do not resize until the centre crosses — sizing by "most overlap" oscillates on the boundary |
| zero displays (mid-hotplug) | `displays.length === 0` | `M = 1.0`, keep the last known world, do not resize the window |
| unknown display for `K` | id not in the persisted map | `K = 1.0`; never inherit a neighbour's taste value |

When a pet's owning display does change, `M` changes discontinuously (0.97 → 1.58 on the
measured pair — a 64% jump). Animate over ~150 ms and anchor the interpolation at the
pet's **feet**: `y` is the feet and `floor` is the contact point, so scaling about the
centre makes the pet visibly sink into or hover above the taskbar for the duration.

---

## 4. The traps

This is the section that saves the most time. Each is a rule and the reason for it.

### 4.1 `clamp()` returns the MIDPOINT when `lo > hi`

```js
// engine.js:76
const clamp = (v, lo, hi) => (lo > hi ? (lo + hi) / 2 : v < lo ? lo : v > hi ? hi : v)
```

Every bound in the engine is written as `something ± this.w * k` or `top + this.h + k`.
**Growing a pet is precisely the operation that drives `lo` past `hi`.** A size increase
therefore does not raise an error anywhere — it silently teleports the pet to the middle
of an impossible range, every frame, forever.

Two confirmed infinite loops come from exactly this:

- **Permanent hop loop.** `roamBounds()` returns `y1 = top + this.h + 24` and
  `y2 = floor - 6` (`engine.js:589-592`). If the grown pet no longer fits the usable band
  (`h > (floor - top) - 30`) then `y1 > y2`, so `settleToFloor()`'s early return
  `if (this.y >= b.y1 - 1 && this.y <= b.y2 + 1) return` (`engine.js:611`) can **never**
  be satisfied. `settleToFloor()` is called on every `roam` frame (`engine.js:912`), so
  the pet hops, lands, hops, lands, forever, on the spot. It is alive, animating and
  permanently unusable.
- **Permanent turn-around loop.** The walk clamp uses `lo = p.x1 + this.w * 0.4`,
  `hi = p.x2 - this.w * 0.4` (`engine.js:1020-1023`). On a card narrower than `0.8 * w`,
  `lo > hi`, and the returned midpoint lies *past* `hi`, so the edge test fires on the
  same frame → `dir *= -1` → `state = 'idle'` → walk → turn. Every frame, on the spot,
  on a card the pet can no longer fit on.

`rand(lo, hi)` (`engine.js:90`) has no `lo > hi` guard at all, so
`moveTo()`'s `rand(p.x1 + this.w, p.x2 - this.w)` (`engine.js:312`) can place a recovered
pet **outside** the card it is being recovered onto.

**Rule:** at every size-derived bound, replace the implicit midpoint with an explicit
`lo > hi` branch that chooses the *visible, standable* end of the range — the floor, or
the card centre — never the midpoint of an impossible one. And cap the applied size
against the space available (`(floor - top) - 30` for a free pet, the card span for a
platform pet) *before* it is applied.

### 4.2 A measurement probe must declare per-monitor DPI awareness before measuring

Call `SetProcessDpiAwareness(2)` (per-monitor v2) as the first thing the probe does,
before any measurement API is touched.

This is not theoretical. On the measured machine, without the call,
`System.Windows.Forms.Screen` reported the ~167 PPI panel as **1707x1067** instead of its
real **2560x1600** — Windows virtualised the coordinates for the DPI-unaware process. A
probe that trusted that output would have **invented a 1.5x difference that does not
exist**, and any conclusion drawn from it would be a fiction built on a fiction.

EDID reads are the exception: the data is stored in the panel and is untouched by DPI
virtualisation, so an EDID probe does not need the call.

### 4.3 A screenshot CANNOT show a per-DPI physical-size bug

The sprite is the same number of CSS px on both panels, hence — via the single
window-wide `S` — the same number of **device** px on both panels. The captured pixels
are **identical by construction**. Screenshot both monitors, overlay them, diff them: the
sprite matches perfectly, and the bug is at its worst.

Only the physical size differs, and a screenshot has thrown away the physical size before
you ever look at it.

To see this bug you must either normalise the capture to physical size (scale each
monitor's capture by `1 / ppi(d)` before comparing) or measure the glass in millimetres.
Anything else is measuring the thing that is correct.

The same reasoning kills "it looks the same to me" as evidence, and kills any test that
compares rendered pixel buffers.

### 4.4 `display.id` is not stable across replug on Windows

Electron's `display.id` is an opaque Chromium value with no documented relationship to
any Windows identifier, and it changes across replug and reorder. **Never persist user
settings keyed on it** — a per-display `K` stored under `display.id` is silently
reassigned to the wrong monitor, or lost, the first time a cable is moved.

The durable join key is the monitor's **device-pixel origin on the virtual desktop**. It
is unique by construction (monitors cannot overlap in the desktop arrangement), available
on both the Electron and Windows sides, and survives replug:

1. For each Electron display: `ox = round(bounds.x * scaleFactor)`,
   `oy = round(bounds.y * scaleFactor)`.
2. For each Windows/EDID row: `(DeviceX, DeviceY)`.
3. Match on origin equality with a ±2 px tolerance. Origins are exact integers on the
   Windows side, so this is a bijection. On the measured desktop the origins matched
   exactly (`0,0` and `-1920,255`) while the *sizes* differed by 1–2 px purely from
   `round(DIP * scaleFactor)`.

Do **not** match on size alone — two identical monitors collide. `display.internal` is a
useful cross-check against EDID's internal-panel flag, but it cannot disambiguate two
external monitors, so it is corroboration and never the key.

For `K` specifically: key on the EDID device string or a hash of
`(bounds.width, bounds.height, scaleFactor, internal?)`, and fall back to `K = 1.0` on an
unrecognised display rather than inheriting a neighbour's value.

### 4.5 `dmLogPixels` is process-wide, not per-monitor

`EnumDisplaySettings`'s `dmLogPixels` returns the *process / system* logical DPI, not the
monitor's. On the measured desktop **both** rows reported `144` (150%) even though the two
panels run at visibly different effective scales. Deriving a per-monitor scale factor
from it produces two identical numbers and a "there is no difference" conclusion.

Take the per-display scale factor from Electron, or from `GetDpiForMonitor`, and read it
live — never cache it, and never carry it across a probe boundary. (On the measured
desktop Windows' Display Settings reported 150% for a panel Electron reported at 1.65;
whatever the cause, it is another reason the number must come from the API you are
actually rendering through.)

### 4.6 Read EDID millimetres from the preferred source mode, not the rounded centimetre field

`WmiMonitorBasicDisplayParams`'s `MaxHorizontalImageSize` / `MaxVerticalImageSize` are
**integer centimetres**. On the measured panels they report `39 x 24 cm` and `48 x 27 cm`.
Deriving PPI from those is wrong by up to 0.8%, and worse, it makes horizontal and
vertical PPI disagree by 1.6% on a **square-pixel panel** — a physically impossible result
that is the tell you used the wrong field.

The correct source is
`WmiMonitorListedSupportedSourceModes` →
`MonitorSourceModes[PreferredMonitorSourceModeIndex]`, whose `HorizontalImageSize` /
`VerticalImageSize` are **millimetres** straight out of the EDID detailed timing
descriptor. Keep the centimetre values as a cross-check and as a fallback only if the
preferred-mode read is unavailable.

### 4.7 Tightening a payload predicate turns a legacy payload into a SILENT no-op install

The display map passes through two hand-duplicated whole-entry predicates:

```js
// overlay.js:30-33  and, duplicated by hand, engine.js:1243-1244
d && Number.isFinite(d.x) && Number.isFinite(d.y)
  && d.width > 0 && d.height > 0 && Number.isFinite(d.floor)
```

They **drop whole display objects**; they never strip fields. And the install is gated:

```js
if (Array.isArray(cur?.displays) && cur.displays.some(usable)) {   // overlay.js:86
  world.setDisplays(cur.displays)                                  // overlay.js:87
}
```

If no entry is usable, `setDisplays` is **never called** and the previous map silently
persists. That gate is deliberate — it stops a payload-less frame from wiping a correct
map — but it means a fully-bad payload is a **no-op, not an error**.

So adding `&& Number.isFinite(d.sizeScale)` to either predicate makes every pre-existing
or mis-built payload vanish entirely, with no log anywhere, and the symptom is "nothing
happened" rather than a stack trace. **Default the new field; never gate on it.** Keep
both filters as `.filter()`, not `.map()` — identity-preserving filtering is the only
reason an added field survives the trip at all.

The same shape of silence appears at four more places, all of which will drop a new field
without complaint:

- `pets.js:397-406` — the `displays.map()` is an explicit six-field whitelist. A field not
  listed here does not exist downstream, and everything after it is dead code.
- `overlay.js:250` — rebuilds `{ floor: screen.floor }` as an object literal. Looks like a
  destructure; is a whitelist.
- `engine.js:1205` — `screenSpan`'s no-display fallback returns a bare `{ top, floor }`
  literal with no `id` and no extra fields. Any code reading `screenSpan(...).sizeScale`
  gets `undefined` here — on exactly the first frame, and over the dead rectangle between
  mismatched monitors.
- `overlay.js:216` — `entryScreen` falls back to `ds[0]`, an arbitrary display rather than
  the right one.

And the two duplicated predicates (`overlay.js:31-32`, `engine.js:1243-1244`) must be kept
in sync by hand. A divergence between them is a latent bug with no test guarding it.

### 4.8 Never pipe Electron's stdout to `head` while debugging

`head` closes the pipe as soon as it has its lines; the writer gets `SIGPIPE` and dies.
The app vanishes mid-startup with a truncated log and no error — **indistinguishable from
a crash**, and you will spend the next hour debugging a crash that never happened.

Redirect to a file and read the file:

```
electron . > run.log 2>&1        # then read run.log
```

The same applies to `| head` on any long-running child process launched while
investigating.

---

## 5. How to verify

### The harness

`tests/pets_engine_regression.mjs` runs on Node's built-in `node:test` +
`node:assert/strict` — no framework, no dependency beyond Node ≥ 18. From the repo root:

```
node tests/pets_engine_regression.mjs
```

TAP 13 to stdout, exit 0 on all pass. Because `app/package.json` has no
`"type": "module"`, the harness copies the engine's **bytes** to a temp `.mjs` and
imports that, rather than touching the engine. The environment is stubbed before the
import: a plain-object DOM, a fake `performance.now`, a `Map`-backed
`requestAnimationFrame` so "a frame" is a function call the test makes, and a seeded
`Math.random` so a failure reproduces every run.

`PETS_ENGINE=<path>` swaps in a different engine file. That is the **anti-vacuity
mechanism**: point it at the pre-fix engine and the new test must fail loudly. A test that
passes against both the old and the new engine is testing nothing.

### The gap this closes

`setDisplays` appears exactly twice in the existing suite: one 400x400 display at the
origin with a synthetic taskbar, and the empty list. **No existing test uses two displays,
two different scale factors, or a negative-origin display, and no existing test asserts
anything about a pet's size at all** — only its position. Every display-aware assertion in
the suite would pass identically on a single-monitor machine.

### The fixture must be in CSS px

Build the two-display fixture by reproducing what `overlayBounds()` actually emits:
device px divided by the single window scale `S`. **Not DIPs.** On the measured desktop
the external monitor is 1746 DIP wide and appears in the overlay as **1164.24 CSS px** —
"correcting" that back to 1746 reintroduces the exact DIP/CSS confusion the fixture exists
to catch.

Two details a fixture author must internalise:

- For whichever display won the `windowScale()` contest, CSS px happens to equal DIP
  numerically. For the other it does not. The coincidence hides the bug half the time.
- Adjacent display rects can **overlap by a fraction of a CSS px** (0.606 px on the
  measured pair, because the external monitor's device width is one pixel wider than its
  native panel). `displayAt()` resolves an exact hit by iteration order
  (`engine.js:1192`), so array order is load-bearing. Assert which display a point in that
  sliver resolves to, so a future change to it is deliberate rather than drift.

Assert the fixture itself before asserting anything interesting: both entries survived
`setDisplays`'s filter; the span invariant
`|max(d.x + d.width) - world.w| <= 0.5` holds (the same check as `pets.js:417`);
`displays[0].x === 0`; the two floors genuinely differ.

### The physical-parity test — and the trap in it

> **A test that asserts `petHeightOn(A) === petHeightOn(B)` in pixels is asserting the
> bug.**

Equal physical size on panels of different PPI **necessarily means unequal pixel size**.
That is not a subtlety; it is the definition. The pet is the same 42.9 device px on both
panels today, and that is exactly what is wrong. A well-meaning later change that
"fixes the test" by equalising pixels turns the suite green while restoring the defect.

So the test has two load-bearing halves:

1. **Assert the pixel heights are NOT equal**, with a message that says why — the low-DPI
   panel needs *fewer* CSS px, because each of its pixels covers more glass.
2. **Assert the ratio**, to a stated tolerance, against a named constant with a comment.

Name which invariant you are enforcing, because the two defensible ones disagree by ~9%
and a later reader will otherwise "fix" it in the wrong direction:

| invariant | ratio | secondary CSS height for a 26 CSS px reference | physical error |
|---|---|---|---|
| **D** — OS-intent parity (`scaleFactor` ratio) | 0.6667 | 17.333 | 9.1% |
| **P** — true physical parity (real PPI ratio) | 0.6108 | 15.880 | 0% |
| *today's engine* | 1.0000 | 26.000 | **63.7%** |

**Enforce D, and record P's number in a comment as a documented non-goal.** `scaleFactor`
is a value the renderer can actually *receive* — Electron reports it — whereas real PPI
requires EDID physical-dimension data the renderer never sees. A test demanding P would be
unimplementable against any plausible fix. Recording P keeps the residual 9% a known,
deliberate discrepancy rather than an undiscovered one.

Express the claim a third time in millimetres — `mm = cssPx * S / ppi * 25.4` — and assert
the two agree within 10%. The ratio arithmetic may get refactored; the intent survives if
it is also stated in the unit a ruler measures.

### The supporting assertions

- **No stretch.** `w` and `h` share one `k`, so `wLo/wHi` must equal `hLo/hHi` to within
  floating-point noise.
- **Continuity across the seam.** Sample size at several x positions either side of the
  seam and require monotonic change with no discontinuity — otherwise the pet visibly pops
  as it crosses.
- **Minimum-size floors survive on the low-DPI side.** Shrinking for the lower-PPI panel
  must not drive the canvas to 0px and make the pet unclickable;
  `layoutClip`'s `Math.max(6, ...)` on the character box and `Math.max(1, ...)` on the
  canvas (`engine.js:217-218`, `:226-227`) must still hold.
- **The destination monitor's floor is respected after a resize.** A pet whose height
  changed at the seam must rest on the floor of the screen it arrived on, not the one it
  left.

### A runtime invariant worth keeping

In the same spirit as the span check at `pets.js:417-423`:

```js
// every pet, on every display, occupies the same physical height ± 1 mm
const mm = (css, d) => css * S / ppi(d) * 25.4
console.assert(Math.abs(mm(H0 * M(a), a) - mm(H0 * M(b), b)) < 1.0)
```

This fires the instant someone reintroduces a global size constant — which is the failure
mode this whole document exists to prevent.
