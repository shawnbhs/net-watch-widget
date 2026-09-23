// Real, physical pixel density per monitor.
//
// Windows' scaleFactor is a *user preference*, not a measurement: two panels at
// 167 and 102 real PPI can both be set to 150%, so a sprite sized purely in CSS
// px is a different physical size on each one. The only trustworthy number is
// the panel's own EDID, which is burned into the display hardware and is
// untouched by DPI scaling.
//
//     const { ppiFor, refresh } = require('./ppi')
//     ppiFor(display)   // Electron Display -> real PPI, or 0 when unknown
//     refresh()         // force a re-probe; normally not needed, see below
//
// The module probes once at load and then subscribes itself to Electron's
// display-added / display-removed / display-metrics-changed events, so the
// cache follows the hardware without the caller having to remember anything.
// refresh() stays exported for tests, for diagnostics, and for a host that
// wants to force a read.
//
// Contract with the rest of the app: 0 means "unknown", never an error. The
// caller falls back to the scaleFactor-based correction. This module is loaded
// by the main process and its result is read while building the overlay
// payload, so it must never throw and must never block: a crash or a stall here
// takes the whole overlay down with it.

'use strict'

const { spawn } = require('node:child_process')

// Outside this range the EDID is lying (a projector reporting 0 mm, a VM
// inventing a 1 mm panel, a TV claiming 3 metres). Treat it as unknown rather
// than scaling a pet by a factor of forty.
const MIN_PPI = 30
const MAX_PPI = 800

// The two sides round independently: Electron's DIP bounds times scaleFactor is
// a float, Windows' dmPosition is an exact integer. Origins cannot collide
// (monitors may not overlap on the virtual desktop), so a couple of pixels of
// slack costs nothing and absorbs the rounding.
const ORIGIN_TOLERANCE_PX = 2

const PROBE_TIMEOUT_MS = 20000

// Overridable purely so the failure path can be exercised for real, and so a
// locked-down box can point at pwsh instead. Not a supported product setting.
const PS_EXE = process.env.NW_PPI_POWERSHELL || 'powershell.exe'

// Keyed by "<deviceX>,<deviceY>" -- the monitor's device-pixel origin on the
// virtual desktop. display.id is NOT usable as a key: it is an opaque Chromium
// value that changes when a monitor is replugged, so a cache keyed on it goes
// silently stale exactly when the user rearranges their screens. The origin is
// the one identifier both Electron and Win32 agree on exactly.
let cache = new Map()
let probing = false
let queued = false
let complained = false

/** One line of noise per process, no matter how often the probe fails. */
function complainOnce(what, err) {
  if (complained) return
  complained = true
  try {
    console.warn('[ppi] physical panel size unavailable (' + what + '): ' +
      ((err && err.message) || err) + ' -- falling back to scaleFactor')
  } catch {
    // Even logging is best-effort; there is nothing sensible left to do.
  }
}

// PowerShell source. String.raw so the many Windows backslashes survive: in a
// normal JS string literal `root\wmi` silently becomes `rootwmi`.
//
// Both halves of the probe run in ONE process: the EDID read (root\wmi, gives
// millimetres) and the desktop geometry read (Win32, gives the device-pixel
// origin that is the join key). Splitting them would let the display
// arrangement change between the two calls.
const PROBE_PS = String.raw`
$ErrorActionPreference = 'Stop'

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class NwDD {
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct DISPLAY_DEVICE {
    public int cb;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)]  public string DeviceName;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string DeviceString;
    public int StateFlags;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string DeviceID;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string DeviceKey;
  }
  [StructLayout(LayoutKind.Sequential)]
  public struct POINTL { public int x; public int y; }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct DEVMODE {
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmDeviceName;
    public ushort dmSpecVersion; public ushort dmDriverVersion; public ushort dmSize; public ushort dmDriverExtra;
    public uint dmFields; public POINTL dmPosition; public uint dmDisplayOrientation; public uint dmDisplayFixedOutput;
    public short dmColor; public short dmDuplex; public short dmYResolution; public short dmTTOption; public short dmCollate;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmFormName;
    public ushort dmLogPixels; public uint dmBitsPerPel; public uint dmPelsWidth; public uint dmPelsHeight;
    public uint dmDisplayFlags; public uint dmDisplayFrequency; public uint dmICMMethod; public uint dmICMIntent;
    public uint dmMediaType; public uint dmDitherType; public uint dmReserved1; public uint dmReserved2;
    public uint dmPanningWidth; public uint dmPanningHeight;
  }
  [DllImport("user32.dll", CharSet=CharSet.Unicode)]
  public static extern bool EnumDisplayDevices(string lpDevice, uint iDevNum, ref DISPLAY_DEVICE lpDisplayDevice, uint dwFlags);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)]
  public static extern bool EnumDisplaySettings(string lpszDeviceName, int iModeNum, ref DEVMODE lpDevMode);
  [DllImport("shcore.dll")]
  public static extern int SetProcessDpiAwareness(int value);
}
'@

# 2 = PROCESS_PER_MONITOR_DPI_AWARE, and it must happen before the first screen
# measurement. Without it Windows virtualises the coordinates it hands back -- a
# 2560x1600 panel at 150% reports 1707x1067 -- and the probe invents a scale
# difference that does not exist.
[void][NwDD]::SetProcessDpiAwareness(2)

function Get-NwString($arr) {
  if ($null -eq $arr) { return $null }
  $s = -join ($arr | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ })
  if ([string]::IsNullOrWhiteSpace($s)) { return $null } else { return $s.Trim() }
}

$edid = @{}

# Millimetres, straight out of the EDID detailed timing descriptor. This is the
# accurate source. WmiMonitorBasicDisplayParams below is CENTIMETRE resolution
# and its rounding is worth up to 0.8% -- enough to make horizontal and vertical
# PPI disagree by 1.6% on a panel whose pixels are provably square.
Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorListedSupportedSourceModes -ErrorAction SilentlyContinue | ForEach-Object {
  $modes = $_.MonitorSourceModes
  $idx = [int]$_.PreferredMonitorSourceModeIndex
  if ($null -ne $modes -and $idx -ge 0 -and $idx -lt $modes.Count) {
    $p = $modes[$idx]
    if ($p.HorizontalImageSize -gt 0 -and $p.VerticalImageSize -gt 0) {
      $k = ($_.InstanceName -replace '_\d+$', '').ToUpperInvariant()
      $edid[$k] = @{
        mmW = [double]$p.HorizontalImageSize
        mmH = [double]$p.VerticalImageSize
        pxW = [int]$p.HorizontalActivePixels
        pxH = [int]$p.VerticalActivePixels
      }
    }
  }
}

# Coarse fallback, only for panels the preferred-mode read missed entirely.
Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorBasicDisplayParams -ErrorAction SilentlyContinue | ForEach-Object {
  $k = ($_.InstanceName -replace '_\d+$', '').ToUpperInvariant()
  if (-not $edid.ContainsKey($k) -and $_.MaxHorizontalImageSize -gt 0 -and $_.MaxVerticalImageSize -gt 0) {
    $edid[$k] = @{
      mmW = [double]$_.MaxHorizontalImageSize * 10.0
      mmH = [double]$_.MaxVerticalImageSize * 10.0
      pxW = 0
      pxH = 0
    }
  }
}

$names = @{}
Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID -ErrorAction SilentlyContinue | ForEach-Object {
  $k = ($_.InstanceName -replace '_\d+$', '').ToUpperInvariant()
  $names[$k] = (Get-NwString $_.UserFriendlyName)
}

$rows = @()
$i = 0
while ($true) {
  $a = New-Object NwDD+DISPLAY_DEVICE
  $a.cb = [Runtime.InteropServices.Marshal]::SizeOf([type]'NwDD+DISPLAY_DEVICE')
  # [NullString]::Value, never $null: PowerShell marshals $null for a [string]
  # P/Invoke parameter as an EMPTY STRING, and EnumDisplayDevices("") returns
  # False with no devices at all.
  if (-not [NwDD]::EnumDisplayDevices([NullString]::Value, $i, [ref]$a, 0)) { break }
  $i++
  if (-not ($a.StateFlags -band 1)) { continue }   # DISPLAY_DEVICE_ACTIVE

  $dm = New-Object NwDD+DEVMODE
  $dm.dmSize = [uint16][Runtime.InteropServices.Marshal]::SizeOf([type]'NwDD+DEVMODE')
  if (-not [NwDD]::EnumDisplaySettings($a.DeviceName, -1, [ref]$dm)) { continue }  # -1 = ENUM_CURRENT_SETTINGS

  $j = 0
  while ($true) {
    $m = New-Object NwDD+DISPLAY_DEVICE
    $m.cb = [Runtime.InteropServices.Marshal]::SizeOf([type]'NwDD+DISPLAY_DEVICE')
    if (-not [NwDD]::EnumDisplayDevices($a.DeviceName, $j, [ref]$m, 1)) { break }
    $j++

    # DeviceID is the interface path form of the same PnP id WMI reports as
    # InstanceName: \\?\DISPLAY#BOE0CE4#4&102fce2&0&UID8388688#{guid}
    # -> DISPLAY\BOE0CE4\4&102fce2&0&UID8388688
    $parts = $m.DeviceID -split '#'
    if ($parts.Count -lt 3) { continue }
    $pnp = ('DISPLAY\' + $parts[1] + '\' + $parts[2]).ToUpperInvariant()

    $ppi = $null; $ppiH = $null; $ppiV = $null; $mmW = $null; $mmH = $null
    if ($edid.ContainsKey($pnp)) {
      $e = $edid[$pnp]
      $mmW = $e.mmW
      $mmH = $e.mmH
      # Prefer the EDID's own native mode; fall back to whatever mode is set
      # now, which is only wrong if the user is running a non-native mode.
      $pxW = if ($e.pxW -gt 0) { $e.pxW } else { [int]$dm.dmPelsWidth }
      $pxH = if ($e.pxH -gt 0) { $e.pxH } else { [int]$dm.dmPelsHeight }
      if ($pxW -gt 0 -and $pxH -gt 0 -and $mmW -gt 0 -and $mmH -gt 0) {
        $ppiH = $pxW / ($mmW / 25.4)
        $ppiV = $pxH / ($mmH / 25.4)
        $ppi  = [math]::Round((($ppiH + $ppiV) / 2.0), 3)
        $ppiH = [math]::Round($ppiH, 3)
        $ppiV = [math]::Round($ppiV, 3)
      }
    }

    $rows += [pscustomobject]@{
      x      = [int]$dm.dmPosition.x
      y      = [int]$dm.dmPosition.y
      w      = [int]$dm.dmPelsWidth
      h      = [int]$dm.dmPelsHeight
      ppi    = $ppi
      ppiH   = $ppiH
      ppiV   = $ppiV
      mmW    = $mmW
      mmH    = $mmH
      name   = $names[$pnp]
      pnp    = $pnp
      gdi    = $a.DeviceName
      primary = [bool]($a.StateFlags -band 4)
    }
  }
}

ConvertTo-Json -InputObject @($rows) -Depth 3 -Compress
`.trim()

function originKey(x, y) {
  return x + ',' + y
}

/** Device-pixel origin of an Electron display, the join key with Win32. */
function originOf(display) {
  const b = display && display.bounds
  if (!b || typeof b.x !== 'number' || typeof b.y !== 'number') return null
  const sf = typeof display.scaleFactor === 'number' && display.scaleFactor > 0
    ? display.scaleFactor
    : 1

  // Multiplying the DIP origin by this display's own scaleFactor is only
  // correct for the display containing the DIP origin. Chromium lays the
  // virtual desktop out in DIP by walking outwards from the primary, so a
  // secondary's DIP offset is accumulated through its NEIGHBOURS' scale
  // factors, not its own. Measured here: a panel really at device (-1920, 255)
  // is DIP (-1746, 154) at sf 1.10, and -1746 * 1.10 = -1921, 154 * 1.10 = 169.
  // The x is one pixel out and the y is eighty-six out -- far past any
  // tolerance, and it would silently match the wrong monitor.
  //
  // screen.dipToScreenRect does the full per-display walk and returns the exact
  // Win32 rectangle (verified: 0,0 and -1920,255). The multiply is kept only as
  // a fallback for when Electron's screen module is not reachable (this module
  // is also runnable under plain node for diagnostics).
  const exact = dipToScreen(b)
  if (exact) return { x: exact.x, y: exact.y, w: exact.width, h: exact.height }

  return {
    x: Math.round(b.x * sf),
    y: Math.round(b.y * sf),
    w: Math.round(b.width * sf),
    h: Math.round(b.height * sf),
  }
}

/** The display's device-pixel rect via Electron, or null outside Electron. */
function dipToScreen(rect) {
  try {
    // Required lazily: `electron` does not resolve under plain node, and this
    // module must stay loadable there for the standalone probe.
    const { screen } = require('electron')
    const out = screen.dipToScreenRect(null, rect)
    if (out && typeof out.x === 'number' && typeof out.y === 'number') return out
    return null
  } catch {
    return null
  }
}

function buildCache(rows) {
  const next = new Map()
  for (const row of rows) {
    if (!row || typeof row.x !== 'number' || typeof row.y !== 'number') continue
    const ppi = Number(row.ppi)
    if (!Number.isFinite(ppi) || ppi < MIN_PPI || ppi > MAX_PPI) continue
    next.set(originKey(row.x, row.y), {
      ppi,
      x: row.x,
      y: row.y,
      w: Number(row.w) || 0,
      h: Number(row.h) || 0,
      name: row.name || null,
      mmW: row.mmW || null,
      mmH: row.mmH || null,
    })
  }
  return next
}

/**
 * Run the probe. Resolves to a Map; never rejects -- a failed probe is an empty
 * result, not an exception, because every caller of this is on a path where
 * throwing would take the overlay down.
 *
 * `null` and an EMPTY MAP are different answers, and the difference is
 * load-bearing:
 *
 *   null       "I could not find out."   -> keep whatever was already known
 *   empty Map  "I looked; nothing here." -> forget it, and report unknown
 *
 * Collapsing the second into the first is what let a dead monitor's PPI outlive
 * it; see the comment on the `built.size === 0` branch below.
 */
function runProbe() {
  return new Promise((resolve) => {
    if (process.platform !== 'win32') {
      complainOnce('platform', new Error(process.platform + ' has no EDID probe'))
      resolve(null)
      return
    }

    let child
    let done = false
    const finish = (value, what, err) => {
      if (done) return
      done = true
      if (err) complainOnce(what, err)
      resolve(value)
    }

    try {
      // -EncodedCommand instead of a quoted -Command: the script contains
      // quotes, braces, dollars and a here-string, and every layer between here
      // and PowerShell would get a chance to mangle them.
      const encoded = Buffer.from(PROBE_PS, 'utf16le').toString('base64')
      child = spawn(PS_EXE, [
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-EncodedCommand', encoded,
      ], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    } catch (err) {
      finish(null, 'spawn', err)
      return
    }

    const timer = setTimeout(() => {
      try { child.kill() } catch { /* already gone */ }
      finish(null, 'timeout', new Error('probe exceeded ' + PROBE_TIMEOUT_MS + 'ms'))
    }, PROBE_TIMEOUT_MS)
    if (typeof timer.unref === 'function') timer.unref()

    let out = ''
    let errText = ''
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (d) => { out += d })
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (d) => { errText += d })
    child.on('error', (err) => {
      clearTimeout(timer)
      finish(null, 'spawn', err)
    })
    child.on('close', (code) => {
      clearTimeout(timer)
      if (done) return
      if (code !== 0) {
        finish(null, 'exit ' + code, new Error(errText.trim().split('\n')[0] || 'no output'))
        return
      }
      try {
        const parsed = JSON.parse(out)
        // A single monitor makes ConvertTo-Json emit a bare object on some
        // PowerShell versions, not a one-element array.
        const rows = Array.isArray(parsed) ? parsed : [parsed]
        const built = buildCache(rows)
        if (built.size === 0) {
          // The probe RAN and answered honestly: these panels have no usable
          // EDID. That is a real, current answer -- not a failure to find out --
          // so it must replace the cache, not be discarded in favour of it.
          //
          // Returning null here (as this branch used to) meant that unplugging
          // a 167 PPI laptop and putting an EDID-less panel at the same
          // device-pixel origin left the dead panel's entry in the cache: the
          // new monitor matched origin (0,0) and ppiFor() answered 167 for a
          // panel that is really ~102, scaling every sprite on it by 1.64x. A
          // confidently wrong number is worse than the 0 the contract promises,
          // because the scaleFactor fallback is never reached to correct it.
          //
          // The warning is still worth printing -- "no EDID anywhere" usually
          // means a VM or an unusual driver -- but it is not an error, so an
          // empty Map is returned rather than null.
          complainOnce('no usable EDID',
            new Error('probe returned ' + rows.length + ' display(s), none with sane EDID'))
          finish(built, null, null)
          return
        }
        finish(built, null, null)
      } catch (err) {
        finish(null, 'parse', err)
      }
    })
  })
}

/**
 * Re-probe the hardware. Safe to call from a display event handler: it is
 * asynchronous, it never throws, and overlapping calls collapse into one
 * trailing re-probe rather than a pile of PowerShell processes (Windows fires
 * display-metrics-changed several times for one settings change).
 */
function refresh() {
  if (probing) {
    queued = true
    return
  }
  probing = true
  runProbe().then((built) => {
    // A failed probe (null) keeps the previous good cache: the panels did not
    // stop existing because WMI was busy. A probe that SUCCEEDED and found
    // nothing usable is an empty Map, which is truthy and does replace the
    // cache -- see runProbe's `built.size === 0` branch for why that
    // distinction is the whole point.
    if (built) cache = built
  }).catch((err) => {
    complainOnce('probe', err)
  }).finally(() => {
    probing = false
    if (queued) {
      queued = false
      refresh()
    }
  })
}

// ── keeping the cache current ───────────────────────────────────────────────
//
// The cache is keyed by device-pixel origin, so a rearranged or replugged
// monitor does not produce a WRONG number -- it produces no match, and the
// display falls back to the scaleFactor approximation. Safe, but permanent:
// without a re-probe that degradation lasts for the rest of the process's life,
// and the user sees it as "the pets were the right size until I unplugged the
// dock once".
//
// The one-shot probe at require time is therefore not enough, and this module
// subscribes itself rather than relying on a call from its consumer. The header
// documents refresh() as "call on display-added/removed/metrics-changed"; a
// contract that depends on every future caller remembering to honour it is a
// contract that silently rots, and it already had (nothing called it).
// Subscribing here also means the diagnostics path, the overlay and any later
// consumer all get a fresh cache without each having to rediscover this.
const RESUBSCRIBE_DELAY_MS = 2000

// Windows fires display-metrics-changed repeatedly while a settings dialog is
// open, and a monitor waking up reports its geometry before it is stable.
// refresh() already collapses CONCURRENT calls into one trailing probe, but a
// slow drip of events spaced further apart than one probe takes would still
// spawn one PowerShell per event. Coalesce on a timer too, and read the
// hardware once things have settled.
let settleTimer = null
function refreshSoon() {
  try {
    if (settleTimer) clearTimeout(settleTimer)
    settleTimer = setTimeout(() => {
      settleTimer = null
      refresh()
    }, RESUBSCRIBE_DELAY_MS)
    // Never hold the process open just to re-probe a display.
    if (typeof settleTimer.unref === 'function') settleTimer.unref()
  } catch (err) {
    complainOnce('schedule', err)
  }
}

let watching = false
let readyRetryArmed = false

/**
 * Subscribe to Electron's display events so the cache follows the hardware.
 *
 * Idempotent, and a no-op outside Electron (this file is also runnable under
 * plain node for diagnostics) or when the screen module is not yet reachable.
 * Like everything else here it must never throw: it is called at require time,
 * and a throw would make requiring the module fail.
 */
function watchDisplayChanges() {
  if (watching) return true

  let electron = null
  try {
    // Lazily required: `electron` does not resolve under plain node.
    electron = require('electron')
  } catch {
    return false                       // not Electron: diagnostics run, nothing to watch
  }
  if (!electron) return false

  // Everything from here touches the screen module, which throws rather than
  // returning falsy when it is used before `app.ready`. So the whole attempt is
  // guarded, and a failure schedules ONE retry on ready instead of giving up.
  try {
    const screen = electron.screen
    if (!screen || typeof screen.on !== 'function') throw new Error('screen module not usable yet')
    screen.on('display-added', refreshSoon)
    screen.on('display-removed', refreshSoon)
    screen.on('display-metrics-changed', refreshSoon)
    watching = true
    return true
  } catch (err) {
    const app = electron.app
    let ready = true
    try { ready = !app || typeof app.isReady !== 'function' || app.isReady() } catch { ready = true }
    if (!ready && typeof app.once === 'function' && !readyRetryArmed) {
      readyRetryArmed = true
      // Not a failure worth complaining about: being required before app.ready
      // is the normal case for a main-process module.
      try {
        app.once('ready', () => {
          watchDisplayChanges()
          // The display list is only trustworthy once the app is ready, so the
          // require-time probe may have run against nothing. Re-read it.
          refreshSoon()
        })
        return false
      } catch { /* fall through to the complaint below */ }
    }
    complainOnce('watch', err)
    return false
  }
}

/**
 * Real physical PPI of the panel this Electron display is showing on, or 0 when
 * it is unknown (probe not finished, no EDID, VM, non-Windows, corrupt data).
 *
 * Pure cache read. Callers use this while assembling the overlay payload, once
 * per display per update, so it does no I/O and cannot block.
 */
function ppiFor(display) {
  try {
    const origin = originOf(display)
    if (!origin || cache.size === 0) return 0

    const exact = cache.get(originKey(origin.x, origin.y))
    if (exact) return exact.ppi

    for (const entry of cache.values()) {
      if (Math.abs(entry.x - origin.x) <= ORIGIN_TOLERANCE_PX &&
          Math.abs(entry.y - origin.y) <= ORIGIN_TOLERANCE_PX) {
        return entry.ppi
      }
    }

    // Last resort: a unique size match. Only accepted when exactly one panel
    // has that device-pixel size -- two identical monitors must stay ambiguous
    // and report unknown rather than guess wrong by a factor of 1.64.
    if (origin.w > 0 && origin.h > 0) {
      let hit = null
      let count = 0
      for (const entry of cache.values()) {
        if (Math.abs(entry.w - origin.w) <= ORIGIN_TOLERANCE_PX &&
            Math.abs(entry.h - origin.h) <= ORIGIN_TOLERANCE_PX) {
          hit = entry
          count++
        }
      }
      if (count === 1 && hit) return hit.ppi
    }
    return 0
  } catch {
    // Unknown, not fatal. A malformed Display object must not reach the caller
    // as an exception.
    return 0
  }
}

/** Everything the probe currently knows. Diagnostics and tests only. */
function snapshot() {
  return Array.from(cache.values()).map((e) => ({ ...e }))
}

// Probe once at load so the first overlay payload has real numbers, and
// subscribe so it stays true afterwards. Failures are swallowed on both:
// requiring this module must never be able to fail.
try {
  refresh()
} catch (err) {
  complainOnce('startup', err)
}
try {
  watchDisplayChanges()
} catch (err) {
  complainOnce('watch', err)
}

module.exports = { ppiFor, refresh, snapshot, watchDisplayChanges }
