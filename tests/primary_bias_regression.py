"""RED: per-monitor size bias for the primary panel.

Reported behaviour: the widget is physically correct but subjectively too big
on a dense primary panel while reading fine on the secondary. Equal physical
size is the existing guarantee, so the fix is a per-monitor multiplier on top
of it -- not a change to TARGET_PPI, which would shrink BOTH screens.

Run: python tests/primary_bias_regression.py
"""
import os
import sys
from pathlib import Path

if not sys.platform.startswith("win"):
    print("SKIP: Windows-only sizing regression")
    raise SystemExit(0)

D = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, D)
os.chdir(D)

import screen_geom as S
import ip_bar as M

M.reg_repoint = lambda *a, **k: None
M.toast = lambda *a, **k: None
S.watch_displays = lambda *a, **k: None
for meth in ("_tz_init", "_net_adopt", "_loop", "_ai_loop", "_ai_keepalive_loop", "_hw_loop"):
    if hasattr(M.IPBar, meth):
        setattr(M.IPBar, meth, lambda self, *a, **k: None)

results = []


def check(condition, message):
    results.append(bool(condition))
    print(("[PASS] " if condition else "[FAIL] ") + message)


PRIMARY = {"name": "PRIM", "primary": True, "x": 0, "y": 0, "w": 2560, "h": 1600,
           "wx": 0, "wy": 0, "ww": 2560, "wh": 1540}
SECOND = {"name": "SEC", "primary": False, "x": -1920, "y": 0, "w": 1920, "h": 1080,
          "wx": -1920, "wy": 0, "ww": 1920, "wh": 1032}

real_physical_scale = S.physical_scale
S.physical_scale = lambda mon, target_ppi: 1.80 if mon["primary"] else 1.20

# RED 1: the bias hook must exist and default to a no-op, so an existing
# install that sets nothing keeps exactly today's equal-physical-size result.
target_scale = getattr(M, "target_scale", None)
check(callable(target_scale), "module exposes target_scale(mon)")
if not callable(target_scale):
    print("RESULT 0 passed, 1 failed")
    sys.stdout.flush()
    os._exit(1)

M.SCALE_PRIMARY = 1.0
M.SCALE_SECONDARY = 1.0
check(abs(target_scale(PRIMARY) - 1.80) < 1e-9,
      f"unbiased primary keeps physical scale ({target_scale(PRIMARY)})")
check(abs(target_scale(SECOND) - 1.20) < 1e-9,
      f"unbiased secondary keeps physical scale ({target_scale(SECOND)})")

# RED 2: the primary knob must move ONLY the primary panel.
M.SCALE_PRIMARY = 0.85
M.SCALE_SECONDARY = 1.0
check(abs(target_scale(PRIMARY) - 1.53) < 1e-9,
      f"primary bias applies to primary ({target_scale(PRIMARY)})")
check(abs(target_scale(SECOND) - 1.20) < 1e-9,
      f"primary bias leaves secondary untouched ({target_scale(SECOND)})")

# RED 3: a nonsense value must not produce an unreadable or giant widget.
for bad in (0.0, -3.0, 99.0, float("nan")):
    M.SCALE_PRIMARY = bad
    got = target_scale(PRIMARY)
    check(0.5 <= got / 1.80 <= 2.0,
          f"bias {bad!r} clamped into a sane range (scale={got})")

# RED 4: end to end -- a smaller bias must render a measurably smaller card
# on the primary, and the secondary must not move by more than rounding.
M.SCALE_PRIMARY = 1.0
M.SCALE_SECONDARY = 1.0
w = M.IPBar(run=False)


def pump(count=8):
    for _ in range(count):
        w.root.update()


pump()
w._base_px = None
w._target_monitor = lambda: PRIMARY
w._sync_scale()
pump()
prim_before = w.root.winfo_reqwidth()

w._target_monitor = lambda: SECOND
w._sync_scale()
pump()
sec_before = w.root.winfo_reqwidth()

M.SCALE_PRIMARY = 0.80
w._target_monitor = lambda: PRIMARY
w._sync_scale()
pump()
prim_after = w.root.winfo_reqwidth()

w._target_monitor = lambda: SECOND
w._sync_scale()
pump()
sec_after = w.root.winfo_reqwidth()

ratio = prim_after / max(1, prim_before)
check(0.74 <= ratio <= 0.86,
      f"primary card shrinks with the bias ({prim_before}px -> {prim_after}px, ratio {ratio:.3f})")
check(abs(sec_after - sec_before) <= 2,
      f"secondary card is unaffected by the primary bias ({sec_before}px -> {sec_after}px)")

S.physical_scale = real_physical_scale
w.root.destroy()
failed = len(results) - sum(results)
print(f"RESULT {sum(results)} passed, {failed} failed")
sys.stdout.flush()
os._exit(1 if failed else 0)
