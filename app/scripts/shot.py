"""Launch the widget and screenshot it. A development tool, not part of the app.

    python app/scripts/shot.py out.png [seconds] [--shot] [--compact]

Photographing a transparent always-on-top window is fiddly enough to be worth a
script, for three reasons each of which cost an hour to find:

* A plain screen BitBlt omits layered windows, and a transparent window is
  layered, so the capture must pass CAPTUREBLT. .NET's CopyFromScreen sets that
  flag internally, which is why a PowerShell capture sees what a naive one does
  not.
* A PowerShell process is not per-monitor DPI aware, so on a scaled display its
  coordinates land on the wrong monitor. Capturing from this already-aware
  process removes the mismatch.
* While the workstation is locked, every screen capture returns the lock screen
  image. A run that photographs bare wallpaper usually means exactly that, not a
  broken window -- check with `--shot`, which renders the page directly and works
  regardless.

`--shot` and `--compact` are passed through to the Electron app: the first dumps
the rendered page to page.png composited over an opaque colour, the second
clicks the compact toggle before capturing.
"""
import ctypes
import ctypes.wintypes as wt
import os
import struct
import subprocess
import sys
import threading
import time
import zlib

u = ctypes.windll.user32
g = ctypes.windll.gdi32
ctypes.windll.shcore.SetProcessDpiAwareness(2)

OUT = sys.argv[1] if len(sys.argv) > 1 else "ui.png"
WAIT = float(sys.argv[2]) if len(sys.argv) > 2 else 13.0
EXTRA = sys.argv[3:]

SRCCOPY = 0x00CC0020
# Without CAPTUREBLT a screen BitBlt silently omits layered windows, and a
# transparent Electron window is layered. .NET's CopyFromScreen sets this
# flag for you, which is why a PowerShell capture sees what this would miss.
CAPTUREBLT = 0x40000000


def capture(x, y, w, h, path):
    screen_dc = u.GetDC(0)
    mem_dc = g.CreateCompatibleDC(screen_dc)
    bmp = g.CreateCompatibleBitmap(screen_dc, w, h)
    g.SelectObject(mem_dc, bmp)
    g.BitBlt(mem_dc, 0, 0, w, h, screen_dc, x, y, SRCCOPY | CAPTUREBLT)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                    ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    hdr = BITMAPINFOHEADER()
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    hdr.biWidth = w
    hdr.biHeight = -h          # negative: top-down rows
    hdr.biPlanes = 1
    hdr.biBitCount = 32
    hdr.biCompression = 0

    buf = ctypes.create_string_buffer(w * h * 4)
    g.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(hdr), 0)
    g.DeleteObject(bmp)
    g.DeleteDC(mem_dc)
    u.ReleaseDC(0, screen_dc)

    # BGRA from GDI, RGB out, one filter byte per row for PNG.
    raw = bytearray()
    src = buf.raw
    for row in range(h):
        raw.append(0)
        off = row * w * 4
        line = src[off:off + w * 4]
        raw.extend(b"".join(bytes((line[i + 2], line[i + 1], line[i]))
                            for i in range(0, len(line), 4)))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


def find(title):
    out = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(h, _):
        if not u.IsWindowVisible(h):
            return True
        n = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(h, n, 256)
        if n.value == title:
            r = wt.RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            out.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    u.EnumWindows(CB(cb), 0)
    return out


def main():
    env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    exe = os.path.join(root, "node_modules", "electron", "dist", "electron.exe")
    proc = subprocess.Popen([exe, "."] + EXTRA, cwd=root,
                            env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")

    def pump():
        for line in proc.stdout:
            text = line.rstrip()
            if text and "Security Warning" not in text:
                print("el:", text[:200])

    threading.Thread(target=pump, daemon=True).start()
    time.sleep(WAIT)

    found = find("Net Watch")
    if not found:
        print("no Net Watch window")
        proc.terminate()
        return 1

    x, y, w, h = found[0]
    pad = 36
    print("window", found[0])
    capture(x - pad, y - pad, w + pad * 2, h + pad * 2, os.path.abspath(OUT))
    print("shot", OUT)
    proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
