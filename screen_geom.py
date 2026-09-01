"""Multi-monitor geometry helpers for an always-on-top Tkinter desktop widget.

The problem this module exists to solve: a widget that remembers its window
position will happily restore that position onto a monitor that has since been
unplugged, leaving the window stranded at coordinates no display covers. The
user then has no way to grab it with the mouse. Everything here is built around
validating a saved rectangle against the monitor layout that exists *right now*
and relocating it to a known-reachable spot when it does not survive that check.

Windows desktop coordinates form a single virtual screen whose origin is the
top-left of the primary monitor. Secondary monitors placed to the left or above
the primary therefore have negative coordinates, so no code here may assume the
desktop starts at (0, 0) or that widths and heights are positive offsets from it.

Pure stdlib plus ctypes. Non-Windows platforms degrade to an empty monitor list
instead of raising, so a caller can import this module anywhere.
"""

from __future__ import annotations

import sys
import threading
from typing import Callable, Optional

__all__ = [
    "monitors",
    "primary",
    "rect_visible_on",
    "bottom_left",
    "bottom_right",
    "monitor_at",
    "dpi_for_monitor",
    "scale_at",
    "scale_for",
    "physical_ppi",
    "physical_scale",
    "physical_scale_at",
    "REFERENCE_PPI",
    "initialize_dpi_awareness",
    "clamp_to_visible",
    "watch_displays",
    "set_enum_override",
]

_IS_WINDOWS = sys.platform.startswith("win")

# Tests and callers that need to simulate a display being unplugged install a
# replacement enumerator here rather than touching the real display config,
# which would be destructive on a live desktop.
_enum_override: Optional[Callable[[], list]] = None

_dpi_awareness_attempted = False


def set_enum_override(func: Optional[Callable[[], list]]) -> None:
    """Install (or clear with None) a stand-in for the OS monitor enumerator.

    This exists so a monitor hot-unplug can be exercised deterministically. The
    alternative, actually changing display settings, is unacceptable on a
    machine someone is using, and hardware-dependent tests do not run in CI.
    """
    global _enum_override
    _enum_override = func


def initialize_dpi_awareness() -> None:
    """Opt this process into physical-pixel coordinates, tolerating failure.

    Without DPI awareness Windows virtualises coordinates for the process, so a
    saved position recorded on a 150% scaled monitor comes back scaled against
    a different monitor and the visibility check silently compares mismatched
    coordinate spaces. The call must be defensive: it is unavailable on older
    Windows, and it hard-fails if awareness was already set by the host process
    or an embedding runtime, neither of which is an error for us.
    """
    global _dpi_awareness_attempted
    if _dpi_awareness_attempted or not _IS_WINDOWS:
        return
    _dpi_awareness_attempted = True

    import ctypes

    # Per-monitor v2 is the only mode where GetMonitorInfoW reports true
    # physical pixels on a mixed-DPI desktop; the older system-wide flag is a
    # fallback for Windows versions that lack the newer entry point.
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _enum_monitors_win() -> list:
    """Enumerate physical monitors through EnumDisplayMonitors/GetMonitorInfoW.

    GetSystemMetrics cannot express a multi-monitor layout and Tkinter's
    winfo_screenwidth only ever describes the primary display, so the raw
    Win32 enumeration is the only source that reports per-monitor bounds and
    the taskbar-excluded working area we need for placement.
    """
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", ctypes.c_wchar * 32),
        ]

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(RECT),
        ctypes.c_double,
    )

    MONITORINFOF_PRIMARY = 0x1
    user32 = ctypes.windll.user32
    found: list = []

    def _callback(hmonitor, hdc, lprect, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(ctypes.c_void_p(hmonitor), ctypes.byref(info)):
            m = info.rcMonitor
            w = info.rcWork
            found.append(
                {
                    "name": info.szDevice,
                    "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
                    "x": int(m.left),
                    "y": int(m.top),
                    "w": int(m.right - m.left),
                    "h": int(m.bottom - m.top),
                    "wx": int(w.left),
                    "wy": int(w.top),
                    "ww": int(w.right - w.left),
                    "wh": int(w.bottom - w.top),
                }
            )
        # Returning non-zero continues enumeration; stopping early would hide
        # monitors from every downstream check.
        return 1

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_callback), 0)
    return found


def monitors() -> list:
    """Return one dict per attached monitor describing bounds and working area.

    Keys are name, primary, x/y/w/h (full bounds) and wx/wy/ww/wh (working area,
    which excludes the taskbar and other appbars). Coordinates are virtual-screen
    physical pixels and may be negative for monitors left of or above the primary.

    Returns an empty list on non-Windows platforms, and also if the Win32
    enumeration fails for any reason, because a geometry helper crashing at
    import or at widget startup is far worse than the caller falling back to a
    default position.
    """
    if _enum_override is not None:
        try:
            return list(_enum_override())
        except Exception:
            return []
    if not _IS_WINDOWS:
        return []
    initialize_dpi_awareness()
    try:
        return _enum_monitors_win()
    except Exception:
        return []


def primary() -> Optional[dict]:
    """Return the primary monitor dict, or None when no monitors are known.

    Windows normally flags exactly one monitor primary, but the flag can be
    momentarily absent while a display change is in flight. Rather than return
    None in that window, and have a widget place itself nowhere, fall back to
    the monitor nearest the virtual-screen origin, which is where the primary
    sits by definition of the coordinate system.
    """
    mons = monitors()
    if not mons:
        return None
    for m in mons:
        if m.get("primary"):
            return m
    return min(mons, key=lambda m: (abs(m["x"]) + abs(m["y"])))


def rect_visible_on(x: int, y: int, w: int, h: int, min_visible: int = 80) -> bool:
    """Report whether the rectangle is grabbable by the user right now.

    A rectangle counts as visible only when its intersection with some single
    monitor's working area is at least min_visible pixels wide *and* tall. Any
    weaker test passes windows that hang a sliver onscreen, which is exactly the
    state a user cannot rescue with the mouse. The intersection is measured
    per monitor rather than summed across monitors because two separate slivers
    on two displays are no easier to grab than one.

    Uses the working area, not the full bounds, so a window whose only overlap
    is underneath the taskbar is correctly rejected.
    """
    for m in monitors():
        overlap_w = min(x + w, m["wx"] + m["ww"]) - max(x, m["wx"])
        overlap_h = min(y + h, m["wy"] + m["wh"]) - max(y, m["wy"])
        if overlap_w >= min_visible and overlap_h >= min_visible:
            return True
    return False


def bottom_left(w: int, h: int, pad_x: int = 16, pad_y: int = 52) -> tuple:
    """Return the (x, y) that parks a w by h window bottom-left on the primary.

    The primary monitor is the one guaranteed to exist and to be in front of the
    user, so it is the only safe recovery target. pad_y defaults large enough to
    clear a taskbar that the working area does not account for, which happens
    with auto-hide taskbars and some third-party docks that reserve no space.

    Falls back to a small positive offset when no monitor could be enumerated,
    since (0, 0) on an unknown layout is still more likely onscreen than the
    stale saved coordinates that got us here.
    """
    p = primary()
    if p is None:
        return (pad_x, pad_y)
    x = p["wx"] + pad_x
    y = p["wy"] + p["wh"] - h - pad_y
    # A window taller than the working area would otherwise be placed above the
    # top edge, hiding its title bar and drag handle.
    if y < p["wy"]:
        y = p["wy"]
    return (int(x), int(y))


def clamp_to_visible(
    x: int, y: int, w: int, h: int, pad_x: int = 16, pad_y: int = 52
) -> tuple:
    """Validate a remembered position and relocate it only when unreachable.

    This is the startup entry point. A position that is still usable is returned
    byte-identical so the widget does not creep across the desktop each launch
    or get yanked off a secondary monitor the user deliberately chose. Only when
    the saved rectangle no longer meaningfully intersects any working area, the
    unplugged-monitor case, does it move, and then to the primary bottom-left.
    """
    if rect_visible_on(x, y, w, h):
        return (x, y)
    return bottom_left(w, h, pad_x=pad_x, pad_y=pad_y)


def _layout_signature() -> tuple:
    """Reduce the layout to a comparable value covering count and geometry.

    Resolution changes and monitor rearrangement strand a window just as
    effectively as an unplug, so the signature includes bounds and working area
    rather than only the monitor count.
    """
    return tuple(
        (m["name"], m["primary"], m["x"], m["y"], m["w"], m["h"],
         m["wx"], m["wy"], m["ww"], m["wh"])
        for m in monitors()
    )


def watch_displays(callback: Callable[[], None], interval: float = 2.0):
    """Poll for display layout changes and invoke callback when one occurs.

    Returns the started daemon thread. Polling is deliberate: the event-driven
    alternative is a hidden message-only window receiving WM_DISPLAYCHANGE, and
    running a second Win32 message pump alongside Tkinter's is a well-known
    source of deadlocks and lost events. A two second poll is imperceptible for
    a recovery that only matters after a cable is pulled.

    The thread is daemonised so it can never hold the widget's process open, and
    every exception, from the enumeration and from the callback alike, is
    swallowed. A monitor watcher must not be able to kill the application it is
    protecting, and it must keep watching after a transient failure during the
    unstable moment when Windows is reconfiguring displays.
    """
    stop = threading.Event()

    def _loop():
        try:
            previous = _layout_signature()
        except Exception:
            previous = None
        while not stop.wait(interval):
            try:
                current = _layout_signature()
            except Exception:
                continue
            if current != previous:
                previous = current
                try:
                    callback()
                except Exception:
                    pass

    thread = threading.Thread(target=_loop, name="watch_displays", daemon=True)
    # Exposed so a caller with an orderly shutdown can end the poll early;
    # daemon status already covers the abrupt case.
    thread.stop_event = stop
    thread.start()
    return thread


# --- Per-monitor DPI -------------------------------------------------------
#
# A process that declares itself per-monitor DPI aware is telling Windows "do
# not scale my windows, I will handle it". Windows takes that literally: the
# same window drawn at the same pixel dimensions covers noticeably less glass
# on a 144 DPI panel than on a 96 DPI one, so a widget authored against a
# 100% display turns into an unreadable postage stamp at 150%. The scale
# factor below is what the process owes back to Windows in return for that
# declaration.

REFERENCE_DPI = 96  # the DPI the widget's pixel constants were authored at


def dpi_for_monitor(mon: dict) -> int:
    """Effective DPI of one monitor, defaulting to 96 when unknowable.

    GetDpiForMonitor lives in shcore, which is Windows 8.1+. On anything older
    the whole desktop necessarily shares one DPI, so the reference value is not
    merely a safe fallback but the correct answer.
    """
    if not _IS_WINDOWS:
        return REFERENCE_DPI
    import ctypes
    try:
        hmon = _hmonitor_at(
            mon["x"] + mon["w"] // 2,
            mon["y"] + mon["h"] // 2,
        )
        if hmon is None:
            return REFERENCE_DPI
        dx = ctypes.c_uint()
        dy = ctypes.c_uint()
        # 0 == MDT_EFFECTIVE_DPI: the scaling the user actually chose, rather
        # than the panel's physical pixel density, which is what we want since
        # we are matching the size of everything else on their desktop.
        if ctypes.windll.shcore.GetDpiForMonitor(
            ctypes.c_void_p(hmon), 0, ctypes.byref(dx), ctypes.byref(dy)
        ) != 0:
            return REFERENCE_DPI
        return int(dx.value) or REFERENCE_DPI
    except Exception:
        return REFERENCE_DPI


def _hmonitor_at(x: int, y: int):
    """Raw HMONITOR handle covering a point, or the nearest one."""
    if not _IS_WINDOWS:
        return None
    import ctypes

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    try:
        MONITOR_DEFAULTTONEAREST = 2
        fn = ctypes.windll.user32.MonitorFromPoint
        fn.restype = ctypes.c_void_p
        return fn(_POINT(int(x), int(y)), MONITOR_DEFAULTTONEAREST)
    except Exception:
        return None


def monitor_at(x: int, y: int):
    """The monitor dict containing a point, or the nearest one.

    Nearest rather than None, because the caller is always asking in order to
    place or scale a window, and every such question has a useful answer even
    when the point is in the dead space between two mismatched monitors.
    """
    mons = monitors()
    if not mons:
        return None
    for m in mons:
        if m["x"] <= x < m["x"] + m["w"] and m["y"] <= y < m["y"] + m["h"]:
            return m

    def _dist(m):
        cx = min(max(x, m["x"]), m["x"] + m["w"] - 1)
        cy = min(max(y, m["y"]), m["y"] + m["h"] - 1)
        return (cx - x) ** 2 + (cy - y) ** 2

    return min(mons, key=_dist)


def scale_at(x: int, y: int) -> float:
    """Multiplier that keeps a window the same APPARENT size at this point."""
    m = monitor_at(x, y)
    if m is None:
        return 1.0
    return dpi_for_monitor(m) / float(REFERENCE_DPI)


def scale_for(mon: dict) -> float:
    """Multiplier for a specific monitor dict."""
    if not mon:
        return 1.0
    return dpi_for_monitor(mon) / float(REFERENCE_DPI)


def bottom_right(w: int, h: int, pad_x: int = 16, pad_y: int = 52,
                 mon: dict = None) -> tuple:
    """Return the (x, y) that parks a w by h window bottom-right on a monitor.

    Defaults to the primary monitor, which is the one guaranteed to exist and
    to be in front of the user, and is therefore the only safe target when
    rescuing a window off a display that just disappeared.

    Note that w and h must be the window's REAL pixel size on the target
    monitor. A caller that rescales for DPI has to rescale first and measure
    second, or the window hangs off the edge by exactly the scale factor.
    """
    p = mon or primary()
    if p is None:
        return (pad_x, pad_y)
    x = p["wx"] + p["ww"] - w - pad_x
    y = p["wy"] + p["wh"] - h - pad_y
    # Clamp rather than trust: a window wider or taller than the working area
    # would otherwise be pushed off the top-left, putting its drag handle out
    # of reach, which is the exact failure this module exists to prevent.
    if x < p["wx"]:
        x = p["wx"]
    if y < p["wy"]:
        y = p["wy"]
    return (int(x), int(y))


# --- Physical size ---------------------------------------------------------
#
# Windows' DPI scaling setting is a USER PREFERENCE, not a measurement. Two
# panels set to the same 100% can have very different pixel densities, so
# scaling a window by the DPI setting alone still leaves it physically larger
# on one screen than the other. The only way to make a widget occupy the same
# amount of GLASS everywhere is to divide by the panel's real pixels-per-inch,
# which comes from the monitor's EDID rather than from any display setting.
#
# This is also why a screenshot can never show the problem: a screenshot
# records pixels, and the widget is the same pixel count on every monitor.
# The difference only exists on the physical panel, in front of the eye.

REFERENCE_PPI = 96.0   # density the widget's pixel constants were authored at

_PPI_CACHE = {}
_EDID_CACHE = None


def _edid_sizes():
    """Map normalised PnP device key -> (width_cm, height_cm) from EDID.

    Queried once per process. The WMI call costs the better part of a second,
    which is fine at startup but unacceptable on a monitor-change event, and
    the physical dimensions of a panel obviously cannot change while it stays
    plugged in.
    """
    global _EDID_CACHE
    if _EDID_CACHE is not None:
        return _EDID_CACHE
    _EDID_CACHE = {}
    if not _IS_WINDOWS:
        return _EDID_CACHE
    import subprocess
    ps = (
        "Get-CimInstance -Namespace root\\wmi "
        "-ClassName WmiMonitorBasicDisplayParams | ForEach-Object "
        "{ \"{0}|{1}|{2}\" -f $_.InstanceName,"
        "$_.MaxHorizontalImageSize,$_.MaxVerticalImageSize }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return _EDID_CACHE
    for line in out.splitlines():
        if "|" not in line:
            continue
        inst, w, h = line.rsplit("|", 2)
        try:
            w = int(w.strip())
            h = int(h.strip())
        except ValueError:
            continue
        # A panel reporting 0 cm is reporting "I do not know", most often a
        # projector or a virtual display. Treated as absent so the caller
        # falls back rather than dividing by zero.
        if w <= 0 or h <= 0:
            continue
        _EDID_CACHE[_pnp_key(inst)] = (w, h)
    return _EDID_CACHE


def _pnp_key(s: str) -> str:
    """Reduce a device path to the vendor+instance tokens both APIs share.

    WMI reports   DISPLAY\\BOE0CE4\\4&102fce2&0&UID8388688_0
    EnumDisplayDevices reports
                  \\\\?\\DISPLAY#BOE0CE4#4&102fce2&0&UID8388688#{guid}

    Same monitor, two spellings. Normalising to BOE0CE4|4&102FCE2&0&UID8388688
    matches them exactly, which matters because guessing the pairing by
    aspect ratio silently mismatches any two panels of similar shape.
    """
    s = (s or "").upper().replace("#", "\\").strip("\\")
    parts = [p for p in s.split("\\") if p and not p.startswith("{")
             and p not in ("?", "DISPLAY")]
    if not parts:
        return ""
    if len(parts) >= 2:
        tail = parts[1].split("_")[0]
        return parts[0] + "|" + tail
    return parts[0]


def _device_key(device_name: str) -> str:
    """PnP key for a \\\\.\\DISPLAYn adapter name, via its attached monitor."""
    if not _IS_WINDOWS:
        return ""
    import ctypes
    from ctypes import wintypes

    class _DD(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD),
                    ("DeviceName", ctypes.c_wchar * 32),
                    ("DeviceString", ctypes.c_wchar * 128),
                    ("StateFlags", wintypes.DWORD),
                    ("DeviceID", ctypes.c_wchar * 128),
                    ("DeviceKey", ctypes.c_wchar * 128)]

    try:
        dd = _DD()
        dd.cb = ctypes.sizeof(_DD)
        EDD_GET_DEVICE_INTERFACE_NAME = 0x00000001
        if not ctypes.windll.user32.EnumDisplayDevicesW(
                ctypes.c_wchar_p(device_name), 0, ctypes.byref(dd),
                EDD_GET_DEVICE_INTERFACE_NAME):
            return ""
        return _pnp_key(dd.DeviceID)
    except Exception:
        return ""


def physical_ppi(mon: dict) -> float:
    """True pixels-per-inch of a monitor's panel, or a sane estimate.

    Falls back to the Windows DPI setting when EDID is unavailable (virtual
    displays, remote sessions, projectors). That fallback is worse but never
    absurd, and it degrades to exactly the old behaviour rather than to a
    window scaled by a garbage number.
    """
    if not mon:
        return REFERENCE_PPI
    name = mon.get("name", "")
    if name in _PPI_CACHE:
        return _PPI_CACHE[name]

    ppi = None
    try:
        sizes = _edid_sizes()
        key = _device_key(name)
        dims = sizes.get(key)
        if dims:
            import math
            diag_in = math.hypot(dims[0], dims[1]) / 2.54
            if diag_in > 1.0:
                ppi = math.hypot(mon["w"], mon["h"]) / diag_in
    except Exception:
        ppi = None

    if not ppi or not (30.0 < ppi < 800.0):
        # Outside that range the EDID is lying (some panels report millimetres
        # in a centimetre field). The DPI setting is the safer answer.
        ppi = float(dpi_for_monitor(mon))

    _PPI_CACHE[name] = ppi
    return ppi


def physical_scale(mon: dict, target_ppi: float = REFERENCE_PPI) -> float:
    """Multiplier giving a window the same PHYSICAL size on any panel.

    A widget authored at target_ppi and drawn at this scale covers the same
    number of centimetres on every monitor, which is the only definition of
    "same size" that matches what the user actually sees.
    """
    ppi = physical_ppi(mon)
    if not ppi:
        return 1.0
    return ppi / float(target_ppi or REFERENCE_PPI)


def physical_scale_at(x: int, y: int, target_ppi: float = REFERENCE_PPI) -> float:
    """physical_scale for whichever monitor contains a point."""
    return physical_scale(monitor_at(x, y), target_ppi)
