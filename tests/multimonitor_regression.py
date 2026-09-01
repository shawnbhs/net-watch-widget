"""Windows integration regression for mixed-DPI scaling and free dragging.

Run with: python tests/multimonitor_regression.py
No registry, network, adapter, or display settings are changed.
"""
import os
import sys
from pathlib import Path

if not sys.platform.startswith("win"):
    print("SKIP: Windows mixed-DPI regression")
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

# RED 1: DPI awareness must be established before Tk creates the HWND.
created_after_dpi_init = []
real_tk = M.tk.Tk

def observed_tk(*args, **kwargs):
    created_after_dpi_init.append(bool(getattr(S, "_dpi_awareness_attempted", False)))
    return real_tk(*args, **kwargs)

M.tk.Tk = observed_tk
w = M.IPBar(run=False)
M.tk.Tk = real_tk
check(created_after_dpi_init == [True], "per-monitor DPI awareness initialized before Tk()")

def pump(count=8):
    for _ in range(count):
        w.root.update()

pump()

# RED 2: a release that changes scale must preserve the user's drop centre.
fake_monitor = {
    "name": "PRIMARY",
    "primary": True,
    "x": 0,
    "y": 0,
    "w": 2560,
    "h": 1600,
    "wx": 0,
    "wy": 0,
    "ww": 2560,
    "wh": 1540,
}
w._target_monitor = lambda: fake_monitor
real_physical_scale = S.physical_scale
S.physical_scale = lambda mon, target_ppi: 1.80
w._apply_scale(1.0, force=True)
w._base_px = None
w.root.geometry("+900+400")
pump()
before = (
    w.root.winfo_x() + w.root.winfo_width() / 2,
    w.root.winfo_y() + w.root.winfo_height() / 2,
)
w._de(type("Event", (), {})())
pump(12)
after = (
    w.root.winfo_x() + w.root.winfo_width() / 2,
    w.root.winfo_y() + w.root.winfo_height() / 2,
)
delta = (abs(after[0] - before[0]), abs(after[1] - before[1]))
check(max(delta) <= 3, f"drag release preserves drop centre (delta={delta}, before={before}, after={after})")
S.physical_scale = real_physical_scale

# RED 3: a monitor transition must not walk through ten visible sizes.
# One requested scale plus at most one measured correction is enough.
w._apply_scale(1.0, force=True)
S.physical_scale = lambda mon, target_ppi: 1.25
real_apply_scale = w._apply_scale
apply_calls = []
def counted_apply(scale, force=False):
    apply_calls.append((scale, force))
    return real_apply_scale(scale, force=force)
w._apply_scale = counted_apply
w._sync_scale()
check(len(apply_calls) <= 2,
      f"DPI transition uses at most two visible scale passes (calls={len(apply_calls)})")
w._apply_scale = real_apply_scale
S.physical_scale = real_physical_scale

# RED/guard 4: the title called out in the screenshots scales with the card.
def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)

w._apply_scale(1.0, force=True)
pump()
title = next(child for child in descendants(w.root)
             if child.winfo_class() == "Label" and child.cget("text") == "Net Watch")
title_h1 = title.winfo_reqheight()
w._apply_scale(2.0, force=True)
pump()
title_h2 = title.winfo_reqheight()
check(1.75 <= title_h2 / title_h1 <= 2.25,
      f"Net Watch title scales with card ({title_h1}px->{title_h2}px)")

# RED 5: modal controls must not fall back to TkDefaultFont.
w._history()
pump()
close_button = next(child for child in descendants(w.root)
                    if child.winfo_class() == "Button" and child.cget("text") == "Close")
check(str(close_button.cget("font")) == str(M.F_BTN),
      f"History Close button uses scalable F_BTN (actual={close_button.cget('font')})")
close_button.winfo_toplevel().destroy()

# RED/guard 6: footer canvas glyphs and rings both scale, not just the canvas.
w._apply_scale(1.0, force=True)
pump()
base = []
for circle in M._SCALABLE_CIRCLES:
    mark = circle.bbox(circle.mark)
    ring = circle.coords(circle.ring)
    base.append((mark, ring))
w._apply_scale(2.0, force=True)
pump()
for index, (circle, (mark1, ring1)) in enumerate(zip(M._SCALABLE_CIRCLES, base), start=1):
    mark2 = circle.bbox(circle.mark)
    ring2 = circle.coords(circle.ring)
    mw1, mh1 = mark1[2] - mark1[0], mark1[3] - mark1[1]
    mw2, mh2 = mark2[2] - mark2[0], mark2[3] - mark2[1]
    rd1 = ring1[2] - ring1[0]
    rd2 = ring2[2] - ring2[0]
    glyph_ok = 1.65 <= mw2 / max(1, mw1) <= 2.35 and 1.65 <= mh2 / max(1, mh1) <= 2.35
    ring_ok = 1.95 <= rd2 / max(1, rd1) <= 2.05
    check(glyph_ok and ring_ok, f"footer control {index} glyph/ring scale together: glyph {mw1}x{mh1}->{mw2}x{mh2}, ring {rd1}->{rd2}")

# RED 7: real data may widen the card after startup; keep it inside rcWork.
w._target_monitor = lambda: fake_monitor
w.root.geometry("613x1000+2057+253")
pump()
clamp = getattr(w, "_clamp_to_work_area", None)
if clamp is None:
    check(False, "post-refresh content growth is clamped to the monitor work area")
else:
    w._dragging = False
    clamp()
    pump()
    check(w.root.winfo_x() + w.root.winfo_width() <= fake_monitor["wx"] + fake_monitor["ww"],
          "post-refresh content growth is clamped to the monitor work area")
    before_drag = (w.root.winfo_x(), w.root.winfo_y())
    w._dragging = True
    w.root.geometry("+2057+253")
    pump()
    clamp()
    pump()
    check((w.root.winfo_x(), w.root.winfo_y()) == (2057, 253),
          "content clamp never fights an active drag")
    w._dragging = False
w.root.geometry("")

w.root.destroy()
failed = len(results) - sum(results)
print(f"RESULT {sum(results)} passed, {failed} failed")
sys.stdout.flush()
os._exit(1 if failed else 0)
