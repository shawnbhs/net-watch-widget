# net-watch-widget

An always-on-top desktop widget for Windows that answers, at a glance:
**am I on the VPN, is the connection healthy, and how much of my AI plan is
left?**

Python gathers the data, React draws it. One PowerShell command to install.
Starts automatically with Windows and stays out of the way.

![The widget in compact mode](docs/compact.png)

---

## Contents

- [What it shows](#what-it-shows)
- [Requirements](#requirements)
- [Install](#install)
- [Using it](#using-it)
- [Pets](#pets)
- [Configuration](#configuration)
- [AI usage tracking](#ai-usage-tracking)
- [Restarting after an edit](#restarting-after-an-edit)
- [Uninstall](#uninstall)
- [Troubleshooting](#troubleshooting)
- [Privacy and security](#privacy-and-security)
- [How it works](#how-it-works)
- [License](#license)

---

## What it shows

**Mini bar** — a tab docked to an edge of the screen, out of the way of
everything: a dial each for CPU, RAM, GPU and the two AI allowances, the ping,
and the adapter dot. It sits on the top, the left or the right edge — a row of
dials on the top, a column of them down a side — at either end of that edge or
in the middle of it. Click it to open the widget; drag it to move it, including
onto a different edge. Nothing is labelled at this size — hover a dial for its
name — because the point of it is to be glanceable, not readable.

All five dials are always there, showing a dash until they have a reading, so
the tab never changes size and never shifts under you. It is the mode the widget
reopens in if it was the mode you left it in.

**Compact mode** — a single strip: country flag and name, ping, VPN state,
public IP, ISP, CPU / RAM / GPU, AI usage bars, clock.

**Full mode** — everything above plus:

| Section | Contents |
|---|---|
| Network | public IP, local IP, gateway, DNS, hostname |
| Latency | round-trip time and packet loss to two hosts |
| Hardware | CPU, RAM, disk, network throughput |
| Timezone | system timezone against the timezone of your exit IP |
| AI usage | Claude and Codex quota percentages with reset countdowns |
| Quick checks | ISP, ASN, proxy/datacenter detection, DNS, a connection score |

The VPN indicator is not a guess from the IP alone: it cross-checks the
country reported by a geo-IP lookup against Cloudflare's own edge report, and
flags a mismatch between the two rather than trusting either one.

**A changed exit address turns its card red.** If either the public IP or the
Runflare address moves, that card gets a red border and pulses three times, then
holds the red for a minute — long enough to still be there when you next glance
over. Hover it to see what the address was before. Only a move between two real
addresses counts: a failed lookup is not a move, so a network hiccup will not
cry wolf.

**Pets**, if you want them — pixel animals that either walk the tops of the
cards and hop between them, or leave the widget and roam the desktop. Off until
you add one. See [Pets](#pets).

---

## Requirements

- Windows 10 or 11
- Python 3.9 or newer from [python.org](https://www.python.org/downloads/)
  (tick **Add python.exe to PATH** during setup)
- Node.js 20 or newer from [nodejs.org](https://nodejs.org)
- One Python dependency, `psutil`, which the installer installs for you

`tkinter` is **not** required. Nothing here imports a Python GUI toolkit.

You do **not** need an API key, an account, or an internet-facing service.

---

## Install

```powershell
git clone https://github.com/shawnbhs/net-watch-widget.git
cd net-watch-widget
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

That single command:

1. verifies Python and Node are present,
2. installs `psutil` if it is missing,
3. installs the UI's dependencies and builds it — the first run downloads
   Electron, about 100 MB,
4. creates `.env` from `.env.example` (all settings optional),
5. registers the widget to start automatically at login,
6. starts it.

Re-running it is safe — it rebuilds the UI, updates the autostart entry and
leaves an existing `.env` alone.

To set it up without autostart:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -NoAutostart
```

### Why `-ExecutionPolicy Bypass`

Windows blocks unsigned scripts by default. The flag applies to this one
invocation only and changes nothing system-wide. Read `install.ps1` first if
you would rather not take that on faith — it is about 150 lines and does
exactly what the list above says.

---

## Using it

- **Drag the title bar** to move it. Position is remembered.
- **Drag the bottom-right corner** to resize it — the whole widget scales, text
  and all. Double-click that corner to go back to normal size.
- **Right-click anywhere** to switch between compact and full.
- **Click a readout** — either IP, the local address, the gateway, a latency
  figure — to copy it.

The footer buttons, left to right: mini bar, compact/full, lock position,
network toggle, refresh, quit. Hover any of them for a tooltip; the two stateful
ones name the action they will perform, so the network button reads "Cut
network" while you are connected and "Restore network" while you are not.

### Resizing it

The grip in the bottom-right corner scales the entire widget — type, icons,
bars, the lot — rather than stretching the panels, because the layout is a
fixed-width column of hand-picked type sizes and a wider one would only leave
small text stranded in the middle of big panels. Double-click the grip to
return to 100%. The size is remembered, and the mini bar inherits it.

It grows to **twice** normal size, and shrinks only about 8%. The floor is a
Windows limit rather than a preference: the frosted panel behind each card is a
window, and Windows will not make a window shorter than 39 pixels, so below
roughly 90% the frost behind the thinnest cards would stand slightly proud of
them.

### The mini bar

The ↑ button collapses the widget to a tab on the edge of the screen.
**Click the tab** to open it again — it returns to wherever you had dragged the
widget, not to the edge. Locking the position stops the drag but not the click.

**Drag the tab** to move it. Letting go parks it on the nearest of nine docks:
the top, left or right edge, at the start, the middle or the end of it. It
changes shape to suit — a row of dials along the top, a column down a side — and
it can be carried to another monitor on the way.

The tab deliberately overhangs its edge by a few pixels. Windows rounds the
corners of the frosted panel behind it and gives no way to ask for square ones,
so the overhang is what puts that rounding out of sight and lets the tab meet
the edge cleanly.

**Quick checks** fill themselves in a second or two after launch, and again
whenever your public IP changes. **Run** re-runs them on demand and **Clear**
blanks them. The five pills below open that IP in an external lookup.

**AI usage** polls every 15 minutes. The ↻ beside the heading forces a poll,
subject to a five-minute floor.

### The network button

The power button cuts your connection: it disables every physical network adapter,
wired **and** wireless. Click it again to bring them back. The dot is green
while connected, red while cut, and amber for the couple of seconds a toggle is
in flight.

Each toggle raises **one UAC prompt**, because disabling an adapter needs
administrator rights and the widget deliberately runs as a normal user rather
than asking you to run the whole thing elevated. Declining the prompt is a
no-op — the network is left exactly as it was.

Virtual adapters are left alone: hypervisor switches (disabling one takes your
VMs and containers offline with it) and VPN tunnel adapters (their client owns
them). See `NET_SKIP` in `.env.example` to adjust the exclusions.

If the adapters ever stay down — a crash mid-toggle, a declined restore — this
puts them back from an **Administrator** PowerShell:

```powershell
Get-NetAdapter | Where-Object { -not $_.Virtual } | Enable-NetAdapter -Confirm:$false
netsh wlan set autoconfig enabled=yes interface="Wi-Fi"
```

The second line matters more than it looks. `netsh wlan set autoconfig
enabled=no` is the other common way to cut Wi-Fi, it survives a reboot, and it
stops the interface associating even after the adapter is enabled again — an
adapter that reads *Up* but will not connect. The widget clears it on every
restore, so you only need this if you are recovering by hand.

---

## Pets

The widget keeps pixel pets. They are optional, off until you add one, and they
come in two kinds.

Open the **Pets** card at the bottom of the full view. **+ Add a pet** shows all
23 species; pick one, pick a colour if it has several, and it drops in. Each pet
gets a row of its own with a **Widget / Screen** switch:

- **Widget** — it lives on the widget. It walks the top edge of a card, hops
  between cards, and stands about on them. Its body reaches up over the card
  above, so a pet crossing the panel will briefly pass in front of a readout.
  That is the trade: the cards are 8px apart and there is nowhere else for it to
  be. The topmost card is the ceiling and nothing stands on it, because there is
  no window left above it to draw a pet in.
- **Screen** — it leaves the widget and roams your desktop, walking and hopping
  wherever it likes and resting just above the taskbar. This opens a second,
  full-screen, click-through window covering your **primary display**; it exists
  only while at least one pet is out there, and closes itself when the last one
  comes home.

The **New pets live in the** switch at the top of the card sets which of the two
a newly added pet starts in. Nothing stops you having some of each.

### Handling them

| Gesture | What happens |
|---|---|
| Click a pet | Poke it |
| Drag a pet | Pick it up; let go and it falls, keeping the throw |
| Double-click a pet | Send it across — widget to desktop, or desktop back to the widget |
| Right-click a widget pet | Hop it up one card |
| Scroll over a widget pet | Move it up or down between cards |
| Right-click a desktop pet | Tell it to follow your cursor, or to stop |

Everything a gesture does has a button equivalent in the card, so nothing is
drag-only.

### Settings

The **⋯** button in the card's header opens the tuning: **size** (16–40px, and
it scales with the widget), **speed**, **liveliness** (how much they move about
versus lie down), **react to the cursor**, **dance breaks**, **contact
shadows** and **reduced motion**. These apply to every pet at once — only the
widget/screen choice is per pet. The roster and the settings are remembered.

Pets are hidden while the widget is collapsed to its mini bar: a tab is a single
card, and with the topmost one reserved as the ceiling there is no ground left
in it. Pets out on the desktop carry on regardless.

### Adding your own sprites

The species list is built by scanning `app/assets/pets/` at startup, so adding a
folder of `<colour>_<action>_8fps.gif` clips there is enough to add a species —
no code change. Re-measure them afterwards, or the pet will resize as it changes
animation:

```powershell
pip install Pillow
python app/tools/gen-metrics.py
```

---

## Configuration

Everything is optional. **The widget runs correctly with no configuration at
all** — copy `.env.example` to `.env` only if you want to change something.

| Key | Default | What it does |
|---|---|---|
| `CLAUDE_CRED_PATHS` | `%USERPROFILE%\.claude\.credentials.json` | Where to find the Claude CLI's stored login |
| `CODEX_CRED_PATHS` | `%USERPROFILE%\.codex\auth.json` | Where to find the Codex CLI's stored login |
| `PING_HOST_1` | `1.1.1.1` | First latency target |
| `PING_HOST_2` | `8.8.8.8` | Second latency target |
| `LOG_FILE` | `%USERPROFILE%\ip_bar_log.txt` | Where the log is written |

Both credential keys accept several paths separated by `;` — the first
readable one wins. `~` and `%VARIABLES%` are expanded.

Restart the widget after editing `.env`; it is read once at startup.

---

## AI usage tracking

If you use the **Claude Code** or **OpenAI Codex** CLI, the widget shows how
much of your plan you have used, with a countdown to each reset.

It works by reading the OAuth token those CLIs already saved when you logged
in. There is no API key to paste, and the widget never sees your password.

**If you have not logged in to either CLI, the widget still works perfectly** —
the AI rows show `setup` and the note line reads *"please run 'claude' and log
in"*. Everything else keeps running. Nothing is polled, so there is no wasted
network traffic and no error spam in the log.

To enable it, log in to whichever CLI you use:

```powershell
claude    # then follow the login prompt
codex     # then follow the login prompt
```

The widget picks the credentials up on its next cycle (within 15 minutes), or
immediately if you hit **Refresh**.

### If you run the CLIs inside WSL

The tokens then live in your Linux home, not your Windows profile. Point the
widget at them in `.env`, replacing the distro and username:

```ini
CLAUDE_CRED_PATHS=\\wsl.localhost\Ubuntu\home\youruser\.claude\.credentials.json
CODEX_CRED_PATHS=\\wsl.localhost\Ubuntu\home\youruser\.codex\auth.json
```

### Staying logged in

Tokens are refreshed an hour before they expire, on a timer that runs whether
or not the usage panel is polling. Without that, a token could quietly lapse
during a long stretch with the machine off or the network down, turning a
background refresh into a manual re-login.

If a login does expire, the note line becomes **"login expired — click to sign
in"**. Clicking opens a console running the CLI's own login. The sign-in itself
is a browser round-trip — the widget cannot and does not automate it, and it
never handles your password. If the CLIs are not on your Windows PATH, set
`AI_LOGIN_CMD_CLAUDE` / `AI_LOGIN_CMD_CODEX` in `.env`.

### "credentials stale" vs "login expired"

These mean different things, and the difference matters when the same account
is stored in more than one place — a Windows profile *and* a WSL home, say.

The widget uses whichever copy expires latest. If the fresh copy becomes
unreadable (the WSL distro is stopped, a share is unmounted), only the old copy
is left, and refreshing from its long-dead token would fail. Rather than report
that as an expired login and send you off to redo a sign-in that was never
broken, copies older than the 16-day refresh-token lifetime are ignored and the
panel says **`credentials stale`** — meaning *the good file is out of reach*,
not *your login died*.

---

## Restarting after an edit

Editing a file on disk does **not** change the running widget.

Python only — `core.py` or `sidecar.py`:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

UI only — anything under `app/src`:

```powershell
cd app
npm run build
npm start
```

`npm start` clears `ELECTRON_RUN_AS_NODE` before launching. Some terminals
export it, and inherited it makes `electron.exe` behave as a plain Node binary:
the app dies on `app is undefined`, with a stack trace that points nowhere near
the cause.

---

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall
```

Removes the autostart entry and stops the widget. Delete the folder to remove
the files, and `%USERPROFILE%\ip_bar_log.txt` for the log.

The log keeps its old name so an existing history is not orphaned; `LOG_FILE`
in `.env` moves it.

---

## Troubleshooting

**Nothing appears after install.**
Run it in a console to see the error:

```powershell
cd app
npm start
```

**"Python was not found"** — Python is not on `PATH`. Reinstall from
python.org with **Add python.exe to PATH** ticked, or open a new terminal if
you just installed it.

**AI rows say `setup`.** That is the expected state before you log in to the
Claude or Codex CLI. See [AI usage tracking](#ai-usage-tracking).

**AI rows say `err`.** A real failure, not a missing login. Check `Open Log`.
The usual cause is an expired refresh token — logging in to the CLI again
fixes it.

**AI rows say `VPN required`.** The AI providers block some regions, so the
widget refuses to send a request when it cannot confirm the exit IP is
outside a blocked one. Connect your VPN and hit Refresh.

**The widget does not start after a reboot.** Check the autostart entry
survived:

```powershell
Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name NetWatch
```

If it is missing or the path is wrong, re-run `install.ps1`.

**Ping shows nothing.** Some networks block ICMP. Point `PING_HOST_1` at your
own gateway instead.

---

## Privacy and security

- **No telemetry.** Nothing about you is sent anywhere except the public API
  requests listed below, all of which you can see in the source.
- **No secrets in this repo.** `.env` is git-ignored, and the widget has no
  API key of its own.
- **Tokens are never logged, copied, or transmitted anywhere except the
  provider's own token endpoint** — `platform.claude.com` and
  `auth.openai.com`. The log records IP changes only.
- **Outbound requests**, in full: `myip.wtf` and `ipwho.is` (public IP),
  `ip-api.com` and `ipinfo.io` (ISP/ASN enrichment), `api.runflare.com`
  (second-opinion IP), `cloudflare.com/cdn-cgi/trace` (independent country
  check), and the two AI usage endpoints when a CLI is logged in.
- Two enrichment endpoints are plain HTTP because their free tier offers no
  HTTPS. They receive only a public IP address — never a credential — and
  their answers feed display fields only.
- Every scheme is checked before a request is opened, so no code path can be
  turned into a local file read.
- CI runs `semgrep`, `bandit`, `pip-audit`, `ruff`, and a full-history
  `gitleaks` scan on every push.

---

## How it works

Two halves, and the split is along the seam that was always there.

| | |
|---|---|
| `core.py` | Everything the widget knows: the geo cross-check behind the VPN verdict, the AI token handling, the adapter toggle, the country tables. Imports no GUI toolkit. |
| `sidecar.py` | Runs those functions on a schedule and writes what they return to stdout as JSON lines. |
| `app/` | An Electron shell and a React + Tailwind page that reads that stream. |
| `chrome.py` | One Win32 call Electron does not expose. |
| `netfast.py` | Local link-state detection, used by `core.gateway()`. |
| `app/assets/pets/` | The sprite set, scanned at startup to build the species list. |

Data is gathered on background threads, so a slow lookup never freezes the UI;
each panel updates as its own answer arrives.

The AI usage panel refreshes every 15 minutes with jitter, backs off
exponentially on failure, and stops entirely — no retries — when a provider
signals a region block. The quick checks run on an IP change and on demand,
never on the network cycle: two of their three lookups are free-tier enrichment
endpoints, and the cycle runs every three seconds.

[`app/README.md`](app/README.md) covers the parts of the UI that are not
obvious — why the frost is a separate window per card, why the cards are 8px
round, what happens when you drag or switch modes, and how the pets find the
ground under them.

### It used to be tkinter

Until recently this was one 6,455-line file, `ip_bar.py`, holding both the data
and a hand-drawn tkinter interface: a signed-distance-field rasteriser for the
icons, hand-measured layout, and per-monitor scaling arithmetic. The interface
is gone. What it did is now done by flexbox and inline SVG, and `core.py` is
the half that was never UI-shaped to begin with.

---

## License

MIT — see [LICENSE](LICENSE).

The pet sprites come from [vscode-pets](https://github.com/tonybaloney/vscode-pets) (MIT). Per-artist
attribution is in [app/assets/pets/CREDITS.md](app/assets/pets/CREDITS.md).
