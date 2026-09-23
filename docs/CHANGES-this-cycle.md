# Changes this cycle

An engineering summary of the uncommitted change set in this working tree. Nothing here
is committed or pushed.

**This revision extends an earlier one.** That earlier text described the tree at
**29 tracked files changed, 5387 insertions, 369 deletions**. Since then the per-display
physical-sizing work landed, and the counts are now **29 tracked files changed, 6586
insertions, 448 deletions**, plus **four** untracked files:

| untracked file | lines | what it is |
|---|---|---|
| `app/electron/ppi.js` | 465 | new main-process module: real panel PPI, read from EDID |
| `docs/multi-monitor-sizing.md` | 565 | new design document for per-display sizing, and its traps |
| `tests/pets_engine_regression.mjs` | 1085 | the engine harness, grown from 12 tests to 28 |
| `docs/CHANGES-this-cycle.md` | — | this document |

Two honesty notes about those numbers. First, `git diff --shortstat` was read twice while
writing this and returned **6569** and then **6586** insertions a few minutes apart: other
work is being edited in this tree concurrently, so the totals are a snapshot of a moving
target rather than a closed ledger. Second, `tests/ai_credsources_regression.py` is
counted among the 29 but has **no content change at all** — its only difference is a file
mode change, `100755` to `100644`. See open item 8.

New since the earlier revision: **[§9, pets were a different physical size on each
monitor](#9-pets-were-a-different-physical-size-on-each-monitor)**; new entries in
[§10](#10-smaller-fixes-made-along-the-way) and
[§11](#11-findings-that-cost-real-time-and-will-again); a re-run
[Verification](#verification) table; and open items **10–13**, which include the parts of
§9 that are built but **not reachable from the UI**.

It is organised by **symptom** — what you saw go wrong — rather than by file, because
the fix for one symptom is usually spread over several files and the file list on its
own tells you nothing about which of your problems it solved.

Every claim below was checked against the actual diff. Where something is unfixed,
partially fixed, or unverified, it says so in [Open items](#open-items) rather than
being quietly omitted.

---

## 1. Pets vanished when the widget was small

**Symptom.** Shrink the widget below roughly 92% zoom and the pets disappear, or get
sliced in half at the window edge. A cold start at the same small size was always
fine — only *crossing* the threshold broke it.

**Root cause.** Three separate defects stacked on the same threshold.

The widget suspends its frosted window panes below `FROST_MIN_SCALE = 0.92` — at that
size the OS cannot draw a pane that small, so the cards paint their own background
instead. That latch was also switching off the *measurement pass* that tells the pet
engine where each card is. `app/src/panes.jsx` gated `schedule()` on a combined
"stopped" predicate that included the frost latch, so under the latch no measurement
was requested at all. The engine kept the card rectangles it had measured at the
instant of crossing, while the window went on shrinking, and pets pinned to cards that
no longer existed at that size ended up outside the window, where `overflow: hidden`
on `.nw-pets` cut them.

`app/src/App.jsx` had a second, independent latch defect. `resumeFrost()` skipped
`resume()` whenever the current scale was below the floor. But `resume()` lifts the
*animation* hold, which is a different mechanism from the frost latch — the animation
hold stops the measurement pass outright, where the frost latch only silences the IPC
to the main process. A resize drag that ended below 0.92 therefore left the animation
hold latched with nothing able to lift it.

Third, the engine clamped pets only against *card* spans, never against the world box.
A card span is a measurement that can go stale; the world box cannot.

**What changed.**

- `app/src/panes.jsx` — `schedule()` now gates on the animation hold alone (`held`),
  and passes `frosted.current` as the `quiet` flag to `flush()`. The latch now means
  only what it always claimed to mean: stop telling the main process. The pass runs,
  subscribers get their rectangles, and nothing is sent over IPC.
- `app/src/App.jsx` — `resumeFrost()` calls `resume()` unconditionally. The
  `frostRef` that carried the old guard is deleted. A third `usePaneSync(resizing)`
  was added: a zoom change moves no layout-unit box, so neither the per-card observers
  nor a window `resize` event can be relied on to fire after the hold is released.
- `app/src/pets/engine.js` — new `Pet.clampToWorld()`, now called at **11 sites**
  (it was 9 when this section was first written; §9 added two more): `snap()`, the drag
  handler, the roam-bounds branch, the floor-settle branch (`h - 8` is shallower than
  the sprite once the widget is small enough), mid-hop, hop landing, the chase branch,
  after the roam turn, unconditionally in `World.setSize()` after either the free-mode
  or `snap()` branch, and at the end of the new `applyDisplaySize()`.
- `clamp()` itself was fixed for inverted bounds: when `lo > hi` (a world narrower than
  the sprite) it now returns the midpoint instead of an arbitrary edge.
- `app/src/pets/PetLayer.jsx` — the world box is now measured from the layer's own
  border box via `ResizeObserver`, not from `window.innerWidth`/`innerHeight`. Measured
  in Chromium at a 300x300 viewport with the document overflowing, `innerHeight`
  reported 300 while the layer measured 285; that 15px strip is world the engine would
  place a pet into and the clip would then slice. Both the observer and the `resize`
  event are kept (neither covers the other) and they share one rAF coalescer.
- `app/src/index.css` — a comment block on `.nw-pets` explaining why `inset: 0` is
  load-bearing and must not be inset to match the shell gutter: the sprites are
  absolutely positioned children of that box while the platforms and cursor are in
  viewport coordinates, so the two agree only while the box origin *is* the viewport
  origin. Insetting also removes the strip a pet at a card's edge is meant to overhang
  into, clipping sooner rather than later.

**Verified by.** `tests/pets_engine_regression.mjs` — including "a pet outside a
shrunken world is pulled back in" and "a very small pet still has a non-zero canvas".
Those tests still pass in the 28-test suite; see [Verification](#verification).

---

## 2. Sprites were squashed horizontally, by up to 74%

**Symptom.** Pets in the widget looked compressed — too narrow for their height. The
*same* pet on the desktop overlay looked correct.

**Root cause.** Tailwind's preflight sets `max-width: 100%` on every `img`. The widget
document loads preflight; the overlay document does not. The engine sizes the sprite by
writing `width` and `height` inline from one scale factor, so the capped width squashed
the art horizontally while the inline height stood. Worse than cosmetic: the character
box and anchor the engine computed alongside those numbers no longer matched what was
drawn, so a pet's feet sat off the card and its grab target sat off the pet.

**What changed.** `app/src/pets/pets.css` — `max-width: none` on `.pet-sprite`, with the
measurement recorded in the comment: the widest canvas in `metrics.json` (morph, 100px
canvas per 24px character) at a 380px window, size 50 at 4x, drew 380px wide instead of
750px — a 49% squash — while the overlay drew the same pet unsquashed.

**Verified by.** Reasoning from the measurement in the comment and the one-line CSS
change. **Not** verified on a running desktop — see [Open items](#open-items).

---

## 3. Pets never appeared at all, with nothing explaining why

**Symptom.** The Pets card showed a greyed-out "no sprites found" button. No pets, no
reason, no way to retry short of restarting.

**Root cause.** An empty array is truthy in JavaScript. `app/electron/pets.js` cached
the built manifest in `let manifest = null` and rebuilt only `if (!manifest)`. A build
that read back empty once — a folder momentarily unreadable, a pack still unpacking, a
disk that answered late — was cached as `[]` for the life of the process. `[]` is not
`null`, so every retry the renderer scheduled and every press of a Retry button was
answered from that same empty array, forever.

The renderer had the mirror-image defect: `app/src/pets/store.jsx` asked once and
swallowed a rejection entirely (`.catch(() => {})`), leaving the manifest empty for the
session with nothing said anywhere. And `useOverlaySync` gated its payload on `ready`
(a *usable* catalogue), so one failed manifest read meant the overlay was never told
anything — including that it should close.

**What changed.**

- `app/electron/pets.js` — only a manifest with species in it is cached. Failure is
  rate-limited instead, by `BUILD_RETRY_FLOOR_MS = 1500`, so a desktop with genuinely
  no sprite pack does not rescan the asset tree on every ask, while any retry past the
  renderer's first one (backoff 400/1200/3000/8000 ms) still reaches the disk. A throw
  from `buildManifest()` is caught and reported as a retryable miss rather than thrown
  across IPC.
- `app/src/pets/store.jsx` — bounded retry schedule `[400, 1200, 3000, 8000]`, then it
  settles with a named error. New state: `manifestError`, `manifestSettled`,
  `missingSpecies`, `retryManifest`. An empty list is treated as fatal in the same way
  as a rejection. `useOverlaySync` now gates on `manifestSettled` (the question being
  *answered*) rather than on `ready` (the answer being *good*).
- `app/src/components/Pets.jsx` — the disabled button is replaced outright by a
  `CatalogueNotice` that distinguishes "still looking" (wait) from "settled with a
  reason" (press **Look again**), and a `MissingSpeciesNotice` that names, one by one,
  the roster pets whose artwork is not in the catalogue. Those rows are kept, not
  pruned — putting the sprite pack back restores them with the same name and size.
- `app/src/pets/PetLayer.jsx` — unresolvable pets are surfaced as an on-screen
  `role="status"` banner instead of silently skipped, `w.add()` is wrapped in
  `try/catch` so one malformed pet cannot abort the whole roster effect, and the
  removal pass was moved outside that guard so a broken pet cannot take pets the user
  just deleted down with it. `keep.add(row.id)` moved after the resolve guard — it was
  marking unresolvable pets as present to the removal pass when they had never been
  added.

**Verified by.** Code reading of the diff. No automated test covers the manifest cache
path — see [Open items](#open-items).

---

## 4. Pets were invisible or misplaced on a multi-monitor desktop

**Symptom.** Desktop ("Screen" mode) pets did not show up, or stood in the wrong place,
on a two-monitor setup — in this case a secondary monitor at a **negative** x
coordinate with a **different scale factor** from the primary.

**Root cause.** A unit mismatch that no single constant can reconcile. Electron measures
displays in DIPs; the renderer lays out in CSS px. On a mixed-DPI desktop each monitor
divides its own device pixels by its own `scaleFactor`, so the DIP desktop is a
*piecewise* scaled copy of the physical one — while the overlay is one window with
exactly one CSS px, namely the device pixel divided by the scale of the monitor Windows
associated the window with. `overlayBounds()` was handing DIP rectangles to a page
measuring in CSS px, which put `displays` hundreds of pixels outside the viewport.

Two secondary defects: nothing re-pushed geometry when the *widget* merely walked onto
another monitor (`display-metrics-changed` / `added` / `removed` do not fire for that),
and `fit(undefined)` in the overlay wiped an already-correct display map.

**What changed.**

- `app/electron/pets.js` — everything routes through device pixels, the one space both
  ends agree on, and divides by the window's own scale only at the very end.
  New `physOf()`, `panelsOf()`, `dipRectToPhysical()` (cuts a rectangle against each
  display, converts each piece with that display's own mapping, clamps to the panel's
  device rect so shared edges stay exact — 1707 DIP x 1.5 is 2560.5, and half a pixel
  at the seam is how a pet falls between screens), and `windowScale()` (Windows applies
  the scale of the monitor a window overlaps most *in device pixels* to the whole
  window). The payload now carries `union` (DIPs, the rectangle to *ask* for) separately
  from the frame it was actually granted, plus `scaleFactor`, `deviceWidth/Height`,
  `worldWidth/Height` and per-display `floor`, all in renderer CSS px. A self-check logs
  when the display span and world width disagree by more than 0.5px.
- `resyncOverlay()` rebuilds the payload from `getBounds()` *after* the fact, because
  Windows clamps and the rectangle asked for is not the rectangle granted. An
  `overlay.on('move')` handler (debounced 140ms) covers the case where Windows
  re-associates the window with a monitor of a different scale — nothing announces that,
  and no `display-*` event fires for it.
- `app/electron/main.js` — `checkDisplay()` now pushes overlay geometry too, through the
  public accessors `pets.overlayWindow()` / `pets.overlayBounds()`.
- `app/src/pets/overlay.js` — new `worldBox()` takes the world size from the union of
  the *display array the engine hit-tests against*, never `window.innerWidth`. `fit()`
  only ever *installs* a display map and never clears one. `requestBounds()` retries
  with backoff (250ms doubling to 4000ms) so a failed first fetch cannot latch. New
  arrivals walk on from the edge of a real screen via `entryScreen()` — between two
  monitors of different heights the bounding box contains a strip that belongs to no
  display, and `world.w - 24` is often inside it, where nothing is drawn.
- `app/electron/overlay-preload.js` — documented as a pure passthrough: nothing on this
  side may scale, offset or origin-shift the payload, because re-deriving anything here
  is how the two ends stop agreeing.

**Verified by.** Code reading and the in-process span-vs-world-width assertion. §9 later
added a two-display, two-scale-factor, negative-origin fixture to the engine suite, which
covers the *renderer's* half of this arithmetic. The Electron half is still untested.
**Not** verified on the real two-monitor desktop — see [Open items](#open-items).

---

## 5. Adding a second Claude account silently reconnected the first

**Symptom.** Sign in to add a second Claude account; the widget adds a duplicate row for
the account you already had, or appears to succeed and nothing new shows up.

**Root cause.** A Claude credential file contains **no account identifier at all**. The
vault's fingerprint was therefore `subscriptionType + expiresAt` — and `expiresAt` is
rewritten on *every* token issue, including the silent refresh the CLI performs on its
own. That makes the fingerprint a timestamp wearing an identity's clothes:

- the duplicate check never fired for Claude, so signing in as the same account twice
  wrote a second row;
- a tombstone keyed on it went stale the first time the token renewed;
- the re-authentication guard in `sidecar.py` could never fire, because a positive match
  was unreachable — it was inert for the very provider it was written for.

**What changed.** Durable identity from `oauthAccount.accountUuid`, which Claude Code
stores in its own state file `~/.claude.json` — **not** in `.claude/.credentials.json`.
This is a local file read; there is no network call anywhere in this path.

- `aiproviders.py` — new section: `cred_identity()`, `cred_fingerprint()`,
  `is_durable_fingerprint()`, `cred_fingerprint_candidates()`,
  `cred_fingerprint_matches()`, plus `_claude_state_paths()`, `_read_claude_state()`
  (memoised on path + mtime + size, keeping only the two id fields — never the project
  history the rest of that file contains), `_email_digest()` and `_claude_legacy_fp()`.
  Durable form is `claude:acct:<uuid>`; the email fallback is `claude:email:<16 hex>`, a
  truncated SHA-256 of the lowercased address — the address itself never leaves the
  function, because only equality is ever needed.
- The back-compatibility mechanism is a **matcher, not a migration**. A credential
  yields `["claude:acct:<uuid>", "claude:<tier>:<expiresAt>"]` — always a superset of
  what the old scheme produced — so nothing already written to a vault row or a
  tombstone can be orphaned. `_claude_legacy_fp()` reproduces the old string byte for
  byte for exactly that reason.
- `aiaccounts.py` — `cred_fingerprint()` gains an optional `cred_path` (additive; the
  existing two-argument callers keep working), `_legacy_fingerprint()` preserved as the
  fallback, plus `is_durable_fingerprint()`, `cred_fingerprint_candidates()`,
  `_stored_fingerprints()`, `_account_fingerprints()` and `_stamp_fingerprints()`.
  `import_from_cli()`'s `known` set became a per-provider dict of full candidate sets, so
  a Claude fingerprint can never be compared against a Codex one.
- `sidecar.py` — `_ident_fn()` (binds by name at call time, vault first then
  `aiproviders`), `_fp_candidates()`, `_capture_identity()`, `_fp_owners()`,
  `_reauth_conflict()`, `_login_refused()`. `isolated_login()` resolves identity **inside
  the sandbox**, against the sandbox's own `.claude.json`, because that is the only
  moment the answer is knowable — teardown deletes it seconds later, and a shared home
  starts answering for whichever account signs in next. The uuid is then stapled onto
  the credential so the vault row is self-describing.
- The re-auth guard now refuses only on a **positive match against a different row**,
  never on a mere difference. That direction is deliberate: every sign-in mints a new
  token, so refusing on a difference would block every legitimate re-authentication. The
  refusal matters because `update_cred` replaces a row's credential wholesale and the
  provider kills the old refresh token as soon as it issues a new one — writing the
  wrong credential would have destroyed the account being repaired.

**Verified by.** `tests/ai_provider_adapters_regression.py`, 43/43. Test 33 is the
named regression: `expiresAt` 1800000000000 -> 1900000000000 moves the legacy
fingerprint while the durable one holds at
`claude:acct:11111111-2222-3333-4444-555555555555`. Test 40 shows the Windows and WSL
copies of one account, with two different expiries, collapsing to one identity.

---

## 6. Deleted accounts came back on their own

**Symptom.** Remove an account; it reappears at the next launch.

**Root cause.** `remove_account()` deleted the vault row and nothing else. The CLI
credential file was still sitting on disk, so the next `import_from_cli()` added it
straight back. The global "already migrated" latch (`_migrated_already()`) was not
protection: both of its witnesses — the in-vault stamp and the marker file — genuinely
go away on a reinstall, a vault reset, or a user clearing `%APPDATA%`.

A second bug compounded it: `aiaccounts._coerce()` returned exactly
`{version, accounts}` and dropped every other top-level key, which ate
`core._mark_migrated()`'s `imported_from_cli` stamp on the very next `save()`. That
stamp has never actually persisted in a live vault; the marker file was doing all the
work.

**What changed.**

- `aiaccounts.py` — tombstones in a **sibling file**, `accounts.json.forgotten`, derived
  from `accounts_file()` so `AI_ACCOUNTS_FILE` redirects it too. Deliberately not a key
  inside the vault: deleting or resetting `accounts.json` is exactly the moment this
  record has to keep working. New `forget_account()` (returns True only once the row is
  confirmed gone *and* the tombstone confirmed written), `_add_tombstone()`,
  `_load_tombstones()`, `_save_tombstones()` (atomic, 0600), `tombstone_match()`,
  `is_tombstoned()`, `list_forgotten()`, `allow_reimport()`. `remove_account()` is kept
  as a compatibility alias so every existing call site gets the fix untouched.
- Three match keys, in descending order of trust: a **durable** fingerprint (survives
  every refresh), a **legacy** fingerprint (still honoured, or every account removed
  before this change would be un-blocked), and the normalised **cred_path** as a
  backstop for credentials that cannot be identified at all. The path key keeps its
  known false-positive direction — a genuinely different login that later occupies the
  same file stays blocked until `allow_reimport()`. That trade-off is deliberate: a
  false "still forgotten" costs one click, a silent resurrection costs trust that delete
  means delete.
- `_coerce()` now carries `imported_from_cli` through an explicit **allowlist** —
  unknown keys still get dropped, so a corrupt or hostile document still cannot smuggle
  an arbitrary key through. `forgotten` is deliberately *not* on that allowlist.
- `core.py` — `_tombstoned()` wrapper that defaults to **True on failure** (wrongly
  skipping costs an account the user can re-add; wrongly importing hands back an account
  they deleted, at every launch). `_import_sweep()` adds rows through `add_account()`
  directly and so never passes through `import_from_cli()`'s check — it now makes the
  check itself. `ai_login_capture()` calls `_allow_reimport()` after a deliberate manual
  sign-in, because logging in again at the same path is the newer statement.

**Verified by.** `tests/ai_accounts_regression.py`, 28 passed / 0 failed / 1 skipped.
Test 21 wipes the whole vault and shows the tombstone surviving; test 24 drifts the
Claude fingerprint and shows it still tombstoned; test 25 covers the honest
false-positive direction (a different account at a shared path stays blocked until
undo). The one skip is `08b vault file permissions are restrictive` — POSIX mode bits
are not enforced on Windows, so the 0600 open path was checked by inspection only.

---

## 7. The sign-in sandbox looked right and was silently not working

**Symptom.** The isolated-login sandbox appeared correct in the code, but the CLI it
launched still behaved as though it had a home and a session.

**Root cause.** Two things living downstream of anything a Windows parent process can
reach.

`WSLENV` carries `CLAUDE_CONFIG_DIR` and the `XDG_*` variables across the interop
boundary correctly — but **not `HOME`**. WSL's interop layer resets `HOME` from
`/etc/passwd` on the far side regardless of `HOME/p`, in both login and non-login
shells. So the sandbox's single most important isolation variable was being dropped on
every `wsl.exe` launch.

And `bash -lic` sources `~/.bashrc` *inside* the distro, which re-creates
`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` /
`CLAUDE_CODE_OAUTH_TOKEN`. A parent-side env dict never contained them, so there was
nothing there to delete — and a CLI that believes it is already authenticated against a
proxy never opens a browser, making the whole sign-in a silent no-op.

Separately, a bare `claude` invocation opens the interactive REPL; `/login` is a slash
command typed *inside* that REPL. Nothing typed it, so a bare invocation never performed
an OAuth flow at all.

**What changed.** `ailogin.py`:

- `create_sandbox()` computes `wsl_root` **once**, via `wsl.exe wslpath -a -u`, from the
  *untranslated* Windows root. Calling `wslpath` on a value `WSLENV`'s own `/p` flag has
  already translated reproduces the doubled `/mnt/c/mnt/c/...` artifact seen during
  diagnosis, so this is the single source of truth.
- New `_wrap_wsl_login_command()` rewrites a `wsl.exe ... bash -c<flags> "<cmd>"` argv
  so the in-distro command itself carries `unset <vars>; env HOME=<wsl_root> <cmd>`.
  Both fixes have to be text baked into the command bash runs, executed *after* the rc
  file's own exports — that is the only point at which either is possible. Any argv
  shape it does not recognise is returned unchanged, so this only ever adds isolation.
- New per-provider `unset_vars` and `login_suffix` fields, each overridable from `.env`
  (`AI_LOGIN_UNSET_VARS_<PROV>`, semicolon separated; `AI_LOGIN_SUBCOMMAND_<PROV>`,
  space separated). `claude auth login` was confirmed against the installed CLI's own
  `--help`. The suffix is appended only when the command does not already end in it. The
  Codex unset list is flagged in the code as an unproven guess, not confirmed against a
  live Codex CLI.
- `launch_login()` uses `tempfile.gettempdir()` as cwd rather than `sandbox.dir`:
  Windows refuses to delete a directory that is a live process's working directory, and
  on the timeout path the CLI is by definition still running when teardown tries. Only
  the production path (argv from `.env`) is rewritten; a caller-supplied argv is trusted
  verbatim, which is what lets the self-test prove the plumbing without a vendor CLI.

**Verified by.** `tests/ai_login_sandbox_regression.py`, 44/44.

---

## 8. A dead credential copy shadowed the live one

**Symptom.** "Login expired", prompting a re-login, for a login that was perfectly
healthy.

**Root cause.** The same login exists twice on this machine — the Windows profile and a
WSL home — and `cli_cred_paths()` returns both. When a CLI logs out it does not delete
its credential file, it **guts** it: `expiresAt` drops to 0 and the refresh token
disappears, leaving a perfectly readable, perfectly parseable, perfectly dead document.
`_read_cli()` took the first readable file, which is correct only while every candidate
is readable. Stop the WSL distro and the live copy becomes unreachable, the dead
leftover is the only survivor, it is silently promoted to "the credential", and the
refresh that follows fails with a 400 shown to the user as an expired login. The real
fault was an *unreadable* file.

**What changed.** `aicredsources.py` — candidates are **aged out**, not merely ranked.
New per-candidate verdicts `CAND_LIVE` / `CAND_STALE` / `CAND_UNKNOWN_AGE` /
`CAND_UNREADABLE` / `CAND_UNREACHABLE` / `CAND_ABSENT` — six states rather than the two
that let the bug through — plus `R_STALE` as a reason prefix distinct from
`R_UNREADABLE`, because those two demand opposite actions from the user.

`_cli_path_reachable()` is the load-bearing distinction: `os.path.exists()` on a
credential inside a stopped WSL distro returns False, exactly as it does for a machine
where the user never logged in there. The tell is ancestry — if either the vendor
directory or the home above it is present, we truly did look; if neither is, the honest
answer is "cannot tell", not "no".

Selection order: a live candidate wins (freshest first, ties broken by candidate order
so an explicit `.env` path still beats the default); then an unknown-age readable file
(refusing it would break every future credential format); then **unreadable is
reported**, naming the path, explicitly stating "this is not an expired login" and
noting that a stale copy exists and is deliberately not used; only when every copy is
readable and every copy is past saving is "log in again" the honest advice.
`CRED_MAX_STALE = 16 * 86400`, re-read from `aiproviders` at call time so the three
copies of that number cannot drift. `describe()` and `scan_all()` now report
`candidates` and attribute `source` to the candidate that actually answered — a stale
leftover is never named as the source.

**Verified by.** `tests/ai_credsources_regression.py`, 55/55, plus eight new checks in
the module's own `_selftest()` built directly against `_cli_candidate()`.

---

## 9. Pets were a different physical size on each monitor

**Symptom.** A desktop pet that looks right on the external monitor is visibly a
different *object* on the laptop panel — noticeably smaller, even though nothing in the
settings changed and both screens are running at scales Windows picked.

This is the symptom everything in [`docs/multi-monitor-sizing.md`](multi-monitor-sizing.md)
exists to explain, and it is the one you cannot screenshot. See
[§11](#11-findings-that-cost-real-time-and-will-again).

**Root cause.** The overlay is **one** `BrowserWindow` stretched across the whole
desktop, and a browser window has exactly **one** CSS pixel grid. Windows gives that
window the scale factor of the monitor it overlaps most — call it `S` — and applies it
to the entire window, including the half hanging over a differently-scaled screen. The
sprite size chain had no per-display term in it anywhere:

```
target = opts.size * world.scale * species.scale * pet.sizeFactor
k      = target / variant.baseH
img.style.width/height = canvas_metrics * k
```

So a pet was the same number of CSS px — hence, through the single `S`, the same number
of *device* px — on every panel. Device pixels are not a physical unit. On the measured
pair of panels (~102 PPI external, ~167 PPI laptop) at `S = 1.10`, a 100 CSS px sprite is
110 device px on both and therefore **27.39 mm** on one and **16.73 mm** on the other:
**63.7% taller on one monitor than the other**.

Geometry had already been fixed for this (§4); size never had. The two are separate
problems with the same cause, and fixing the rectangles made the size defect *more*
visible, not less.

**What changed.**

**A new main-process module, `app/electron/ppi.js` (465 lines, untracked).** Real
physical panel density, from the display's EDID, because `scaleFactor` is a *user
preference* and not a measurement — two panels at 167 and 102 real PPI can both be set
to 150%.

- One PowerShell child per probe, launched with `-EncodedCommand` (the script contains
  quotes, braces, dollars and a here-string; every intermediate layer would otherwise
  get a chance to mangle it), `-NoProfile -NonInteractive`, `windowsHide`.
- The EDID read and the desktop-geometry read happen in **one** process, because
  splitting them would let the monitor arrangement change between the two calls.
- `SetProcessDpiAwareness(2)` is the first thing the script does. Without it Windows
  virtualises coordinates for a DPI-unaware process and the probe *invents* a scale
  difference that does not exist — measured: a 2560x1600 panel reported as 1707x1067.
- Millimetres come from `WmiMonitorListedSupportedSourceModes` →
  `MonitorSourceModes[PreferredMonitorSourceModeIndex]`, which is the EDID detailed
  timing descriptor in **millimetres**. `WmiMonitorBasicDisplayParams` is kept only as a
  coarse fallback: it is integer **centimetres**, and deriving PPI from it makes
  horizontal and vertical PPI disagree by 1.6% on a provably square-pixel panel.
- The join key is the monitor's **device-pixel origin on the virtual desktop**, never
  `display.id` — an opaque Chromium value that changes on replug, so a cache keyed on it
  goes stale exactly when the user rearranges screens. `[NullString]::Value` is used for
  `EnumDisplayDevices`, not `$null`, because PowerShell marshals `$null` as an empty
  string and the call then returns no devices at all.
- On the Electron side the origin comes from `screen.dipToScreenRect()`, not from
  `bounds.x * scaleFactor`. Chromium accumulates a secondary's DIP offset through its
  *neighbours'* scale factors, so the multiply is wrong: measured, a panel really at
  device `(-1920, 255)` computed to `(-1921, 169)` — the y **eighty-six pixels** out,
  far past any tolerance, and it would have silently matched the wrong monitor. The
  multiply survives only as a fallback for running this file under plain node.
- Contract with the rest of the app: **`ppiFor()` returns 0 for "unknown" and never
  throws**, because it is read while assembling the overlay payload and an exception
  there takes the overlay down. `MIN_PPI = 30` / `MAX_PPI = 800` reject a projector
  reporting 0 mm or a VM inventing a 1 mm panel. Origin match has a ±2px tolerance (the
  two sides round independently); a unique device-pixel **size** match is the last
  resort, and two identical monitors deliberately stay ambiguous and report unknown
  rather than guess wrong by a factor of 1.64.
- `refresh()` collapses overlapping calls into one trailing re-probe (Windows fires
  `display-metrics-changed` several times per settings change), a failed probe keeps the
  previous good cache, the 20s timeout timer is `unref`'d, and `complainOnce()` caps the
  noise at one line per process. Requiring the module can never fail.

**`app/electron/pets.js`** — `require('./ppi')` inside a `try/catch` with a null resolver
fallback, `ppiOf()` normalising anything non-finite to 0, and two new per-display fields
on every payload entry: `sf` (that display's own `scaleFactor`) and `ppi` (real, or 0).
The comment mislabelling `deviceWidth`/`deviceHeight` as CSS px was corrected — they are
device px, and on the measured desktop `deviceWidth` is 4480.8 while the adjacent
`worldWidth` is 2715.6, a factor of 1.65 apart. `dipRectToPhysical()`'s no-intersection
fallback no longer returns the input DIP rect (every caller divides the result by `S`
again, so that path double-divided silently); it converts through the nearest panel's
mapping instead, approximate but in the unit the callers are owed.

**`app/src/pets/engine.js`** — the size chain gained two factors, kept permanently apart:

- `setWindowScale()` / `windowScaleOf()`. When the host has not said, the fallback is the
  largest `sf` in the display map, which degrades to 1 on a payload that predates the
  field and makes the whole correction a no-op rather than a wrong answer.
- `displayScale(d)` — **`M`**, the measurement. `ppi / (96 * S)` when EDID is known,
  else `sf / S`, else exactly 1. Clamped to `[0.25, 4]`. Anything unusable returns 1,
  which is the old behaviour.
- `displayTaste(d)` — **`K`**, the opinion, read from `d.k`, clamped to `[0.5, 2]`,
  default 1.
- `applySize()` multiplies by `M * K` and stores `dispM`/`dispK` on the pet. They are
  never fused into one number: `M` is derived and volatile (recomputed on every display
  change), `K` is authored and persistent, and fusing them means a display change either
  destroys the preference or freezes a stale scale factor into it.
- `governingDisplay()` — a **deadband**, `max(8, w * 0.5)`. A pet is only re-sized once
  its centre is properly inside a new screen, further in than the clamp could push it
  back out; otherwise a pet on a seam oscillates between two sizes and `layoutClip()`
  writes the DOM every frame of it. A screen narrower than the deadband is exempt, or a
  pet could never adopt it at all. A point over *no* display keeps the last good screen.
- `applyDisplaySize()` — the only place a live pet's size changes because of where it
  is, with a documented, non-commuting order: refuse while `held`/`air`/`hop` (each owns
  the pet's geometry outright), resolve the screen once, skip when neither factor moved
  by more than 0.5% (`layoutClip` has no dirty check, so a standing pet would otherwise
  write six style properties every frame), resize through `applySize()` (never through
  `k`/`w`/`h`, because `render` reads the shadow's size back out of its own inline CSS),
  drop a `target` chosen at the old size, re-snap or re-settle, then `clampToWorld()`
  last because the world box is the bound that cannot go stale.
- `setDisplays(list, windowScale)` takes the window scale alongside the map and
  re-applies size to every pet, including platform pets — a monitor swapped under a card
  changes the density its pet stands at even though the card never moved. The entry
  filter is **deliberately not** tightened to require `sf` or `ppi`; see the finding
  below about what tightening it would have done.

**`app/src/pets/overlay.js`** — pushes `setWindowScale()` *before* the display map, so a
recompute triggered by the map already sees the matching denominator. `entryScreen()`
returns the display entry itself rather than a rebuilt `{ floor }` literal, which had
been stripping every per-display field except the floor — a pet spawned on the second
monitor was sized for the first one.

**`app/src/pets/store.jsx`** — `K` is persisted. `PET_TASTE_MIN/MAX/STEP/DEFAULT`,
`petTaste()` (every bad input becomes 1), `petTasteMap()`, `displayTasteKey()` (a durable
panel key, explicitly **not** `display.id`, with its two known failure modes written
down: two identical EDID-less panels share a key, and a genuinely changed panel reverts
to 1.0), actions `setDisplayTaste` / `resetDisplayTaste` / `resetAllDisplayTaste`, and
readers `displayTaste` / `hasDisplayTaste`. The map is a **sibling** of `opts`, not a
field inside it; a corrupt `taste` map no longer takes the roster, the names and the
sizes down with it; "remove all pets" deliberately does not clear it, because taste is a
property of the desk rather than of any pet; and storing a value equal to the default is
kept distinct from never having touched that screen.

**`app/src/components/Pets.jsx`** — the size read-out was re-derived for a per-display
world. `petBaseHeight()` is the single place the `opts.size × OVERLAY_SIZE_FACTOR` chain
is expressed, so "twice" cannot become "three times". `petReadout()` prints **px for a
widget pet** and **millimetres for a screen pet** — a desktop pet is deliberately a
different pixel count on each monitor, so quoting one monitor's pixels would be
publishing one screen's number as if it were the answer. The mm figure follows from
`M = ppi / (96 * S)` alone and needs no display list. The header wraps rather than
clipping, because `"2.0x · 14.7 mm"` is longer than what was there before.

**`docs/multi-monitor-sizing.md` (565 lines, new).** The three units and their
conversions, the `M_phys` / `M_sf` maths with the measured residual (the scaleFactor
fallback recovers ~86% of the error; the remaining 9.2% is the gap between *chosen*
scaling and *true* density), why `K` must stay a separate factor forever, a table of
every degenerate input and its fallback, eight numbered traps, and how to verify.

**Verified by.** `tests/pets_engine_regression.mjs`, **28/28** — grown from 12. The
display-aware tests are new and include: a mixed-DPI fixture built in **CSS px** (not
DIPs) asserting both entries survive the filter and the span invariant holds; "a pet is
the same PHYSICAL size on both monitors, which means a DIFFERENT pixel size" (a test
asserting equal *pixel* heights would be asserting the bug); "the EDID path, when ppi is
known, gives TRUE physical parity"; seam crossing changes size once and monotonically;
seam **hysteresis** — jiggling on the boundary never oscillates; roam bounds stay ordered
on both screens however big a pet gets; a pet that grows onto the dense screen settles
instead of hopping forever; 1200 frames on a mixed-DPI desktop reach a stable size with
no loop; a payload with **no `sf` and no `ppi`** still installs with `M = 1.0`; a single
legacy display still works; and the per-display factor can never make a pet invisible or
screen-filling.

**What is not verified.** `app/electron/ppi.js` has **no test of any kind** — nothing in
`tests/` loads it — and the whole EDID path has never been executed against the real
two-monitor desktop. `K` is built, persisted and clamped but **cannot currently be set or
read by anything** — see open items 10 and 11, which are the honest state of this section.

---

## 10. Smaller fixes made along the way

- **The pet animation loop never stopped.** `World.stop()` only flipped a flag, leaving
  a frame already queued. On the desktop overlay — a transparent, always-on-top,
  full-desktop window with `backgroundThrottling` disabled — that loop went on
  compositing forever behind a layer nobody was looking at. `stop()` now calls
  `cancelAnimationFrame`; `frame()` parks itself when there are no pets; new `wake()`
  is called from `add()`, the only place the pet list ever grows. `PetLayer` switched
  from `visibility` to `display`, because a hidden-but-laid-out layer still had a size
  and nothing ever told the engine it was idle.
- **Move-event storm on a DPI boundary.** `app/electron/main.js` — `onWindowMove` did
  a `JSON.stringify` of a fresh native `getBounds()` (unconditionally, whether or not
  `--trace` was passed), a display match and one native `setBounds` per pane on every
  single event. Windows emits `move` faster than a frame during a drag, and faster still
  crossing between two DPI panels, which is what turned that crossing into a stall.
  Now coalesced to one pass per frame (`MOVE_COALESCE_MS = 16`), with only the pane
  hiding left on the raw event. The trace is guarded behind `if (TRACE)`.
- **Timers outliving their window.** `win.on('closed')` now clears `moveCoalesce` and
  `moveIdle` and forgets `lastDisplayId`, so a recreated window re-announces its display
  instead of matching the id the dead one happened to end on.
- **Pane seams moving across a DPI change.** `paneBounds()` now snaps width and height
  through the same device-pixel grid as x and y. Rounding edges on one grid and extent
  on another lets a card's right edge land a pixel off its neighbour's left one, and
  which way it lands changes with the scale factor.
- **Overlay click-through flapping.** `app/src/pets/overlay.js` — turning solid is
  immediate (a pet that cannot be grabbed the instant the cursor touches it feels
  broken); letting go is delayed 120ms, because each call ends in a cross-process
  `SetWindowLong` on an always-on-top full-desktop window and a hit test oscillating on
  a sprite's boundary would fire one per mousemove. Sub-pixel jitter under 2px is
  re-answered rather than re-asked.
- **A pet spawned on the second monitor was sized for the first.** `entryScreen()`'s
  caller rebuilt `{ floor: screen.floor }` as an object literal, which looked like a
  destructure and was a whitelist: every per-display field but the floor was dropped on
  the way in. The entry itself is now passed through, and the new pet also carries
  `display` from its first frame rather than waiting for something else to resolve its
  position back to a screen.
- **The accounts pane heard nothing during a sign-in.** `app/src/useSidecar.js` gained
  an `ai_accounts` reducer case with its own state slice, kept apart from the `ai`
  usage-poll slice so a login beat cannot stomp usage numbers or vice versa. It carries
  a `seq` counter so a *repeated identical* notice still reaches a subscriber — a second
  duplicate-account attempt sends the exact same `note` text, and a React consumer
  watching only `note` would stay silent the second time.
  `app/src/components/AiAccounts.jsx` consumes `err` / `note` / `status` (field names
  read from the sidecar's emitter — an earlier draft read `notice`/`message`, which no
  emitter ever sets, so every one of them was silently dropped) and clears the pending
  row on `status: 'refused'`, instead of spinning for the full 12s `PENDING_MS` and then
  claiming it got no answer.
- **Error notices were truncated mid-word.** `clip()` no longer shortens anything; the
  notice element wraps. `NOTICE_MAX` is kept at 320 as a layout hint only — the
  sidecar's longest known message is the ~175-character duplicate verdict.
- **One-click permanent account deletion.** The remove control was a bare `×` glyph, the
  same size and colour as the refresh `↻` one pixel away, with no confirmation. It now
  reads **Remove**, and the first click only arms a `role="alertdialog"` confirm row; a
  command in flight clears a stale prompt, because the click after it would resend a
  remove for whatever account is selected by then.
- **The browser warning was a tooltip.** A tooltip nobody reads before clicking is
  decoration, not a warning. It is now `SignInWarning`, a click of its own, asked every
  time — cheap to click through when the browser is already right, and skipping it "for
  good" would put the first user who forgets right back where this bug started.
- **`.env.example` documents the login sandbox.** A new "Adding a SECOND account
  (isolated login)" section, with twelve commented keys: `AI_LOGIN_HOME_VARS_*`,
  `AI_LOGIN_CONFIG_VAR_*`, `AI_LOGIN_CONFIG_REL_*`, `AI_LOGIN_CRED_REL_*`,
  `AI_LOGIN_UNSET_VARS_*` and `AI_LOGIN_SUBCOMMAND_*` for both providers. The stale
  `WIDGET_TARGET_PPI` / `WIDGET_SCALE_PRIMARY` / `WIDGET_SCALE_SECONDARY` block is now
  explicitly marked as belonging to the old Tk build and ignored by the React one —
  which is worth reading next to §9, since the Tk build did per-monitor EDID sizing and
  the React build is only now getting it back.
- **The two user-facing documents were rewritten around the browser problem.**
  `README.md` gained "Read this before adding a second account";
  `docs/multi-account-login.md` grew from a short note to eleven numbered sections
  (what the sandbox isolates and what it does not, re-authentication, removal and why it
  stays removed, matching and its deliberate over-reach, undoing a removal, the
  two-copies problem, the three failure states, the geographic gate).

---

## 11. Findings that cost real time, and will again

These are the non-obvious ones. Each was expensive to find and none of them announces
itself.

**An empty array is truthy.** `if (!manifest)` never re-runs once `manifest = []`. A
cache that stores "the answer" and a cache that stores "whether we have an answer" are
different caches, and `[]` is exactly where they diverge. Cache success; rate-limit
failure.

**A fingerprint built on a field that changes on every token refresh is not a
fingerprint.** `subscriptionType + expiresAt` looked like identity and behaved like a
timestamp. Everything downstream — duplicate detection, tombstones, the re-auth guard —
was silently inert for Claude, and each one of those looked correct in isolation. When
an identity check never fires, suspect the identity, not the check.

**`grep -c $'\r'` lies on this UNC share.** It reports CR on files that are pure LF. Any
line-ending check on `//wsl.localhost/...` has to read the bytes in Python. Several
hours of "the file has CRLF now" chased a tool artifact.

**A test suite can pass while four real bugs are live.** The pre-existing login suite
never crossed the WSL interop boundary, so it could not see that `HOME` was being reset
from `/etc/passwd`, that `~/.bashrc` was re-exporting the proxy variables, that a bare
`claude` opens a REPL rather than signing in, or that `wslpath` double-translation
produced `/mnt/c/mnt/c/...`. A green suite is evidence about the boundary it crosses and
nothing beyond it. Several suites in this cycle were deliberately run against the
*pre-fix* code to prove they actually fail — a test that has never been seen to fail is
not yet known to test anything.

**Tailwind preflight beats an inline width.** `img { max-width: 100% }` from preflight
overrode `style="width: 750px"`. Worse, only one of the two documents loads preflight,
so the widget and the overlay disagreed about the same sprite — which reads as an engine
bug and is a stylesheet bug. When two renderers of the same code disagree, compare their
*stylesheets* before their logic.

**A screenshot cannot show a per-DPI physical-size bug.** The sprite is the same number
of CSS px on both panels, hence — through the single window-wide `S` — the same number of
*device* px. The captured pixels are **identical by construction**: screenshot both
monitors, overlay them, diff them, and they match perfectly while the bug is at its
worst. Only the physical size differs, and a screenshot threw that away before you
looked. The same reasoning kills "it looks the same to me" as evidence and kills any test
that compares rendered pixel buffers. To see it you must normalise each capture by
`1 / ppi(d)`, or measure the glass with a ruler.

**`clamp()` returning the midpoint on inverted bounds turns a size increase into an
infinite loop.** Every bound in the engine is written as `something ± w * k`, and
*growing a pet is precisely the operation that drives `lo` past `hi`*. Nothing raises;
the pet is silently teleported to the middle of an impossible range, every frame. Two
confirmed loops came from exactly this — a permanent hop loop when the usable band is
shorter than the pet, and a permanent turn-around loop on a card narrower than `0.8 * w`.
The pet is alive, animating and permanently unusable, which does not look like a crash.

**`dmLogPixels` is process-wide, not per-monitor.** `EnumDisplaySettings` reported `144`
for **both** panels on a desktop whose two screens run at visibly different effective
scales. A per-monitor scale derived from it yields two identical numbers and the
confident, wrong conclusion that there is no difference to fix.

**A DPI-unaware measurement process measures a fiction.** Without
`SetProcessDpiAwareness(2)` before the first measurement call, Windows virtualised the
coordinates and a 2560x1600 panel reported as 1707x1067 — a probe trusting that would
have invented a 1.5x difference that does not exist. EDID reads are the exception: that
data lives in the panel and is untouched by virtualisation.

**Tightening a payload filter turns a legacy payload into a silent no-op.** The display
map passes through two hand-duplicated whole-entry predicates (`overlay.js` and
`engine.js`) that **drop whole display objects**, and the install is gated on
`some(usable)` so that a payload with no usable entry is never installed — the previous
map simply persists. Adding `&& Number.isFinite(d.sizeScale)` to either one would make
every older payload vanish with no log anywhere, and the symptom would be "nothing
happened" rather than a stack trace. Default a new field; never gate on it. The same
shape of silence sits in four more places, all whitelists wearing the clothes of a
destructure — which is exactly how the `{ floor: screen.floor }` bug in §10 happened.

**Never pipe Electron's stdout to `head` while debugging.** `head` closes the pipe, the
writer takes `SIGPIPE` and dies, and the app vanishes mid-startup with a truncated log —
indistinguishable from a crash you will then spend an hour debugging. Redirect to a file.

**A browser session decides an OAuth identity, no matter how good the sandbox is.** This
one is not a defect and there is no fix for it. See below.

---

## 12. The limit no code can remove

**Even with a perfect CLI sandbox, an OAuth sign-in completes as whoever the system
browser is already signed in as.**

The sandbox isolates the CLI: it gives it a fake home, so it finds no existing session
and is forced to ask. But asking means opening the user's real browser, which this
software does not own and cannot isolate. If that browser still holds a session for the
first account, the handshake completes as that account immediately — no login form, no
account picker, no prompt. The credential handed back is genuine and correct; it is
simply for the wrong account.

There is no code change that fixes this. What the code can do, and now does, is refuse
to act on the result: a sign-in that comes back as an account already in the vault is
refused and nothing is written, and a re-authentication that comes back as a *different*
account is refused too (that second one matters most — both providers kill the old
refresh token the instant they issue a new one, so writing the wrong credential into a
row would have destroyed the account being repaired).

**The user must sign out at the provider's own site, or use a private/incognito window,
or use a separate browser profile, before adding a second account.** The `SignInWarning`
screen says so before every sign-in, and `README.md` and `docs/multi-account-login.md`
say so at length.

---

## Verification

Every number below is from a run performed while writing **this revision**, on this
working tree. Nothing is inherited from an earlier report or from the earlier revision of
this document.

Run from the repository root.

```
python3 tests/ai_accounts_regression.py
python3 tests/ai_provider_adapters_regression.py
python3 tests/ai_login_sandbox_regression.py
python3 tests/ai_credsources_regression.py
node   tests/pets_engine_regression.mjs
```

| Suite | Result |
|---|---|
| `tests/ai_accounts_regression.py` | **28 passed, 0 failed, 1 skipped, 29 total** |
| `tests/ai_provider_adapters_regression.py` | **43 passed, 0 failed, 0 skipped, 43 total** |
| `tests/ai_login_sandbox_regression.py` | **44 passed, 0 failed, 0 skipped, 44 total** |
| `tests/ai_credsources_regression.py` | **55 passed, 0 failed, 0 skipped, 55 total** |
| `tests/pets_engine_regression.mjs` | **# tests 28 / # pass 28 / # fail 0** (was 12/12) |

Two results carry a note the summary line does not:

- `ai_accounts_regression.py` skips `08b vault file permissions are restrictive`, with
  the reason "POSIX mode bits are not enforced on Windows; checked the 0600 open path by
  inspection only". That is a genuine gap on this platform, not a passing test.
- `ai_login_sandbox_regression.py` passes but prints a contract finding:
  `classify_account` takes `(provider, cred, accounts)` while the documented contract
  describes `(cred, accounts)`. Defensible — the fingerprint is provider-specific — but
  a caller written to the contract binds its arguments one slot over and gets a **wrong
  answer rather than a TypeError**. Unresolved.

Several of these suites were separately run against the pre-fix code during the cycle
and produced real failures, so they are known to be non-vacuous. The engine harness has
a first-class mechanism for this — `PETS_ENGINE=<path>` swaps in a different engine file,
so the new tests can be pointed at the pre-fix engine and required to fail. That earlier
evidence is recorded in the per-task reports, not re-run here.

**What was not run:** there is no build, lint, typecheck or Electron launch in the list
above, and no test at all exercises `app/electron/ppi.js`, `app/electron/pets.js`,
`app/electron/main.js`, `app/src/pets/overlay.js`, `app/src/pets/store.jsx`,
`app/src/components/Pets.jsx`, `app/src/components/AiAccounts.jsx` or
`app/src/useSidecar.js`.

---

## Open items

Read this section before treating the change set as finished.

**1. None of this has been run on the real desktop.** No part of this change set has
been exercised in a running Electron app on the user's actual two-monitor setup. "The
tests pass" means five Python/Node suites pass; it does not mean pets walk correctly,
that the squash fix looks right, that the overlay lands on the right monitors, that the
EDID probe returns anything at all on this machine, or that a sign-in completes. The
multi-monitor geometry (§4), the sprite-squash fix (§2) and the whole of the
per-display sizing work (§9) are especially exposed: all three were derived from
measurement and reasoning, and all three are exactly the kind of change that can only
really be confirmed by looking at it — and §9 cannot be confirmed by a screenshot even
then. **This is the single largest open risk in the change set.**

**2. `aisecrets.py` F1 - the destructive half is now fixed; the read path is not.**
The finding was: `_dpapi_unprotect` returns `None` on any failure, `read_secret`
propagates `None`, `aiaccounts.load()` turns that into an empty document
indistinguishable from a fresh install, and the next `save()` writes that empty
document over the only copy - returning **True**, because the readback check only
confirms that what was written reads back and an empty document round-trips fine. No
backup, so the loss was permanent.

`aisecrets.py` was changed late in this cycle (199 insertions) and the irreversible
half is closed. The read path now reports three outcomes instead of one:
`read_secret_ex()` returns `(data, status)` with `STATUS_OK` / `STATUS_ABSENT` /
`STATUS_UNREADABLE` / `STATUS_ERROR`, keeping "absent" apart from "exists but could not
be decrypted" apart from "could not be fetched at all". `write_secret()` gained an
`allow_unreadable_overwrite=False` parameter and **raises `VaultUnreadableError`**
rather than landing a write on top of bytes it could not read. A second guard,
`VaultVerificationError`, checks in memory that a freshly wrapped blob decrypts back to
its input before anything touches the target file. `read_secret()` keeps its old
collapse-to-`None` signature, so existing callers are unaffected.

`aiaccounts.save()` inherits the protection without being changed: it calls
`_secrets.write_secret()` at `aiaccounts.py:269` inside a `try/except`, so an
unreadable vault now makes the save return **False** instead of destroying the
ciphertext. That is the outcome that matters - a stall the user can act on rather than
a data-destroying success.

**What is still open.** `aiaccounts.load()` still calls the collapsing `read_secret()`
(`aiaccounts.py:176`), not `read_secret_ex()` — re-checked against the current file for
this revision, and still true. So an undecryptable vault still *presents* as zero
accounts with no explanation: the user sees an apparently empty widget and is told
nothing about why. The data is no longer destroyed, but the diagnosis is still missing,
and nothing surfaces "your vault exists and this machine cannot open it" to the UI. Also
unchanged: there is still no backup of the blob before the first wrap, which was step (3)
of the recorded fix order.

The hazard remains latent while the vault is plaintext, since a legacy document always
parses. Encryption is now materially safer to enable than it was, but the read-path
diagnosis should be wired through before it is turned on.

**3. `list_forgotten()` / `allow_reimport()` have no UI.** Still true. `core.py` calls
`_allow_reimport()` internally after a deliberate manual sign-in, but nothing exposes
either function to the user: there is no "previously removed" row and no one-click undo,
and `list_forgotten` appears nowhere in `sidecar.py` or `app/src`. Recovery today means a
`python -c` invocation. This matters because the tombstone's `cred-path` match has a
deliberate false-positive direction — a genuinely different login that later occupies the
same credential file stays blocked — and the undo for that is not reachable from the
widget.

**4. The generic `cmd` channel is now closed; one sibling channel is not.** This item
previously recorded an open hole — `preload.js` exposes named verbs *and* a generic
`send`, and `main.js` was said to re-validate only the verbs its `AI_COMMANDS` table
names, letting everything else fall through to `toSidecar(cmd)` verbatim. **That was
closed in this cycle and the description above is no longer accurate.** The `cmd`
handler (`app/electron/main.js:1385`) now resolves the verb against two closed tables —
`AI_COMMANDS` (`main.js:1253`) and `RENDERER_COMMANDS` (`main.js:1310`) — using
`Object.hasOwn` rather than `in` or a bare lookup, explicitly so an inherited member such
as `__proto__` or `constructor` cannot resolve to a callable (`main.js:1411-1414`). A
verb in neither table is dropped with a traced line and never reaches the sidecar
(`main.js:1415-1422`). What does reach it is rebuilt from validated fields —
`toSidecar({ cmd: name, ...fields })` at `main.js:1433` — so an extra field the renderer
invented is discarded rather than forwarded. `grep 'toSidecar('` returns four call sites:
`main.js:491`, `497` and `1554` are fixed literals and `1433` is the rebuilt payload;
**there is no verbatim pass-through left.** Field validators go through `safeKey()`
(`main.js:1239`), which enforces `/^[A-Za-z0-9][A-Za-z0-9._-]*$/` and rejects `..`. The
`open` verb is gated separately on `OPEN_SCHEMES = {http:, https:}` parsed via `new URL()`
(`main.js:1195`, `1387-1397`), so `ms-msdt:`, `search-ms:` and the `smb://` NTLM-leak are
refused before `shell.openExternal`. The sidecar verbs the UI never sends —
`ai_cred_scan`, `ai_cred_import`, `ai_windsurf_cached`, `ai_login_cancel`, `open_log`,
`style_pane` — are deliberately absent from both tables, so naming one no longer reaches
the credential surface. The `preload.js` comment still says "Do not read these named
methods as the boundary", and that remains correct: the boundary is `main.js`, which is
where it now is.

**What is still open in this class:** `sendHome(id)` (`app/electron/overlay-preload.js:51`)
forwards an unvalidated `id` from the overlay renderer to the widget renderer via
`ipcMain.on('pets-home')` (`main.js:1381-1383`). It is the one channel that did not get a
validator this cycle. The `id` is used only as a lookup key into a roster the widget owns,
so an unknown id is a no-op with no path to a filesystem, URL or subprocess — but the rule
should be uniform. Applying `safeKey(id, MAX_KEY)` at `main.js:1381` is the fix, and it is
consistency work, not an urgent hole.

**5. No import-on-demand trigger.** `sidecar.ai_migrate()` exists but is a **startup
thread**, one run per process, not a command anyone can invoke: it is in the launch list
alongside `net_loop` and `ai_loop` and has no dispatch entry. There is still no
`ai_migrate` / `import_from_cli` verb on the sidecar's command surface and no UI
reference to one; the user-driven path remains the per-file `ai_cred_scan` /
`ai_cred_import` pair. The designed auto-import consent card (`accepted` / `deferred` /
`declined`, with `ai_import_policy`, `ai_import_decide`, `ai_import_candidates`) is
specified in the reports and **not built**.

**6. The Codex unset list is a guess.** `("OPENAI_API_KEY", "OPENAI_BASE_URL")` is
labelled in `ailogin.py:258-259` as "unproven guess, not yet confirmed against a live
codex CLI". The Claude list and `claude auth login` *were* confirmed against the
installed CLI. Both lists are now also visible as commented `.env` keys, which makes the
guess overridable without a code change but does not make it correct.

**7. `classify_account` contract mismatch.** See the Verification note above. The
implementation takes `(provider, cred, accounts)`; the documented contract says
`(cred, accounts)`. A caller written to the documentation silently gets a wrong answer.

**8. A mode change is in the diff.** `tests/ai_credsources_regression.py` shows in
`git diff --stat` with **0 insertions and 0 deletions** — the only change to it is
`mode change 100755 => 100644` (confirmed via `git diff --summary` for this revision).
Its content is untouched. It is worth deciding whether that mode change should be part
of the commit.

**9. No coverage for large parts of what changed.** The Electron main process, the
overlay renderer, the pets store, and every React component listed at the end of the
Verification section have no automated test. They were changed on the strength of
reading, and they are the parts item 1 above would exercise. `app/electron/ppi.js` — 465
lines of new code, containing an embedded PowerShell program, a P/Invoke surface and a
cache — is the largest single instance of this.

**10. `K`, the per-display taste multiplier, is built but unreachable.** This is the
biggest gap in §9, and it is a plumbing gap rather than a design one. Every piece exists
in isolation and no wire connects them:

- `engine.js` reads `K` from `d.k` on a display entry — and **nothing anywhere sets
  `d.k`**. `app/electron/pets.js` builds each display entry with an explicit field list
  that does not include it.
- `store.jsx` exports `setDisplayTaste` / `resetDisplayTaste` / `resetAllDisplayTaste`
  and `displayTaste` / `hasDisplayTaste`, and **nothing outside `store.jsx` calls any of
  them**. `Pets.jsx` does not import `PET_TASTE_*` and has no per-screen control; its
  sliders are Size, Speed, Liveliness and the toggles, exactly as before.
- `useOverlaySync()` does send the whole `taste` map across IPC — and
  `overlay.js`'s `onState` handler never reads `state.taste`.

So `displayTaste()` returns 1 for every display, on every path, today. The consequence is
benign (the measured correction `M` still applies and `K = 1` is its documented default)
but the feature is not shipped, and `Pets.jsx`'s read-out comment already says the mm
figure excludes a taste value the user cannot currently set. Finishing it means: emit `k`
per display from the main process (or look it up by `displayTasteKey` in the overlay),
consume `state.taste` in `overlay.js`, and add the per-screen control.

**11. The per-display correction does not apply to widget pets.** `setDisplays()` has
exactly one caller, `overlay.js`. `PetLayer.jsx` — the in-widget world — never installs a
display map at all, so `displayScale()` returns 1 for every pet inside the widget. That
may well be the right answer (the widget is one small window on one screen at a time, and
`Pets.jsx` deliberately reads out widget pets in px and screen pets in mm), but it is
undocumented and it means a widget dragged between two panels of different density still
changes physical size. Decide it deliberately rather than leaving it as an artifact of
where the wiring stopped.

**12. `ppi.refresh()` is never called after startup.** The module's own header documents
it as "call on display-added/removed/metrics-changed", and `ppi.js` exports it — but the
only invocation is the one-shot probe at require time. `pets.js` registers
`display-metrics-changed` / `added` / `removed` handlers that call `resyncOverlay()` and
do not re-probe. Because the cache is keyed by device-pixel origin, a rearranged or
replugged monitor does not produce a *wrong* PPI — it produces no match, `ppiFor()`
returns 0, and the display silently falls back to the `scaleFactor` approximation for the
rest of the process's life. That is the safe direction, and it is still a permanent,
silent degradation that a one-line call in `watchDisplays()` would fix.

**13. The units comment in `pets.js` was fixed; the latent double-divide was not
fully.** `dipRectToPhysical()`'s no-intersection path now converts through the nearest
panel rather than returning DIPs, which closes the silent double-divide — but when there
is no panel at all it still returns the input rectangle unconverted. That is reachable
only with an empty panel list (mid-hotplug, every display gone), and it is the one
remaining place in that function where the returned unit is not the one the contract
promises.
