"""Equal-physical-size and placement regression across real monitors.

Windows-only: it reads the actual attached panels via EnumDisplayMonitors and
their true density from EDID, so it cannot run in CI. Run it on the machine.

Run: python tests/physical_size_regression.py
"""
import sys as _sys
if not _sys.platform.startswith("win"):
    print("SKIP: Windows-only physical sizing regression")
    raise SystemExit(0)

import sys,os,faulthandler
faulthandler.dump_traceback_later(180, exit=True)
from pathlib import Path as _P
D=str(_P(__file__).resolve().parents[1])
sys.path.insert(0,D); os.chdir(D)
import screen_geom as S, ip_bar as M
# Geometry test: silence the registry write, toasts and every polling thread.
M.reg_repoint=lambda *a,**k:None; M.toast=lambda *a,**k:None
S.watch_displays=lambda *a,**k:None
for meth in ("_tz_init","_net_adopt","_loop","_ai_loop","_ai_keepalive_loop","_hw_loop"):
    if hasattr(M.IPBar,meth): setattr(M.IPBar,meth,lambda self,*a,**k:None)
# This suite tests the EQUAL-PHYSICAL-SIZE engine. The per-monitor preference
# bias sits on top of that engine and is deliberately allowed to break equality,
# so neutralise it here; tests/primary_bias_regression.py owns the bias itself.
# Without this, a taste setting in .env silently fails a correctness test.
M.SCALE_PRIMARY=1.0; M.SCALE_SECONDARY=1.0
ok=0;bad=0
def chk(c,m):
    global ok,bad
    print(("[PASS] " if c else "[FAIL] ")+m)
    globals().__setitem__('ok',ok+1) if c else globals().__setitem__('bad',bad+1)

w=M.IPBar(run=False)
def pump(n=8):
    for _ in range(n):
        try: w.root.update()
        except Exception: pass
mons=S.monitors()
sam=[m for m in mons if m["w"]==1920][0]; lap=[m for m in mons if m["w"]==2560][0]

def move_to(mon):
    """Put the window inside a monitor, let it rescale, then let it re-park."""
    w.root.geometry(f'+{mon["wx"]+60}+{mon["wy"]+60}')
    w.root.update_idletasks(); pump(4)
    w._sync_scale(); pump(6)
    w._repos(); w.root.update_idletasks(); pump(6)

def size():
    return (w.root.winfo_width() or w.root.winfo_reqwidth(),
            w.root.winfo_height() or w.root.winfo_reqheight())

print("--- physical WIDTH on each panel (the thing the eye judges) ---")
res={}
for tag,mon in (("Samsung",sam),("Laptop",lap)):
    move_to(mon)
    W,H=size(); ppi=S.physical_ppi(mon)
    res[tag]=(W,H,ppi,W/ppi*2.54,w._scale,w.root.winfo_x(),w.root.winfo_y())
    print(f"   {tag:<9} scale={w._scale:5.3f}  {W}x{H}px  ppi={ppi:5.1f}  width={W/ppi*2.54:5.2f} cm")

a=res["Samsung"][3]; b=res["Laptop"][3]
chk(abs(a-b)<0.15, f"SAME PHYSICAL WIDTH: {a:.2f} vs {b:.2f} cm (delta {abs(a-b)*10:.1f} mm)")
chk(6.3<a<8.6, f"width {a:.2f} cm is in the comfortable band")
chk(res["Laptop"][4]>res["Samsung"][4], "denser panel gets the larger scale")
chk(res["Laptop"][0]>res["Samsung"][0], "and therefore more pixels")
chk(abs(res["Samsung"][4]-101.6/M.TARGET_PPI)<0.10, "Samsung scale near ppi/TARGET_PPI")
chk(abs(res["Laptop"][4]-167.4/M.TARGET_PPI)<0.12, "Laptop scale near ppi/TARGET_PPI")

print("--- parks bottom-RIGHT of whichever panel it is on ---")
for tag,mon in (("Samsung",sam),("Laptop",lap)):
    move_to(mon)
    W,H=size(); gx,gy=w.root.winfo_x(),w.root.winfo_y()
    rg=(mon["wx"]+mon["ww"])-(gx+W); bg=(mon["wy"]+mon["wh"])-(gy+H)
    px,py=w._scaled_pad()
    print(f"   {tag:<9} at ({gx},{gy}) {W}x{H}  gap right={rg} bottom={bg}  (pad {px},{py})")
    chk(abs(rg-px)<=2, f"{tag}: right gap equals the scaled pad")
    chk(abs(bg-py)<=2, f"{tag}: bottom gap equals the scaled pad")
    chk(S.rect_visible_on(gx,gy,W,H), f"{tag}: fully on screen")
    chk(gx>=mon["wx"] and gy>=mon["wy"], f"{tag}: inside its own monitor")

print("--- no drift over repeated monitor changes ---")
move_to(sam); s1=w._scale; f1={n:f.cget("size") for n,f in w._fonts.items()}; w1=size()[0]
for _ in range(8): move_to(lap); move_to(sam)
chk(abs(w._scale-s1)<1e-9, "scale identical after 8 round trips")
chk(f1=={n:f.cget("size") for n,f in w._fonts.items()}, "font sizes identical after 8 round trips")
chk(size()[0]==w1, f"pixel width identical after 8 round trips ({w1}px)")
for _ in range(12): w._sync_scale()
chk(abs(w._scale-s1)<1e-9, "12 redundant syncs are a no-op")

print("--- bars ---")
nb=len(M._SCALABLE_BARS); chk(nb>0, f"{nb} bars registered")
move_to(lap); c,bw,bh=M._SCALABLE_BARS[0]; got=int(c.cget("width"))
print(f"   bar authored {bw}px -> {got}px at scale {w._scale:.3f}")
chk(abs(got-bw*w._scale)<=3, "bar width tracks the scale")

print("--- guards ---")
w._apply_scale(9999); chk(0.6<=w._scale<=4.0, "absurd scale clamped")
w._apply_scale(-5);   chk(0.6<=w._scale<=4.0, "negative scale clamped")
move_to(sam)

print("--- compact mode gets its own constant physical size ---")
move_to(sam)
full_sam=size()[0]
w._tog_cmp(); pump(6); w._sync_scale(); pump(8); w._repos(); pump(6)
c_sam=size()[0]; c_sam_cm=c_sam/S.physical_ppi(sam)*2.54
move_to(lap); c_lap=size()[0]; c_lap_cm=c_lap/S.physical_ppi(lap)*2.54
print(f"   compact  Samsung {c_sam}px = {c_sam_cm:.2f} cm | Laptop {c_lap}px = {c_lap_cm:.2f} cm")
chk(abs(c_sam_cm-c_lap_cm)<0.20, f"compact mode also matches physically (delta {abs(c_sam_cm-c_lap_cm)*10:.1f} mm)")
chk(c_sam!=full_sam, "compact really is a different layout")
w._tog_cmp(); pump(6); w._sync_scale(); pump(8)
move_to(sam)
chk(abs(size()[0]-full_sam)<=4, f"toggling back restores the full width ({full_sam} -> {size()[0]})")

print("--- no runaway growth ---")
move_to(sam); start=size()[0]
for _ in range(15): w._sync_scale(); pump(3)
chk(size()[0]==start, f"15 syncs in place do not grow the widget ({start} -> {size()[0]})")
for _ in range(4):
    w._tog_cmp(); pump(4); w._sync_scale(); pump(4)
    w._tog_cmp(); pump(4); w._sync_scale(); pump(4)
move_to(sam)
chk(abs(size()[0]-start)<=4, f"4 mode round trips do not grow it ({start} -> {size()[0]})")

print(f"\nRESULT {ok} passed, {bad} failed")
sys.stdout.flush()
os._exit(1 if bad else 0)
