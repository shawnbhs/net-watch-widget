"""A fixture sidecar and a launcher for README screenshots. A development tool,
not part of the app.

    python app/scripts/demo_sidecar.py shoot full|compact|mini out.png

The README screenshots must not carry anyone's real network: the real sidecar
reports the machine's public and local addresses, gateway, ISP, ASN, DNS
servers and timezone. This file replaces it with fixed, made-up data --
documentation addresses (RFC 5737), "Example ISP", AS64500 (RFC 5398) -- and
runs a demo instance that cannot touch the real widget:

* Its own `--user-data-dir` in a temp folder, so the real widget's position,
  view mode and localStorage are never read or written, and the single-instance
  lock (which is per profile) does not collide with a widget already running.
* The sidecar is swapped without editing main.js. main.js spawns
  `$NET_WATCH_PYTHON <repo>/sidecar.py`; the launcher points that at a real
  python and puts a generated `sitecustomize.py` on PYTHONPATH which, when
  NW_DEMO_FIXTURE is set, runs this file as the fixture and exits before
  sidecar.py is ever executed.
* The mini bar's AI choice lives in localStorage (`nw.miniAi.v1`), so the
  launcher seeds it once over the DevTools protocol in the demo profile.

The capture itself is the app's own `--shot` path (main.js): it renders the
page and composites it over an opaque #2b3442, writing `page.png` to the repo
root. The launcher moves that file to `out.png`, crops it to the widget, and
kills the demo instance. The card frost is a separate DWM acrylic window and is
therefore not in a `--shot` render -- the opaque backdrop stands in for it.

Windows only. Electron is taken from NET_WATCH_ELECTRON, else the local mirror
at %LOCALAPPDATA%/net-watch/node_modules/electron (it cannot run from a UNC
path), else app/node_modules/electron.
"""
import json
import os
import sys
import threading
import time

# ── the fixture ───────────────────────────────────────────────────────────────

IP = "203.0.113.42"        # TEST-NET-3
LOCAL = "192.0.2.10"       # TEST-NET-1
GW = "192.0.2.1"
DNS = ["198.51.100.53 \U0001F1E9\U0001F1EA Germany", "198.51.100.54 \U0001F1E9\U0001F1EA Germany"]


def _clock(seconds=False):
    tm = time.localtime()
    h = tm.tm_hour % 12 or 12
    s = "am" if tm.tm_hour < 12 else "pm"
    return ("%d:%02d:%02d %s" % (h, tm.tm_min, tm.tm_sec, s)) if seconds \
        else ("%d:%02d %s" % (h, tm.tm_min, s))


def _messages(tick):
    wob = (tick % 5) - 2
    cpu, ram, gpu = 14 + wob, 47, 9 + (tick % 3)
    hist = {
        "cpu": [12, 18, 15, 22, 13, 11, 16, 19, 14, cpu],
        "ram": [46, 46, 47, 47, 47, 47, 47, 47, 47, ram],
        "gpu": [6, 8, 12, 9, 7, 10, 11, 8, 9, gpu],
    }
    return [
        {"t": "net", "ip": IP, "ip2": IP, "local": LOCAL, "gw": GW,
         "flag": "\U0001F1E9\U0001F1EA", "country": "Germany", "code": "DE",
         "flag2": "\U0001F1E9\U0001F1EA", "country2": "Germany", "code2": "DE",
         "isp": "Example ISP", "vpn": False, "changed": False,
         "ping1": "18ms", "ping2": "21ms", "loss1": 0, "loss2": 0,
         "ping1_ms": 18, "ping2_ms": 21, "clock": _clock(True)},
        {"t": "hw", "cpu": cpu, "ram": ram, "gpu": gpu, "gpu_raw": "%d%%" % gpu,
         "hist": hist},
        {"t": "tz", "tz": "W. Europe Standard Time", "suspect": False, "tz_code": "DE"},
        {"t": "netstate", "up": True, "busy": False},
        {"t": "hist", "items": [{"at": "2026-10-26 09:12:03", "ip": IP}]},
        {"t": "checks", "asn": "AS64500 Example ISP", "isp": "Example ISP",
         "proxy": False, "hosting": False, "host": "static-42.example.net",
         "dns": DNS, "dns_leak": False, "score": 100},
        {"t": "ai", "stale": False, "age": 0, "status": None, "off": False,
         "polled_at": _clock(), "retry": 0,
         "claude": {"err": "", "setup": False, "plan": "max",
                    "session_pct": 42, "session_reset": "2h 14m",
                    "week_pct": 23, "week_reset": "4d 6h",
                    "model_pct": 11, "model_reset": "4d 6h", "model_name": ""},
         "codex": {"err": "", "setup": False, "plan": "plus",
                   "sess_pct": 18, "sess_reset_ts": "3h 41m",
                   "week_pct": 61, "week_reset_ts": "2d 19h",
                   "limit_reached": False}},
    ]


def run_fixture():
    """Speak the sidecar's JSON-line protocol with fixed data until stdin closes."""
    out = sys.stdout
    lock = threading.Lock()
    stop = threading.Event()

    def emit(msg):
        with lock:
            try:
                out.write(json.dumps(msg, ensure_ascii=False) + "\n")
                out.flush()
            except Exception:
                stop.set()

    def stdin_loop():
        # Commands (style_pane, ai_refresh, quit, ...) are accepted and ignored;
        # EOF means Electron is gone.
        for line in sys.stdin:
            if '"quit"' in line:
                break
        stop.set()

    emit({"t": "hello", "pid": os.getpid(), "refresh": 3})
    emit({"t": "ai_providers", "providers": [], "err": "unavailable"})
    threading.Thread(target=stdin_loop, daemon=True).start()
    tick = 0
    while not stop.is_set():
        for m in _messages(tick):
            emit(m)
        if tick == 0:
            emit({"t": "ready"})
        tick += 1
        stop.wait(2)
    os._exit(0)


# ── the launcher ──────────────────────────────────────────────────────────────

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
ROOT = os.path.dirname(APP)
MINI_AI = [{"id": "claude", "week": True}, {"id": "codex", "week": True}]
BG = (0x2B, 0x34, 0x42)        # the --shot backdrop in main.js


def _electron():
    for p in (os.environ.get("NET_WATCH_ELECTRON"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "net-watch",
                           "node_modules", "electron", "dist", "electron.exe"),
              os.path.join(APP, "node_modules", "electron", "dist", "electron.exe")):
        if p and os.path.isfile(p):
            return p
    sys.exit("electron.exe not found; set NET_WATCH_ELECTRON")


def _python():
    exe = sys.executable
    return exe.replace("pythonw.exe", "python.exe")


def _env(work):
    shim = os.path.join(work, "shim")
    os.makedirs(shim, exist_ok=True)
    with open(os.path.join(shim, "sitecustomize.py"), "w", encoding="utf-8") as fh:
        fh.write("import os, runpy, sys\n"
                 "_p = os.environ.get('NW_DEMO_FIXTURE')\n"
                 "if _p:\n"
                 "    sys.argv = [_p, 'fixture']\n"
                 "    runpy.run_path(_p, run_name='__main__')\n"
                 "    os._exit(0)\n")
    env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
    env.update(NET_WATCH_PYTHON=_python(), PYTHONPATH=shim,
               NW_DEMO_FIXTURE=os.path.abspath(__file__))
    return env


def _launch(work, extra):
    import subprocess
    profile = os.path.join(work, "profile")
    log = open(os.path.join(work, "electron.log"), "a", encoding="utf-8")
    return subprocess.Popen([_electron(), APP, "--user-data-dir=" + profile] + extra,
                            cwd=work, env=_env(work), stdout=log, stderr=log)


def _kill(proc):
    import subprocess
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                   capture_output=True)


class _Cdp:
    """Just enough of a DevTools websocket client to evaluate one expression."""

    def __init__(self, port):
        import socket
        import urllib.request
        deadline = time.time() + 30
        while True:
            try:
                targets = json.load(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % port, timeout=2))
                page = next(t for t in targets if t.get("type") == "page"
                            and "index.html" in t.get("url", ""))
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        url = page["webSocketDebuggerUrl"]
        host, path = url[5:].split("/", 1)
        h, p = host.split(":")
        self.s = socket.create_connection((h, int(p)), timeout=10)
        key = "bndkZW1vLWtleS0xMjM0NQ=="
        self.s.sendall(("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
                        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, key)).encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.s.recv(1)
        self.n = 0

    def _send(self, text):
        import struct
        data = text.encode()
        mask = os.urandom(4)
        head = bytearray([0x81])
        ln = len(data)
        if ln < 126:
            head.append(0x80 | ln)
        elif ln < 65536:
            head.append(0x80 | 126); head += struct.pack(">H", ln)
        else:
            head.append(0x80 | 127); head += struct.pack(">Q", ln)
        self.s.sendall(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.s.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def _recv(self):
        import struct
        b0, b1 = self._recv_exact(2)
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._recv_exact(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._recv_exact(8))[0]
        return self._recv_exact(ln).decode("utf-8", "replace")

    def call(self, method, **params):
        self.n += 1
        self._send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self._recv())
            if msg.get("id") == self.n:
                return msg


def seed(work):
    """Write the mini bar's AI choice into the demo profile's localStorage."""
    port = 9347
    proc = _launch(work, ["--remote-debugging-port=%d" % port])
    try:
        cdp = _Cdp(port)
        expr = ("localStorage.setItem('nw.miniAi.v1', %s); localStorage.getItem('nw.miniAi.v1')"
                % json.dumps(json.dumps(MINI_AI)))
        r = cdp.call("Runtime.evaluate", expression=expr, returnByValue=True)
        print("seeded:", r.get("result", {}).get("result", {}).get("value"))
        # A clean quit flushes localStorage to disk; a kill may not.
        try:
            cdp.call("Runtime.evaluate", expression="setTimeout(() => window.close(), 50)")
        except Exception:
            pass
        try:
            proc.wait(timeout=15)
        except Exception:
            pass
    finally:
        if proc.poll() is None:
            _kill(proc)


def crop(src, dst, pad=14):
    """Crop to everything that is not the flat backdrop, with an even margin."""
    from PIL import Image
    im = Image.open(src).convert("RGB")
    w, h = im.size
    px = im.load()
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            if abs(r - BG[0]) + abs(g - BG[1]) + abs(b - BG[2]) > 18:
                xs.append(x); ys.append(y)
    if not xs:
        im.save(dst)
        return im.size
    box = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
    # Paste onto a fresh backdrop rather than cropping with a margin: the mini
    # bar's window is exactly as wide as its card, so a crop margin would be
    # cut off on those sides and the three images would not match.
    out = Image.new("RGB", (box[2] - box[0] + 2 * pad, box[3] - box[1] + 2 * pad), BG)
    out.paste(im.crop(box), (pad, pad))
    out.save(dst, optimize=True)
    return out.size


def shoot(view, out, work=None):
    work = work or os.path.join(os.environ.get("TEMP", "."), "nw-demo")
    os.makedirs(work, exist_ok=True)
    if not os.path.isdir(os.path.join(work, "profile", "Local Storage")):
        seed(work)
    flag = {"full": [], "compact": ["--compact"], "mini": ["--mini"]}[view]
    page = os.path.join(ROOT, "page.png")
    if os.path.exists(page):
        sys.exit("refusing to run: %s already exists" % page)
    proc = _launch(work, ["--shot", "--wait=1500"] + flag)
    try:
        deadline = time.time() + 40
        while not os.path.exists(page):
            if time.time() > deadline:
                sys.exit("no page.png within 40s; see %s" % os.path.join(work, "electron.log"))
            time.sleep(0.5)
        time.sleep(1.0)            # let the write finish
    finally:
        _kill(proc)
    raw = os.path.splitext(out)[0] + ".raw.png"
    import shutil
    shutil.move(page, raw)       # page.png is on the repo's drive, out may not be
    print(view, "->", out, crop(raw, out))


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "shoot":
        shoot(sys.argv[2], os.path.abspath(sys.argv[3]),
              sys.argv[4] if len(sys.argv) > 4 else None)
    else:
        run_fixture()
