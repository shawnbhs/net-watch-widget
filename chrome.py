"""The one Win32 call Electron does not expose: rounding a window.

This was a much larger module when the widget had a tkinter interface -- acrylic
accent policies, colour keys, backdrop capture, z-order helpers. The React build
gets its frost from Electron's own `setBackgroundMaterial`, so all of that went
with the UI. What could not go is the corner.

DWM will round a window *and* clip its acrylic backdrop to the same shape, but
only through `DwmSetWindowAttribute`, and Electron has no binding for it.
`SetWindowRgn`, the obvious alternative, was measured on this project and does
not clip an acrylic backdrop at all -- the frost stays square behind a rounded
card. So the Electron shell hands each backing window's handle to the Python
sidecar, which calls this. One API call is not worth a native Node module.

The radius is Windows' own and takes no parameter: roughly 8px, which is why the
cards are drawn at 8px rather than the design's 20px. The frost cannot be made
rounder, so the card is made to match it.
"""

import ctypes
import ctypes.wintypes as wt

DWMWA_WINDOW_CORNER_PREFERENCE = 33
CORNER_ROUND = 2
CORNER_ROUND_SMALL = 3


def hwnd_of(window):
    """The Win32 handle behind whatever was passed in.

    Accepts anything exposing `frame()`: that was a Tk toplevel once, and is now
    a small shim around the window handle Electron reports for a pane.
    """
    try:
        handle = window.frame()
        return int(handle, 16) if str(handle).startswith("0x") else int(handle)
    except Exception:
        return 0


def rounded(window, small=False):
    """Round a window's corners. A no-op before Windows 11."""
    hwnd = hwnd_of(window)
    if not hwnd:
        return False
    try:
        pref = ctypes.c_int(CORNER_ROUND_SMALL if small else CORNER_ROUND)
        res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wt.HWND(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref))
        return res == 0
    except Exception:
        # Undocumented enough to be worth surviving: an older build, or a
        # locked-down system. The cards then have square frost behind them,
        # which is how they looked before this existed.
        return False
