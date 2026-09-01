# pythonw started with an absolute UNC path does not reliably place the
# script's own directory on sys.path, and the sibling modules imported below
# live beside this file.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

import tkinter as tk
import tkinter.font as tkfont
import threading, time, urllib.request, urllib.error, urllib.parse, subprocess, json
import sys, os, winreg, ipaddress, socket, re, base64, shlex
import random as _rnd
import datetime as _dt
import psutil
from collections import deque

# Sibling modules rather than inline code, because both are testable without a
# display or a live network: the monitor logic was verified against injected
# layouts and the link logic against injected states, so neither fix required
# unplugging a real monitor or dropping a real connection to prove it works.
import screen_geom
import netfast


# ── Configuration ─────────────────────────────────────────────────────────────
# Everything machine-specific is read from a .env file beside this script, so
# the script itself holds no personal paths. Every key is optional: with no
# .env at all the widget still runs, using the defaults below.

def _load_env():
    """Read KEY=VALUE lines from .env next to this script into a dict.

    Blank lines, '#' comments and an optional leading 'export ' are ignored;
    values may be wrapped in single or double quotes. A missing or unreadable
    file yields an empty config rather than an error, because the widget must
    start for a first-time user who has configured nothing at all.
    """
    cfg = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.removeprefix("export ").partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                cfg[key.strip()] = value
    except OSError:
        pass
    return cfg


ENV = _load_env()


def env_str(key, default):
    """Config value from .env, else process environment, else default."""
    value = ENV.get(key, os.environ.get(key, "")).strip()
    return value or default


def env_float(key, default):
    """Numeric config value, falling back to the default when unparseable.

    A malformed number in a config file must not stop the widget from
    starting; a sizing knob that is slightly wrong is recoverable, a widget
    that refuses to launch is not.
    """
    raw = env_str(key, "")
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def _expand(path):
    """Expand both '~' and %ENVVAR% so .env accepts either Windows style."""
    return os.path.expanduser(os.path.expandvars(path))


def env_paths(key):
    """Semicolon-separated path list; variables expanded, empties dropped."""
    return [_expand(p.strip())
            for p in env_str(key, "").split(";") if p.strip()]


def cred_paths(key, default):
    """Configured credential locations first, then the standard one.

    Order is significant: _pick_cred walks this list and takes the first
    readable file, so a user's explicit .env entry must outrank the default.
    Duplicates are dropped so listing the default explicitly is harmless.
    """
    seen = {}
    for path in env_paths(key) + [default]:
        seen.setdefault(os.path.normcase(path), path)
    return list(seen.values())


# ── Constants ──────────────────────────────────────────────────────────────────
REFRESH   = 3
PING_N    = 2
# Corner margin, authored at TARGET_PPI and scaled per panel. Equal on both
# axes: the work area already excludes the taskbar, so an extra bottom margin
# just parks the widget in mid-air well above the corner it is meant to hug.
PAD_R     = 16
PAD_B     = 16
FLASH_N   = 8
FLASH_MS  = 150
HIST_MAX  = 10
LOG_FILE  = _expand(env_str(
    "LOG_FILE", os.path.join(os.path.expanduser("~"), "ip_bar_log.txt")))

PING_HOST_1 = env_str("PING_HOST_1", "1.1.1.1")
PING_HOST_2 = env_str("PING_HOST_2", "8.8.8.8")

# ── Apple-inspired dark system palette ────────────────────────────────────────
# Surfaces: true black canvas with elevated gray cards (Apple dark surfaces).
BG   = "#0e0e10"   # THE card surface: every section shares it, no striping
SURF = BG
CHR  = "#161618"   # chrome: title + footer bars
TRK  = "#3a3a3c"   # progress rail
HAIR = "#242426"   # 1px divider, horizontal and the vertical column split
STRK = "#3a3a3c"   # pill / footer-circle outline
HOV  = "#1e1e20"   # pill / circle hover fill

# Legacy aliases: the section surface is now uniform, so these all resolve
# to the single card colour rather than the old alternating stripes.
BG2  = SURF
BG3  = TRK
BORD = HAIR

# One chromatic accent. Everything interactive is this blue, nothing else is.
ACC  = "#0a84ff"   # Apple Blue (dark-mode variant of #0071e3)

# Semantic status only — never decorative.
GRN  = "#30d158"   # systemGreen  dark
YLW  = "#ffd60a"   # systemYellow dark
ORG  = "#ff9f0a"   # systemOrange dark
RED  = "#ff453a"   # systemRed    dark
TEA  = "#64d2ff"   # systemTeal   dark
PRP  = "#bf5af2"   # systemPurple dark

# Text ladder: primary / secondary / tertiary / quaternary (Apple label colors).
WHT  = "#f5f5f7"   # primary label
SEC  = "#aeaeb2"   # secondary label
MUT  = "#8e8e93"   # tertiary label
LBL  = "#636366"   # quaternary label — section captions

# Apple tracks tight at every size; Segoe UI is the closest system face here.
F_IP  = ("Segoe UI Semibold",10);  F_LBL = ("Segoe UI",7)
F_CTY = ("Segoe UI",8);            F_SM  = ("Segoe UI",7)
F_MN  = ("Cascadia Mono",8);       F_CMP = ("Segoe UI Semibold",9)
F_CMS = ("Segoe UI",7)
F_VAL = ("Segoe UI Semibold",8)    # numeric readouts
F_CAP = ("Segoe UI",7)             # captions / reset countdowns

# Tk exposes no OpenType 'tnum' feature, so a genuine monospace face is the
# only way to get tabular figures. Every number that refreshes uses these, in
# a fixed-width right-aligned label, so digits never jitter between polls.
F_NUM = ("Consolas",9)             # primary tabular readout (IP, latency)
F_NUS = ("Consolas",8)             # small tabular readout (percent, loss)

# Chrome faces. These used to be written inline at each call site as bare
# tuples, which quietly opted them out of DPI rescaling: only fonts listed in
# _FONT_NAMES become live objects, and a tuple's size is copied into the
# widget once and never revisited. Naming them here is what lets the title,
# the footer glyphs and the toast grow with the rest of the widget.
F_TTL = ("Segoe UI Semibold",9)   # card title
F_DOT = ("Segoe UI",7)            # status dot
F_GLY = ("Segoe UI",8)            # footer circle glyphs
F_BTN = ("Segoe UI",9)            # push button face
F_TST = ("Segoe UI Semibold",10)  # toast headline
F_LCK = ("Segoe MDL2 Assets",9)   # padlock glyph

# --- Physical sizing -------------------------------------------------------
#
# Every constant above is authored for a panel of TARGET_PPI pixels per inch.
# On a denser panel the same pixel count covers less glass, so the widget is
# rescaled at runtime by the ratio of the real panel density to this number
# (see IPBar._sync_scale). That keeps its physical size identical on every
# monitor instead of merely its pixel size, which is what the eye cares about.
#
# 84 rather than the conventional 96 because the widget is glanceable chrome
# read at arm's length, not a document: at 84 the primary readout subtends
# about 0.20 degrees at a 60 cm viewing distance, which is the usual comfort
# threshold, and the whole widget measures roughly 10 cm across on any screen.
TARGET_PPI = env_float("WIDGET_TARGET_PPI", 84.0)

# Equal physical size is the correct default, but it is not the whole story:
# the primary panel here is a large desk monitor viewed at arm's length while
# the secondary is a laptop screen read from closer, so a card that measures
# right on one reads as oversized on the other. These knobs bias the computed
# scale per monitor. Lowering TARGET_PPI instead would shrink BOTH screens,
# which is not what is being asked for.
SCALE_PRIMARY   = env_float("WIDGET_SCALE_PRIMARY", 1.0)
SCALE_SECONDARY = env_float("WIDGET_SCALE_SECONDARY", 1.0)

# A bias outside this band stops being a taste adjustment and becomes a
# broken widget -- illegible at the bottom, off the screen at the top.
_BIAS_MIN = 0.5
_BIAS_MAX = 2.0


def _clamp_bias(value):
    """Coerce a configured bias into a range that still renders.

    A typo in a config file must degrade to "slightly wrong size", never to a
    widget too small to read or too large to place. NaN fails every
    comparison, so it is rejected explicitly rather than by clamping.
    """
    try:
        bias = float(value)
    except (TypeError, ValueError):
        return 1.0
    if bias != bias:  # NaN
        return 1.0
    return max(_BIAS_MIN, min(_BIAS_MAX, bias))


def target_scale(mon):
    """Physical scale for a monitor, with the configured per-monitor bias.

    Kept separate from screen_geom.physical_scale because that function
    answers a question about the panel ("how many pixels is a centimetre
    here") while this one answers a question about preference ("how big should
    the card be on that panel").
    """
    scale = screen_geom.physical_scale(mon, TARGET_PPI)
    bias = SCALE_PRIMARY if (mon or {}).get("primary") else SCALE_SECONDARY
    return scale * _clamp_bias(bias)

_FONT_NAMES = ("F_IP","F_LBL","F_CTY","F_SM","F_MN","F_CMP","F_CMS",
               "F_VAL","F_CAP","F_NUM","F_NUS",
               "F_TTL","F_DOT","F_GLY","F_BTN","F_TST","F_LCK")
_SCALABLE_BARS = []
_SCALABLE_CIRCLES = []


def _capture_pads(root):
    """Record every authored padding in the widget tree, once.

    Fonts alone do not keep the widget a constant physical size: a large part
    of its width is fixed pixel padding, which stays put while the text grows
    and so pulls the proportions apart on a dense panel. Walking the tree
    beats registering each frame by hand -- a frame added later is picked up
    for free instead of silently staying unscaled.

    Returns a list of (widget, kind, {option: base_value}).
    """
    out = []
    stack = [root]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        conf = {}
        for opt in ("padx", "pady", "ipadx", "ipady", "borderwidth",
                    "highlightthickness", "wraplength"):
            try:
                val = w.cget(opt)
            except Exception:
                continue
            try:
                val = int(str(val))
            except (TypeError, ValueError):
                continue
            if val:
                conf[opt] = val
        if conf:
            out.append((w, "widget", conf))
        # Geometry-manager padding is separate from widget padding and just
        # as fixed; pack/grid both report it back as a string or tuple.
        try:
            mgr = w.winfo_manager()
            info = w.pack_info() if mgr == "pack" else (
                   w.grid_info() if mgr == "grid" else None)
        except Exception:
            info = None
        if info:
            geo = {}
            for opt in ("padx", "pady", "ipadx", "ipady"):
                val = info.get(opt)
                if val in (None, "", 0, "0"):
                    continue
                if isinstance(val, (list, tuple)):
                    try:
                        pair = tuple(int(str(v)) for v in val)
                    except (TypeError, ValueError):
                        continue
                    if any(pair):
                        geo[opt] = pair
                else:
                    try:
                        num = int(str(val))
                    except (TypeError, ValueError):
                        continue
                    if num:
                        geo[opt] = num
            if geo:
                out.append((w, mgr, geo))
    return out


def _scale_pads(pads, scale):
    """Re-apply every captured padding at `scale`, from the ORIGINAL values.

    Always scaling from the captured base, never from the current value, so
    repeated monitor changes cannot compound rounding error.
    """
    def grow(v):
        if isinstance(v, tuple):
            return tuple(max(0, int(round(x * scale))) for x in v)
        return max(0, int(round(v * scale)))

    for widget, kind, base in pads:
        try:
            if kind == "widget":
                widget.configure(**{k: grow(v) for k, v in base.items()})
            elif kind == "pack":
                widget.pack_configure(**{k: grow(v) for k, v in base.items()})
            elif kind == "grid":
                widget.grid_configure(**{k: grow(v) for k, v in base.items()})
        except Exception:
            # A widget destroyed by a mode switch must not take the rescale
            # down with it -- the rest of the tree still needs resizing.
            continue


def _register_scalable_bar(canvas, base_w, base_h):
    """Remember a Bar canvas and the size it was authored at.

    Recorded against the ORIGINAL constant rather than the current width, so
    repeated monitor changes always rescale from the authored value and cannot
    accumulate rounding drift.
    """
    _SCALABLE_BARS.append((canvas, base_w, base_h))


def _promote_fonts():
    """Swap the F_* tuples for live tkfont.Font objects, in place.

    Every widget here passes font=F_SOMETHING at construction. Tk copies a
    tuple's values into the widget, so mutating the tuple later changes
    nothing -- but it holds a REFERENCE to a font object, so reconfiguring
    that object restyles every widget already using it. Promoting the
    constants up front turns a DPI change into a handful of font
    reconfigurations instead of a teardown and rebuild of the whole UI.

    Called after a Tk root exists and before any widget is built.
    """
    out = {}
    g = globals()
    for name in _FONT_NAMES:
        spec = g[name]
        if isinstance(spec, tuple):
            # Positive Tk sizes are points, so their pixel size changes with
            # Tk's DPI context. Convert the 96-DPI authored point size to an
            # equivalent negative pixel size; our own per-monitor pass then
            # remains the single source of physical scaling.
            authored=spec[1]
            pixels=-max(1,int(round(abs(authored)*96/72))) if authored>0 else authored
            f = tkfont.Font(family=spec[0], size=pixels)
            for opt in spec[2:]:
                if opt in ("bold", "normal"):
                    f.configure(weight=opt)
                elif opt in ("italic", "roman"):
                    f.configure(slant=opt)
            g[name] = f
            out[name] = f
        else:
            out[name] = spec
    return out

TZ_CC = {
    "Iran Standard Time":"IR","Afghanistan Standard Time":"AF",
    "Arab Standard Time":"SA","Arabic Standard Time":"IQ",
    "Turkey Standard Time":"TR","Azerbaijan Standard Time":"AZ",
    "Pakistan Standard Time":"PK","GMT Standard Time":"GB",
    "US Eastern Standard Time":"US","Central Standard Time":"US",
    "Pacific Standard Time":"US","Mountain Standard Time":"US",
    "Central Europe Standard Time":"DE","W. Europe Standard Time":"DE",
    "Russian Standard Time":"RU","China Standard Time":"CN",
    "Tokyo Standard Time":"JP","Korea Standard Time":"KR",
    "India Standard Time":"IN","E. Australia Standard Time":"AU",
    "Romance Standard Time":"FR","Canada Central Standard Time":"CA",
}
SUSPECT = {"Iran Standard Time","Afghanistan Standard Time",
           "Arab Standard Time","Arabic Standard Time",
           "Azerbaijan Standard Time","Pakistan Standard Time"}

CNAMES = {
    "AF":"Afghanistan","AX":"Aland Islands","AL":"Albania","DZ":"Algeria",
    "AS":"American Samoa","AD":"Andorra","AO":"Angola","AI":"Anguilla",
    "AG":"Antigua and Barbuda","AR":"Argentina","AM":"Armenia","AW":"Aruba",
    "AU":"Australia","AT":"Austria","AZ":"Azerbaijan","BS":"Bahamas",
    "BH":"Bahrain","BD":"Bangladesh","BB":"Barbados","BY":"Belarus",
    "BE":"Belgium","BZ":"Belize","BJ":"Benin","BM":"Bermuda","BT":"Bhutan",
    "BO":"Bolivia","BA":"Bosnia and Herzegovina","BW":"Botswana","BR":"Brazil",
    "BN":"Brunei","BG":"Bulgaria","BF":"Burkina Faso","BI":"Burundi",
    "CV":"Cape Verde","KH":"Cambodia","CM":"Cameroon","CA":"Canada",
    "KY":"Cayman Islands","CF":"Central African Republic","TD":"Chad",
    "CL":"Chile","CN":"China","CO":"Colombia","KM":"Comoros","CG":"Congo",
    "CD":"DR Congo","CK":"Cook Islands","CR":"Costa Rica","CI":"Ivory Coast",
    "HR":"Croatia","CU":"Cuba","CW":"Curacao","CY":"Cyprus","CZ":"Czech Republic",
    "DK":"Denmark","DJ":"Djibouti","DM":"Dominica","DO":"Dominican Republic",
    "EC":"Ecuador","EG":"Egypt","SV":"El Salvador","GQ":"Equatorial Guinea",
    "ER":"Eritrea","EE":"Estonia","SZ":"Eswatini","ET":"Ethiopia",
    "FK":"Falkland Islands","FO":"Faroe Islands","FJ":"Fiji","FI":"Finland",
    "FR":"France","GF":"French Guiana","PF":"French Polynesia","GA":"Gabon",
    "GM":"Gambia","GE":"Georgia","DE":"Germany","GH":"Ghana","GI":"Gibraltar",
    "GR":"Greece","GL":"Greenland","GD":"Grenada","GP":"Guadeloupe","GU":"Guam",
    "GT":"Guatemala","GN":"Guinea","GW":"Guinea-Bissau","GY":"Guyana",
    "HT":"Haiti","VA":"Vatican","HN":"Honduras","HK":"Hong Kong","HU":"Hungary",
    "IS":"Iceland","IN":"India","ID":"Indonesia","IR":"Iran","IQ":"Iraq",
    "IE":"Ireland","IL":"Israel","IT":"Italy","JM":"Jamaica","JP":"Japan",
    "JO":"Jordan","KZ":"Kazakhstan","KE":"Kenya","KI":"Kiribati",
    "KP":"North Korea","KR":"South Korea","KW":"Kuwait","KG":"Kyrgyzstan",
    "LA":"Laos","LV":"Latvia","LB":"Lebanon","LS":"Lesotho","LR":"Liberia",
    "LY":"Libya","LI":"Liechtenstein","LT":"Lithuania","LU":"Luxembourg",
    "MO":"Macao","MG":"Madagascar","MW":"Malawi","MY":"Malaysia","MV":"Maldives",
    "ML":"Mali","MT":"Malta","MH":"Marshall Islands","MQ":"Martinique",
    "MR":"Mauritania","MU":"Mauritius","MX":"Mexico","FM":"Micronesia",
    "MD":"Moldova","MC":"Monaco","MN":"Mongolia","ME":"Montenegro",
    "MA":"Morocco","MZ":"Mozambique","MM":"Myanmar","NA":"Namibia","NR":"Nauru",
    "NP":"Nepal","NL":"Netherlands","NC":"New Caledonia","NZ":"New Zealand",
    "NI":"Nicaragua","NE":"Niger","NG":"Nigeria","NU":"Niue","NO":"Norway",
    "MK":"North Macedonia","OM":"Oman","PK":"Pakistan","PW":"Palau",
    "PS":"Palestine","PA":"Panama","PG":"Papua New Guinea","PY":"Paraguay",
    "PE":"Peru","PH":"Philippines","PL":"Poland","PT":"Portugal","PR":"Puerto Rico",
    "QA":"Qatar","RE":"Reunion","RO":"Romania","RU":"Russia","RW":"Rwanda",
    "KN":"Saint Kitts and Nevis","LC":"Saint Lucia","VC":"Saint Vincent",
    "WS":"Samoa","SM":"San Marino","ST":"Sao Tome and Principe",
    "SA":"Saudi Arabia","SN":"Senegal","RS":"Serbia","SC":"Seychelles",
    "SL":"Sierra Leone","SG":"Singapore","SX":"Sint Maarten","SK":"Slovakia",
    "SI":"Slovenia","SB":"Solomon Islands","SO":"Somalia","ZA":"South Africa",
    "SS":"South Sudan","ES":"Spain","LK":"Sri Lanka","SD":"Sudan",
    "SR":"Suriname","SE":"Sweden","CH":"Switzerland","SY":"Syria",
    "TW":"Taiwan","TJ":"Tajikistan","TZ":"Tanzania","TH":"Thailand",
    "TL":"Timor-Leste","TG":"Togo","TO":"Tonga","TT":"Trinidad and Tobago",
    "TN":"Tunisia","TR":"Turkey","TM":"Turkmenistan","TV":"Tuvalu",
    "UG":"Uganda","UA":"Ukraine","AE":"UAE","GB":"United Kingdom",
    "US":"United States","UY":"Uruguay","UZ":"Uzbekistan","VU":"Vanuatu",
    "VE":"Venezuela","VN":"Vietnam","VG":"British Virgin Islands",
    "VI":"US Virgin Islands","WF":"Wallis and Futuna","EH":"Western Sahara",
    "YE":"Yemen","ZM":"Zambia","ZW":"Zimbabwe","AQ":"Antarctica",
    "GG":"Guernsey","IM":"Isle of Man","JE":"Jersey","SH":"Saint Helena",
    "PM":"Saint Pierre and Miquelon","TC":"Turks and Caicos","CX":"Christmas Island",
    "PN":"Pitcairn","TK":"Tokelau","NF":"Norfolk Island","IO":"British Indian Ocean Territory",
}

IRAN_RAW = """
2.57.3.0/24
2.144.0.0/14
2.176.0.0/12
5.1.43.0/24
5.22.0.0/17
5.22.192.0/21
5.22.200.0/22
5.23.112.0/21
5.34.192.0/20
5.42.217.0/24
5.42.223.0/24
5.52.0.0/16
5.53.32.0/19
5.56.128.0/22
5.56.132.0/22
5.57.32.0/21
5.61.24.0/21
5.62.160.0/19
5.62.192.0/18
5.63.8.0/21
5.63.23.0/24
5.72.0.0/16
5.73.0.0/16
5.74.0.0/16
5.75.0.0/17
5.104.208.0/21
5.106.0.0/16
5.112.0.0/12
5.134.128.0/18
5.134.192.0/21
5.144.128.0/21
5.145.112.0/21
5.159.48.0/21
5.159.192.0/24
5.182.44.0/22
5.190.0.0/16
5.198.160.0/19
5.200.64.0/19
5.200.96.0/19
5.200.128.0/17
5.201.128.0/18
5.201.192.0/18
5.202.0.0/16
5.208.0.0/15
5.210.0.0/16
5.211.0.0/16
5.212.0.0/15
5.214.0.0/15
5.216.0.0/15
5.218.0.0/16
5.219.0.0/18
5.219.64.0/18
5.219.128.0/18
5.219.192.0/18
5.220.0.0/15
5.232.0.0/13
5.250.0.0/17
5.252.216.0/22
5.253.24.0/22
5.253.96.0/22
5.253.225.0/24
31.2.128.0/17
31.7.64.0/20
31.7.88.0/22
31.7.96.0/19
31.7.128.0/20
31.14.80.0/20
31.14.112.0/20
31.14.144.0/20
31.24.200.0/21
31.24.232.0/21
31.25.90.0/24
31.25.91.0/24
31.25.92.0/22
31.25.104.0/21
31.25.128.0/21
31.25.232.0/23
31.40.0.0/21
31.41.35.0/24
31.47.32.0/19
31.170.48.0/20
31.171.216.0/21
31.184.128.0/18
31.193.112.0/21
31.193.186.0/24
31.214.132.0/23
31.214.146.0/23
31.214.154.0/24
31.214.168.0/23
31.214.170.0/23
31.214.172.0/22
31.214.200.0/23
31.214.228.0/22
31.214.248.0/21
31.216.62.0/24
31.217.208.0/21
37.9.248.0/22
37.9.252.0/22
37.10.64.0/22
37.10.109.0/24
37.10.117.0/24
37.19.80.0/20
37.32.0.0/19
37.32.32.0/21
37.32.40.0/21
37.32.112.0/20
37.44.56.0/21
37.49.144.0/21
37.63.128.0/17
37.75.240.0/23
37.75.242.0/24
37.75.243.0/24
37.75.244.0/22
37.98.0.0/17
37.114.192.0/18
37.129.0.0/16
37.130.200.0/21
37.137.0.0/16
37.143.144.0/21
37.148.0.0/17
37.148.248.0/22
37.152.160.0/20
37.152.176.0/20
37.153.128.0/22
37.153.176.0/20
37.156.0.0/22
37.156.8.0/21
37.156.16.0/20
37.156.48.0/20
37.156.100.0/22
37.156.112.0/20
37.156.128.0/20
37.156.144.0/22
37.156.152.0/21
37.156.160.0/21
37.156.176.0/22
37.156.212.0/22
37.156.232.0/21
37.156.240.0/22
37.156.248.0/22
37.191.64.0/19
37.202.128.0/18
37.202.224.0/19
37.221.0.0/18
37.228.131.0/24
37.228.133.0/24
37.228.135.0/24
37.228.136.0/22
37.235.16.0/20
37.254.0.0/15
45.8.160.0/22
45.9.144.0/22
45.9.252.0/22
45.11.184.0/22
45.15.200.0/22
45.15.248.0/22
45.80.84.0/24
45.81.16.0/22
45.82.136.0/22
45.83.12.0/22
45.84.156.0/22
45.84.248.0/22
45.86.4.0/22
45.86.196.0/22
45.87.4.0/22
45.89.136.0/22
45.89.200.0/22
45.89.220.0/24
45.89.221.0/24
45.89.222.0/24
45.89.223.0/24
45.90.72.0/22
45.91.152.0/22
45.92.92.0/22
45.94.212.0/22
45.94.252.0/22
45.95.88.0/22
45.128.140.0/22
45.129.36.0/22
45.129.116.0/22
45.132.32.0/24
45.132.168.0/22
45.132.172.0/22
45.134.97.0/24
45.134.99.0/24
45.135.240.0/22
45.137.16.0/23
45.137.19.0/24
45.138.132.0/22
45.139.8.0/22
45.140.28.0/22
45.140.224.0/22
45.140.228.0/22
45.142.188.0/22
45.143.252.0/22
45.144.16.0/22
45.144.124.0/22
45.147.76.0/22
45.148.248.0/22
45.149.76.0/22
45.150.88.0/22
45.150.150.0/24
45.155.192.0/22
45.156.116.0/22
45.156.180.0/22
45.156.184.0/22
45.156.192.0/22
45.156.196.0/22
45.156.200.0/22
45.157.244.0/22
45.158.120.0/22
45.159.112.0/22
45.159.148.0/22
45.159.196.0/22
45.248.164.0/22
46.18.248.0/21
46.28.72.0/21
46.29.32.0/24
46.29.33.0/24
46.29.34.0/24
46.32.0.0/19
46.34.96.0/19
46.34.160.0/22
46.34.164.0/22
46.34.168.0/21
46.34.176.0/20
46.36.96.0/20
46.38.128.0/19
46.41.192.0/18
46.51.0.0/17
46.100.0.0/16
46.102.120.0/21
46.102.128.0/20
46.102.184.0/22
46.143.0.0/17
46.143.192.0/18
46.164.64.0/18
46.167.128.0/19
46.182.32.0/21
46.235.76.0/23
46.245.0.0/19
46.245.32.0/20
46.245.48.0/21
46.245.56.0/21
46.245.64.0/18
46.248.32.0/19
46.249.120.0/21
46.251.224.0/24
46.251.226.0/24
46.251.237.0/24
46.255.216.0/21
62.3.14.0/24
62.3.41.0/24
62.3.42.0/24
62.60.128.0/17
62.102.128.0/20
62.106.95.0/24
62.193.0.0/19
62.204.61.0/24
62.220.96.0/21
62.220.104.0/22
62.220.108.0/24
62.220.109.0/24
62.220.110.0/23
62.220.112.0/20
66.79.96.0/19
69.194.64.0/18
77.36.128.0/17
77.72.80.0/24
77.74.202.0/24
77.77.64.0/18
77.81.32.0/20
77.81.128.0/21
77.81.144.0/20
77.81.192.0/19
77.95.219.0/24
77.237.160.0/19
77.238.104.0/21
77.238.112.0/21
77.238.120.0/22
77.238.124.0/22
77.245.224.0/20
78.24.205.0/24
78.31.232.0/22
78.38.0.0/15
78.41.61.0/24
78.41.62.0/24
78.41.137.0/24
78.109.192.0/20
78.110.112.0/21
78.110.120.0/22
78.110.124.0/22
78.111.0.0/22
78.111.4.0/23
78.111.6.0/24
78.111.7.0/24
78.111.8.0/21
78.154.32.0/19
78.157.32.0/19
78.158.160.0/19
79.127.0.0/17
79.132.192.0/23
79.132.200.0/21
79.132.208.0/20
79.143.84.0/22
79.174.160.0/21
79.175.128.0/18
80.66.176.0/20
80.71.112.0/20
80.71.149.0/24
80.75.0.0/20
80.75.213.0/24
80.91.208.0/24
80.94.80.0/23
80.191.0.0/16
80.210.0.0/18
80.210.128.0/17
80.242.0.0/20
80.244.7.0/24
80.244.11.0/24
80.249.112.0/23
80.249.114.0/24
80.249.115.0/24
80.250.192.0/20
80.253.128.0/20
80.253.144.0/20
81.12.0.0/17
81.16.112.0/20
81.28.32.0/20
81.28.48.0/20
81.28.252.0/22
81.29.240.0/20
81.30.98.0/24
81.30.107.0/24
81.30.108.0/24
81.31.160.0/20
81.31.176.0/20
81.31.224.0/19
81.90.144.0/20
81.91.128.0/20
81.91.144.0/20
82.99.192.0/18
82.180.192.0/18
83.97.72.0/24
83.120.0.0/14
83.150.192.0/22
84.47.192.0/19
84.47.224.0/21
84.47.232.0/21
84.47.240.0/20
84.241.0.0/18
85.9.64.0/18
85.15.0.0/18
85.133.128.0/17
85.159.113.0/24
85.185.0.0/16
85.198.0.0/21
85.198.8.0/21
85.198.16.0/21
85.198.24.0/22
85.198.28.0/22
85.198.48.0/20
85.204.30.0/23
85.204.76.0/23
85.204.80.0/20
85.204.104.0/23
85.204.128.0/22
85.204.208.0/20
85.208.252.0/22
85.209.40.0/24
85.209.41.0/24
85.239.192.0/19
86.55.0.0/16
86.57.0.0/17
86.104.32.0/20
86.104.80.0/20
86.104.96.0/20
86.104.232.0/21
86.104.240.0/21
86.105.40.0/21
86.105.128.0/20
86.106.142.0/24
86.106.192.0/21
86.107.0.0/20
86.107.47.0/24
86.107.80.0/20
86.107.144.0/20
86.107.172.0/22
86.107.184.0/24
86.107.208.0/20
86.109.32.0/19
87.107.0.0/16
87.236.38.0/24
87.236.39.0/24
87.236.208.0/21
87.247.168.0/21
87.247.176.0/23
87.247.178.0/24
87.247.179.0/24
87.247.180.0/22
87.247.184.0/21
87.248.128.0/19
87.251.128.0/19
88.135.36.0/22
88.135.40.0/21
88.135.68.0/24
88.135.72.0/24
88.135.75.0/24
88.218.16.0/22
89.32.0.0/19
89.32.96.0/20
89.32.196.0/23
89.32.248.0/22
89.33.18.0/23
89.33.87.0/24
89.33.100.0/22
89.33.128.0/24
89.33.129.0/24
89.33.204.0/23
89.33.234.0/23
89.33.240.0/23
89.34.20.0/23
89.34.32.0/19
89.34.88.0/23
89.34.94.0/23
89.34.128.0/19
89.34.168.0/23
89.34.176.0/23
89.34.200.0/23
89.34.248.0/21
89.35.58.0/23
89.35.64.0/21
89.35.120.0/22
89.35.132.0/23
89.35.156.0/23
89.35.172.0/24
89.35.176.0/23
89.35.180.0/22
89.35.194.0/23
89.36.16.0/23
89.36.48.0/20
89.36.96.0/20
89.36.176.0/20
89.36.194.0/23
89.36.226.0/23
89.36.252.0/23
89.37.0.0/20
89.37.30.0/23
89.37.42.0/23
89.37.102.0/23
89.37.144.0/21
89.37.152.0/22
89.37.168.0/22
89.37.198.0/23
89.37.208.0/22
89.37.218.0/23
89.37.240.0/20
89.38.24.0/23
89.38.80.0/20
89.38.102.0/23
89.38.184.0/21
89.38.192.0/21
89.38.212.0/22
89.38.242.0/23
89.38.244.0/22
89.39.8.0/22
89.39.186.0/23
89.39.208.0/24
89.40.35.0/24
89.40.38.0/23
89.40.65.0/24
89.40.78.0/23
89.40.90.0/23
89.40.106.0/23
89.40.110.0/23
89.40.128.0/23
89.40.152.0/21
89.40.240.0/20
89.41.8.0/21
89.41.16.0/21
89.41.32.0/23
89.41.40.0/22
89.41.58.0/23
89.41.184.0/22
89.41.192.0/19
89.41.240.0/21
89.42.32.0/23
89.42.44.0/22
89.42.56.0/23
89.42.68.0/23
89.42.96.0/21
89.42.136.0/22
89.42.150.0/23
89.42.184.0/21
89.42.196.0/22
89.42.208.0/22
89.42.228.0/23
89.43.0.0/20
89.43.36.0/23
89.43.70.0/23
89.43.88.0/21
89.43.96.0/21
89.43.144.0/21
89.43.182.0/23
89.43.188.0/23
89.43.204.0/23
89.43.216.0/21
89.43.224.0/21
89.44.112.0/23
89.44.118.0/23
89.44.128.0/21
89.44.146.0/23
89.44.176.0/21
89.44.190.0/23
89.44.202.0/23
89.44.240.0/22
89.45.48.0/20
89.45.68.0/23
89.45.80.0/23
89.45.89.0/24
89.45.112.0/21
89.45.126.0/23
89.45.152.0/21
89.45.230.0/23
89.46.44.0/23
89.46.60.0/23
89.46.94.0/23
89.46.184.0/21
89.46.216.0/22
89.47.64.0/20
89.47.128.0/19
89.47.196.0/22
89.47.200.0/22
89.144.128.0/18
89.165.0.0/17
89.196.0.0/16
89.198.0.0/17
89.198.128.0/17
89.199.0.0/16
89.219.64.0/18
89.219.192.0/18
89.221.80.0/20
89.235.64.0/22
89.235.68.0/22
89.235.72.0/21
89.235.80.0/20
89.235.96.0/22
89.235.100.0/22
89.235.104.0/21
89.235.112.0/20
91.92.104.0/24
91.92.114.0/24
91.92.121.0/24
91.92.122.0/23
91.92.124.0/22
91.92.129.0/24
91.92.130.0/23
91.92.132.0/22
91.92.145.0/24
91.92.146.0/23
91.92.148.0/22
91.92.156.0/22
91.92.164.0/22
91.92.172.0/22
91.92.180.0/22
91.92.184.0/21
91.92.192.0/23
91.92.204.0/22
91.92.208.0/21
91.92.220.0/22
91.92.228.0/23
91.92.231.0/24
91.92.236.0/22
91.102.126.0/24
91.102.127.0/24
91.106.64.0/19
91.108.128.0/19
91.109.104.0/21
91.133.128.0/17
91.147.64.0/20
91.184.64.0/20
91.184.80.0/20
91.185.128.0/19
91.190.88.0/21
91.192.160.0/24
91.194.6.0/24
91.195.37.0/24
91.197.242.0/24
91.198.110.0/24
91.199.9.0/24
91.199.14.0/24
91.199.18.0/24
91.199.27.0/24
91.199.30.0/24
91.199.43.0/24
91.199.215.0/24
91.206.28.0/24
91.206.171.0/24
91.206.177.0/24
91.207.18.0/24
91.207.205.0/24
91.208.163.0/24
91.209.96.0/24
91.209.161.0/24
91.209.183.0/24
91.209.184.0/24
91.209.186.0/24
91.212.174.0/24
91.212.232.0/24
91.213.83.0/24
91.213.151.0/24
91.213.157.0/24
91.213.164.0/24
91.213.167.0/24
91.213.172.0/24
91.216.71.0/24
91.216.159.0/24
91.216.171.0/24
91.216.217.0/24
91.217.166.0/24
91.217.177.0/24
91.219.116.0/24
91.220.0.0/24
91.220.113.0/24
91.220.243.0/24
91.221.116.0/23
91.221.232.0/23
91.221.240.0/23
91.222.196.0/23
91.222.204.0/22
91.223.61.0/24
91.223.116.0/24
91.223.146.0/24
91.223.187.0/24
91.224.20.0/23
91.226.224.0/23
91.226.244.0/24
91.226.246.0/24
91.227.27.0/24
91.227.84.0/22
91.227.246.0/23
91.228.22.0/23
91.228.132.0/23
91.228.168.0/24
91.228.192.0/24
91.229.46.0/23
91.231.144.0/24
91.231.222.0/24
91.234.38.0/24
91.234.39.0/24
91.234.52.0/24
91.234.147.0/24
91.236.168.0/23
91.239.59.0/24
91.239.189.0/24
91.239.192.0/24
91.239.210.0/24
91.240.95.0/24
91.240.116.0/24
91.243.114.0/24
91.243.119.0/24
91.244.120.0/22
91.245.228.0/22
91.246.31.0/24
91.246.44.0/24
91.247.171.0/24
91.247.174.0/24
91.250.224.0/20
91.251.0.0/16
92.42.48.0/21
92.42.201.0/24
92.42.202.0/24
92.42.203.0/24
92.42.205.0/24
92.42.207.0/24
92.43.160.0/22
92.61.176.0/22
92.61.180.0/22
92.61.184.0/21
92.114.16.0/20
92.114.48.0/22
92.114.64.0/20
92.119.56.0/22
92.119.68.0/22
92.246.144.0/22
92.246.156.0/22
92.249.56.0/22
93.88.64.0/21
93.88.72.0/23
93.93.204.0/24
93.95.27.0/24
93.110.0.0/16
93.113.224.0/20
93.114.16.0/20
93.114.104.0/21
93.115.120.0/21
93.115.144.0/21
93.115.216.0/21
93.115.224.0/20
93.117.0.0/19
93.117.32.0/20
93.117.96.0/19
93.117.176.0/20
93.118.96.0/19
93.118.128.0/19
93.118.160.0/20
93.118.180.0/22
93.118.184.0/22
93.119.32.0/19
93.119.64.0/19
93.119.208.0/20
93.190.24.0/21
94.24.0.0/20
94.24.16.0/21
94.24.80.0/20
94.24.96.0/21
94.74.128.0/18
94.101.128.0/20
94.101.176.0/20
94.101.240.0/20
94.139.160.0/20
94.139.176.0/20
94.176.8.0/21
94.176.32.0/21
94.177.72.0/21
94.182.0.0/16
94.183.0.0/17
94.183.128.0/20
94.183.144.0/22
94.183.148.0/24
94.184.0.0/17
94.184.128.0/17
94.199.0.0/24
94.199.136.0/22
95.38.0.0/16
95.64.0.0/17
95.80.128.0/18
95.128.155.0/24
95.128.159.0/24
95.128.194.0/24
95.130.56.0/21
95.130.225.0/24
95.130.240.0/21
95.142.224.0/20
95.156.222.0/23
95.156.233.0/24
95.156.234.0/23
95.156.236.0/23
95.156.248.0/23
95.156.252.0/22
95.162.0.0/16
95.215.59.0/24
103.111.69.0/24
103.111.71.0/24
103.132.228.0/23
103.140.128.0/23
103.215.220.0/22
103.216.60.0/22
103.217.124.0/22
103.231.136.0/22
109.70.73.0/24
109.70.74.0/24
109.70.76.0/24
109.70.77.0/24
109.70.78.0/24
109.70.237.0/24
109.72.192.0/20
109.74.224.0/20
109.94.164.0/23
109.94.166.0/23
109.95.60.0/22
109.107.131.0/24
109.107.132.0/24
109.108.160.0/19
109.109.32.0/19
109.122.224.0/20
109.122.240.0/20
109.125.128.0/19
109.125.160.0/19
109.162.128.0/17
109.201.0.0/19
109.203.128.0/19
109.203.160.0/19
109.206.252.0/22
109.225.128.0/18
109.230.64.0/19
109.230.192.0/23
109.230.200.0/24
109.230.204.0/22
109.230.221.0/24
109.230.223.0/24
109.230.242.0/24
109.230.246.0/24
109.230.247.0/24
109.230.251.0/24
109.232.0.0/21
109.238.176.0/20
109.239.0.0/20
113.203.0.0/17
128.0.105.0/24
128.65.176.0/20
130.185.72.0/21
130.193.24.0/24
130.193.77.0/24
130.255.192.0/18
134.255.196.0/23
134.255.200.0/21
134.255.245.0/24
134.255.246.0/24
134.255.248.0/24
134.255.249.0/24
146.19.104.0/24
146.19.212.0/24
146.19.217.0/24
146.66.128.0/22
146.66.132.0/23
146.66.134.0/23
151.232.0.0/14
151.238.0.0/15
152.89.12.0/22
152.89.44.0/22
153.51.0.0/19
153.51.128.0/19
158.58.0.0/17
158.58.184.0/21
158.255.74.0/24
158.255.78.0/24
159.20.96.0/20
164.40.233.0/24
164.138.16.0/21
164.138.128.0/18
164.138.202.0/24
164.138.203.0/24
164.138.204.0/24
164.138.206.0/24
164.215.56.0/21
164.215.128.0/17
171.22.24.0/22
172.80.128.0/17
176.10.95.0/24
176.46.128.0/19
176.56.144.0/21
176.56.152.0/22
176.56.156.0/22
176.62.144.0/21
176.65.160.0/19
176.65.192.0/19
176.65.224.0/20
176.65.240.0/22
176.65.244.0/22
176.65.248.0/22
176.65.252.0/22
176.67.64.0/20
176.97.218.0/24
176.97.220.0/24
176.105.245.0/24
176.116.7.0/24
176.117.107.0/24
176.120.16.0/24
176.120.17.0/24
176.120.18.0/24
176.120.19.0/24
176.126.120.0/24
176.126.223.0/24
176.221.64.0/21
176.223.80.0/21
178.21.40.0/21
178.21.160.0/21
178.22.72.0/21
178.22.120.0/21
178.131.0.0/16
178.157.0.0/24
178.157.1.0/24
178.173.128.0/18
178.173.192.0/19
178.211.145.0/24
178.216.175.0/24
178.216.248.0/22
178.216.252.0/22
178.236.32.0/22
178.236.96.0/20
178.238.192.0/20
178.239.144.0/20
178.248.40.0/21
178.251.208.0/21
178.252.128.0/18
185.2.12.0/22
185.3.124.0/22
185.3.200.0/22
185.3.212.0/22
185.4.0.0/22
185.4.16.0/22
185.4.28.0/22
185.4.104.0/22
185.5.156.0/22
185.5.213.0/24
185.7.172.0/24
185.7.212.0/24
185.8.172.0/22
185.10.71.0/24
185.10.72.0/22
185.10.240.0/24
185.11.68.0/22
185.11.88.0/22
185.11.176.0/22
185.12.60.0/22
185.12.100.0/22
185.13.228.0/22
185.14.80.0/22
185.16.232.0/22
185.18.156.0/22
185.18.212.0/22
185.19.33.0/24
185.19.201.0/24
185.20.160.0/22
185.21.68.0/22
185.21.76.0/22
185.22.28.0/23
185.22.30.0/23
185.23.128.0/22
185.24.136.0/22
185.24.148.0/22
185.24.228.0/22
185.24.252.0/22
185.25.172.0/22
185.26.32.0/22
185.26.232.0/22
185.27.44.0/24
185.27.45.0/24
185.29.220.0/22
185.30.4.0/22
185.30.76.0/22
185.31.8.0/24
185.31.124.0/22
185.32.128.0/22
185.33.25.0/24
185.34.160.0/22
185.36.145.0/24
185.36.228.0/24
185.36.231.0/24
185.37.52.0/22
185.40.16.0/24
185.40.240.0/22
185.41.0.0/22
185.41.220.0/22
185.42.212.0/22
185.42.224.0/22
185.43.33.0/24
185.44.36.0/22
185.44.100.0/22
185.44.112.0/22
185.45.188.0/22
185.46.0.0/22
185.46.108.0/22
185.46.216.0/22
185.47.48.0/22
185.49.84.0/22
185.49.96.0/22
185.49.104.0/23
185.49.106.0/23
185.49.174.0/24
185.49.231.0/24
185.50.36.0/24
185.50.37.0/24
185.50.38.0/23
185.51.40.0/22
185.51.200.0/22
185.53.140.0/22
185.55.224.0/22
185.56.92.0/22
185.56.96.0/22
185.57.132.0/22
185.57.164.0/22
185.57.200.0/22
185.58.240.0/22
185.59.112.0/23
185.60.32.0/22
185.60.59.0/24
185.60.136.0/22
185.62.232.0/22
185.63.113.0/24
185.63.114.0/24
185.63.236.0/22
185.64.176.0/22
185.65.118.0/24
185.66.224.0/22
185.66.228.0/22
185.67.12.0/22
185.67.100.0/22
185.67.156.0/22
185.67.212.0/22
185.69.108.0/22
185.70.60.0/22
185.71.152.0/22
185.71.192.0/22
185.72.24.0/22
185.72.80.0/22
185.73.0.0/22
185.73.76.0/22
185.73.112.0/22
185.73.226.0/24
185.74.164.0/22
185.74.221.0/24
185.75.196.0/22
185.75.204.0/22
185.76.248.0/22
185.77.21.0/24
185.78.20.0/22
185.79.16.0/24
185.79.60.0/22
185.79.96.0/22
185.79.156.0/22
185.80.100.0/22
185.80.198.0/23
185.81.40.0/22
185.81.96.0/22
185.82.28.0/22
185.82.64.0/22
185.82.136.0/22
185.82.164.0/22
185.82.180.0/22
185.83.28.0/22
185.83.76.0/22
185.83.80.0/22
185.83.88.0/24
185.83.89.0/24
185.83.90.0/23
185.83.112.0/22
185.83.180.0/24
185.83.181.0/24
185.83.182.0/24
185.83.183.0/24
185.83.184.0/22
185.83.196.0/22
185.83.208.0/22
185.84.156.0/24
185.84.157.0/24
185.84.158.0/24
185.84.159.0/24
185.84.160.0/22
185.84.220.0/22
185.84.226.0/24
185.85.68.0/22
185.85.136.0/22
185.86.36.0/22
185.86.180.0/22
185.88.48.0/22
185.88.152.0/22
185.88.176.0/22
185.88.252.0/22
185.89.22.0/24
185.89.112.0/22
185.92.4.0/22
185.92.8.0/22
185.92.40.0/22
185.93.88.0/24
185.93.89.0/24
185.94.96.0/22
185.94.180.0/24
185.94.181.0/24
185.95.60.0/22
185.95.152.0/22
185.95.180.0/22
185.96.240.0/22
185.97.116.0/22
185.98.112.0/22
185.99.212.0/22
185.100.44.0/22
185.101.39.0/24
185.101.228.0/22
185.103.84.0/22
185.103.128.0/22
185.103.201.0/24
185.103.244.0/22
185.103.248.0/22
185.104.228.0/22
185.104.232.0/22
185.104.240.0/22
185.105.100.0/22
185.105.120.0/22
185.105.184.0/22
185.105.236.0/24
185.105.237.0/24
185.105.238.0/23
185.106.136.0/22
185.106.144.0/22
185.106.200.0/22
185.106.228.0/22
185.107.28.0/22
185.107.32.0/22
185.107.244.0/22
185.107.248.0/22
185.108.96.0/22
185.108.164.0/22
185.109.60.0/22
185.109.72.0/22
185.109.80.0/22
185.109.128.0/22
185.109.244.0/22
185.109.248.0/22
185.110.28.0/22
185.110.218.0/23
185.110.228.0/22
185.110.236.0/22
185.110.244.0/22
185.110.252.0/22
185.111.8.0/22
185.111.12.0/22
185.111.64.0/22
185.111.80.0/22
185.111.136.0/22
185.112.32.0/22
185.112.36.0/22
185.112.128.0/22
185.112.148.0/22
185.112.168.0/22
185.113.9.0/24
185.113.10.0/24
185.113.56.0/22
185.113.112.0/22
185.113.248.0/24
185.114.188.0/22
185.115.76.0/22
185.115.148.0/23
185.115.150.0/24
185.115.151.0/24
185.115.168.0/22
185.116.20.0/22
185.116.24.0/22
185.116.44.0/22
185.116.112.0/24
185.116.160.0/22
185.117.48.0/22
185.117.136.0/22
185.117.204.0/22
185.118.12.0/22
185.118.136.0/22
185.118.152.0/22
185.119.4.0/22
185.119.164.0/22
185.119.199.0/24
185.119.240.0/22
185.120.120.0/22
185.120.160.0/22
185.120.168.0/22
185.120.192.0/22
185.120.196.0/22
185.120.200.0/22
185.120.208.0/22
185.120.212.0/22
185.120.216.0/22
185.120.220.0/22
185.120.224.0/22
185.120.228.0/22
185.120.232.0/22
185.120.236.0/22
185.120.240.0/22
185.120.244.0/22
185.120.248.0/22
185.121.56.0/22
185.121.128.0/22
185.122.80.0/22
185.123.68.0/22
185.123.208.0/22
185.124.112.0/22
185.124.156.0/22
185.124.172.0/22
185.125.20.0/22
185.125.244.0/22
185.125.248.0/22
185.125.252.0/22
185.126.0.0/22
185.126.4.0/22
185.126.8.0/22
185.126.12.0/22
185.126.16.0/22
185.126.40.0/22
185.126.132.0/22
185.126.156.0/22
185.126.200.0/23
185.126.202.0/23
185.127.232.0/22
185.128.40.0/24
185.128.48.0/22
185.128.80.0/22
185.128.136.0/22
185.128.152.0/23
185.128.154.0/23
185.128.164.0/22
185.129.80.0/22
185.129.108.0/22
185.129.116.0/22
185.129.168.0/22
185.129.184.0/22
185.129.188.0/22
185.129.196.0/22
185.129.200.0/22
185.129.212.0/22
185.129.216.0/22
185.129.228.0/22
185.129.232.0/22
185.129.236.0/22
185.129.240.0/22
185.130.50.0/24
185.130.76.0/22
185.130.101.0/24
185.131.28.0/22
185.131.84.0/22
185.131.88.0/22
185.131.92.0/22
185.131.100.0/22
185.131.108.0/22
185.131.112.0/22
185.131.116.0/22
185.131.124.0/22
185.131.128.0/22
185.131.136.0/22
185.131.140.0/22
185.131.148.0/22
185.131.152.0/22
185.131.156.0/22
185.131.164.0/22
185.131.168.0/22
185.132.80.0/22
185.132.124.0/24
185.132.212.0/22
185.133.125.0/24
185.133.152.0/22
185.133.164.0/22
185.133.244.0/22
185.134.96.0/22
185.135.28.0/22
185.135.46.0/24
185.135.47.0/24
185.135.228.0/22
185.136.100.0/22
185.136.133.0/24
185.136.135.0/24
185.136.172.0/22
185.136.180.0/22
185.136.192.0/22
185.136.220.0/22
185.137.24.0/22
185.137.60.0/22
185.137.108.0/22
185.139.64.0/22
185.140.4.0/22
185.140.56.0/22
185.140.232.0/22
185.141.36.0/22
185.141.48.0/22
185.141.104.0/22
185.141.132.0/22
185.141.168.0/22
185.141.212.0/22
185.141.244.0/22
185.142.92.0/22
185.142.124.0/22
185.142.156.0/22
185.142.232.0/22
185.143.72.0/22
185.143.204.0/22
185.143.232.0/22
185.144.64.0/22
185.145.8.0/22
185.145.184.0/22
185.147.40.0/22
185.147.84.0/22
185.147.160.0/22
185.147.176.0/22
185.149.192.0/24
185.150.108.0/22
185.151.236.0/22
185.153.184.0/22
185.153.208.0/22
185.154.184.0/22
185.154.190.0/24
185.155.8.0/22
185.155.12.0/22
185.155.72.0/24
185.155.73.0/24
185.155.229.0/24
185.155.236.0/22
185.157.8.0/22
185.158.172.0/22
185.159.152.0/22
185.159.176.0/22
185.159.189.0/24
185.160.104.0/22
185.160.176.0/22
185.160.205.0/24
185.161.36.0/22
185.161.112.0/22
185.161.121.0/24
185.161.250.0/24
185.162.40.0/22
185.162.216.0/24
185.162.217.0/24
185.162.218.0/23
185.164.72.0/22
185.164.252.0/22
185.165.28.0/22
185.165.40.0/22
185.165.116.0/22
185.165.204.0/22
185.166.60.0/22
185.166.92.0/24
185.166.104.0/22
185.166.112.0/22
185.167.72.0/22
185.167.100.0/22
185.167.124.0/22
185.169.6.0/24
185.169.20.0/22
185.169.36.0/22
185.170.8.0/24
185.170.236.0/22
185.171.52.0/22
185.172.0.0/22
185.172.68.0/22
185.172.212.0/22
185.173.104.0/22
185.173.129.0/24
185.173.130.0/24
185.173.168.0/22
185.174.132.0/24
185.174.134.0/24
185.174.200.0/22
185.174.248.0/22
185.175.76.0/22
185.175.240.0/22
185.176.32.0/22
185.176.56.0/22
185.177.156.0/22
185.177.232.0/22
185.178.104.0/22
185.178.220.0/22
185.179.90.0/24
185.179.168.0/22
185.179.220.0/22
185.180.52.0/22
185.180.128.0/22
185.181.180.0/22
185.182.220.0/22
185.182.248.0/22
185.184.32.0/22
185.184.48.0/22
185.185.16.0/22
185.185.240.0/22
185.186.48.0/22
185.186.240.0/22
185.187.48.0/22
185.187.84.0/23
185.187.86.0/23
185.188.104.0/22
185.188.112.0/23
185.188.114.0/23
185.189.120.0/22
185.190.20.0/22
185.190.25.0/24
185.190.39.0/24
185.191.76.0/22
185.192.8.0/22
185.192.112.0/22
185.193.47.0/24
185.193.208.0/22
185.194.76.0/22
185.194.244.0/22
185.195.72.0/22
185.196.148.0/22
185.197.68.0/22
185.197.112.0/22
185.198.160.0/22
185.199.64.0/22
185.199.208.0/24
185.199.210.0/23
185.201.48.0/22
185.202.56.0/22
185.203.160.0/22
185.204.180.0/22
185.204.197.0/24
185.205.203.0/24
185.206.92.0/22
185.206.229.0/24
185.206.231.0/24
185.206.236.0/22
185.207.52.0/22
185.207.72.0/22
185.208.76.0/22
185.208.148.0/22
185.208.174.0/23
185.208.180.0/22
185.209.42.0/24
185.209.188.0/22
185.210.200.0/22
185.211.84.0/22
185.211.88.0/22
185.212.48.0/22
185.212.192.0/22
185.213.8.0/22
185.213.164.0/22
185.213.195.0/24
185.214.36.0/22
185.215.124.0/22
185.215.152.0/22
185.215.228.0/22
185.217.6.0/24
185.217.39.0/24
185.219.112.0/22
185.220.224.0/22
185.221.112.0/22
185.221.192.0/22
185.221.239.0/24
185.222.120.0/22
185.222.180.0/22
185.222.210.0/24
185.223.160.0/24
185.223.214.0/24
185.224.176.0/22
185.225.80.0/22
185.225.180.0/22
185.226.97.0/24
185.226.116.0/22
185.226.132.0/22
185.226.140.0/22
185.227.64.0/22
185.227.78.0/24
185.227.79.0/24
185.227.116.0/22
185.228.58.0/24
185.228.59.0/24
185.228.236.0/22
185.229.0.0/22
185.229.28.0/22
185.229.133.0/24
185.229.134.0/24
185.229.135.0/24
185.229.204.0/24
185.231.65.0/24
185.231.112.0/23
185.231.114.0/24
185.231.115.0/24
185.231.180.0/22
185.232.152.0/22
185.232.176.0/22
185.233.12.0/22
185.233.84.0/22
185.233.131.0/24
185.234.14.0/24
185.234.192.0/22
185.235.136.0/24
185.235.139.0/24
185.235.196.0/24
185.235.197.0/24
185.235.198.0/24
185.235.245.0/24
185.236.36.0/22
185.236.45.0/24
185.236.88.0/22
185.237.8.0/22
185.237.84.0/22
185.238.20.0/22
185.238.44.0/22
185.238.92.0/22
185.238.140.0/24
185.238.143.0/24
185.238.167.0/24
185.239.0.0/22
185.239.104.0/22
185.240.56.0/22
185.240.148.0/22
185.241.204.0/24
185.243.48.0/22
185.244.52.0/22
185.246.4.0/22
185.248.32.0/24
185.249.9.0/24
185.249.10.0/24
185.251.76.0/22
185.252.28.0/22
185.252.84.0/24
185.252.85.0/24
185.252.86.0/24
185.252.200.0/24
185.254.165.0/24
185.254.166.0/24
185.255.88.0/22
185.255.98.0/23
185.255.208.0/22
188.0.240.0/23
188.0.242.0/23
188.0.244.0/22
188.0.248.0/21
188.75.64.0/18
188.94.188.0/24
188.95.89.0/24
188.95.198.0/24
188.118.64.0/18
188.121.96.0/19
188.121.128.0/19
188.122.96.0/19
188.136.128.0/21
188.136.136.0/22
188.136.140.0/22
188.136.144.0/20
188.136.160.0/19
188.136.192.0/19
188.158.0.0/16
188.159.0.0/17
188.159.128.0/18
188.159.192.0/19
188.208.56.0/21
188.208.64.0/19
188.208.144.0/20
188.208.160.0/19
188.208.200.0/22
188.208.208.0/21
188.208.224.0/19
188.209.0.0/21
188.209.8.0/21
188.209.16.0/20
188.209.32.0/20
188.209.64.0/20
188.209.116.0/22
188.209.152.0/23
188.209.192.0/20
188.210.64.0/20
188.210.80.0/21
188.210.96.0/19
188.210.128.0/18
188.210.192.0/20
188.210.232.0/22
188.211.0.0/20
188.211.32.0/19
188.211.64.0/18
188.211.128.0/19
188.211.176.0/20
188.211.192.0/19
188.212.22.0/24
188.212.48.0/20
188.212.64.0/19
188.212.96.0/22
188.212.144.0/21
188.212.160.0/19
188.212.200.0/21
188.212.208.0/20
188.212.224.0/20
188.212.240.0/21
188.213.64.0/20
188.213.96.0/19
188.213.144.0/20
188.213.176.0/20
188.213.192.0/21
188.213.208.0/22
188.214.4.0/22
188.214.84.0/22
188.214.96.0/22
188.214.120.0/23
188.214.160.0/19
188.214.216.0/21
188.215.24.0/22
188.215.88.0/22
188.215.128.0/20
188.215.160.0/19
188.215.192.0/19
188.215.240.0/22
188.229.0.0/17
188.240.196.0/24
188.240.212.0/24
188.240.248.0/21
188.253.32.0/19
188.253.64.0/19
192.15.0.0/16
192.166.36.0/24
192.166.37.0/24
192.166.38.0/24
193.3.31.0/24
193.3.182.0/24
193.3.231.0/24
193.3.255.0/24
193.5.44.0/24
193.8.139.0/24
193.9.24.0/24
193.19.147.0/24
193.22.20.0/24
193.24.103.0/24
193.24.105.0/24
193.24.118.0/24
193.24.120.0/24
193.24.121.0/24
193.27.9.0/24
193.29.50.0/24
193.30.30.0/24
193.34.244.0/22
193.35.230.0/24
193.36.92.0/24
193.36.93.0/24
193.37.37.0/24
193.37.38.0/24
193.38.247.0/24
193.39.70.0/24
193.41.206.0/24
193.46.214.0/24
193.56.59.0/24
193.56.61.0/24
193.56.107.0/24
193.56.118.0/24
193.56.181.0/24
193.84.255.0/24
193.93.169.0/24
193.93.171.0/24
193.93.182.0/24
193.104.29.0/24
193.105.153.0/24
193.105.234.0/24
193.106.190.0/24
193.107.44.0/24
193.107.48.0/24
193.109.56.0/24
193.111.234.0/23
193.111.236.0/24
193.134.100.0/23
193.138.77.0/24
193.141.64.0/23
193.141.126.0/23
193.142.232.0/23
193.142.254.0/23
193.148.64.0/22
193.150.66.0/24
193.151.128.0/19
193.162.129.0/24
193.176.97.0/24
193.176.240.0/22
193.177.242.0/24
193.177.245.0/24
193.186.32.0/24
193.186.215.0/24
193.200.102.0/23
193.200.148.0/24
193.201.23.0/24
193.201.66.0/24
193.201.72.0/23
193.201.192.0/22
193.228.90.0/23
193.228.136.0/24
193.228.168.0/23
193.242.125.0/24
193.246.174.0/23
193.246.200.0/23
194.0.234.0/24
194.1.155.0/24
194.5.16.0/24
194.5.40.0/22
194.5.50.0/24
194.5.54.0/24
194.5.175.0/24
194.5.176.0/22
194.5.188.0/24
194.5.195.0/24
194.5.205.0/24
194.9.56.0/23
194.9.80.0/23
194.26.64.0/24
194.26.66.0/24
194.26.99.0/24
194.26.117.0/24
194.26.195.0/24
194.29.79.0/24
194.31.108.0/24
194.32.209.0/24
194.32.213.0/24
194.32.214.0/24
194.32.215.0/24
194.34.160.0/22
194.36.0.0/24
194.36.172.0/22
194.39.36.0/22
194.39.248.0/24
194.39.254.0/24
194.41.48.0/22
194.48.198.0/24
194.50.42.0/24
194.50.204.0/24
194.50.209.0/24
194.50.216.0/24
194.50.218.0/24
194.53.118.0/23
194.53.122.0/23
194.56.148.0/24
194.59.170.0/23
194.59.214.0/24
194.59.215.0/24
194.60.208.0/22
194.60.228.0/22
194.61.3.0/24
194.62.17.0/24
194.62.43.0/24
194.88.232.0/24
194.107.116.0/24
194.110.24.0/24
194.110.118.0/24
194.117.64.0/24
194.117.82.0/24
194.145.119.0/24
194.146.68.0/24
194.146.69.0/24
194.147.140.0/24
194.147.142.0/24
194.147.150.0/24
194.147.164.0/22
194.147.170.0/24
194.147.212.0/24
194.147.222.0/24
194.150.68.0/22
194.150.165.0/24
194.156.77.0/24
194.156.140.0/22
194.180.11.0/24
194.180.208.0/23
194.180.224.0/23
194.180.238.0/24
194.213.118.0/24
194.225.0.0/16
194.242.22.0/24
194.246.34.0/24
195.2.234.0/24
195.8.102.0/24
195.8.110.0/24
195.8.112.0/24
195.8.114.0/24
195.10.220.0/24
195.18.10.0/24
195.24.233.0/24
195.24.236.0/24
195.24.237.0/24
195.26.26.0/24
195.26.27.0/24
195.28.10.0/24
195.28.11.0/24
195.28.168.0/23
195.38.19.0/24
195.62.4.0/24
195.78.115.0/24
195.88.208.0/24
195.96.128.0/24
195.96.153.0/24
195.110.38.0/23
195.114.4.0/23
195.114.8.0/23
195.137.167.0/24
195.146.32.0/19
195.149.127.0/24
195.158.230.0/24
195.177.255.0/24
195.181.0.0/19
195.181.32.0/19
195.181.64.0/18
195.182.38.0/24
195.190.130.0/24
195.190.139.0/24
195.190.144.0/24
195.191.22.0/23
195.191.44.0/23
195.200.76.0/24
195.200.77.0/24
195.211.44.0/22
195.211.71.0/24
195.214.235.0/24
195.225.232.0/24
195.226.223.0/24
195.230.97.0/24
195.230.105.0/24
195.230.107.0/24
195.230.124.0/24
195.234.80.0/24
195.234.153.0/24
195.234.191.0/24
195.238.231.0/24
195.238.240.0/24
195.238.247.0/24
195.254.165.0/24
204.18.0.0/16
212.1.192.0/21
212.16.64.0/19
212.23.201.0/24
212.23.214.0/24
212.23.216.0/24
212.33.192.0/20
212.33.208.0/20
212.46.45.0/24
212.80.0.0/19
212.86.64.0/19
212.108.97.0/24
212.108.98.0/24
212.108.102.0/24
212.108.124.0/24
212.108.125.0/24
212.108.127.0/24
212.120.192.0/19
213.108.240.0/22
213.109.199.0/24
213.109.240.0/20
213.134.17.0/24
213.176.0.0/19
213.176.32.0/19
213.176.64.0/18
213.177.176.0/24
213.177.177.0/24
213.177.178.0/24
213.177.179.0/24
213.177.181.0/24
213.195.0.0/20
213.195.16.0/21
213.195.32.0/20
213.195.48.0/22
213.195.52.0/22
213.195.56.0/21
213.207.192.0/20
213.207.208.0/20
213.207.224.0/19
213.232.124.0/22
213.233.160.0/19
217.11.16.0/20
217.18.48.0/24
217.18.90.0/24
217.18.94.0/24
217.20.252.0/24
217.24.144.0/20
217.25.48.0/20
217.66.192.0/20
217.66.208.0/20
217.77.112.0/20
217.114.40.0/24
217.114.46.0/24
217.144.104.0/22
217.146.208.0/20
217.170.240.0/20
217.171.145.0/24
217.171.148.0/22
217.172.96.0/22
217.172.100.0/23
217.172.102.0/23
217.172.104.0/21
217.172.112.0/21
217.172.120.0/21
217.174.16.0/20
217.198.190.0/24
217.218.0.0/15
31.130.176.0/20
46.148.32.0/20
81.163.0.0/21
88.135.32.0/24
88.135.33.0/24
88.135.34.0/23
91.207.138.0/23
91.208.165.0/24
91.209.242.0/24
91.212.16.0/24
91.212.252.0/24
91.216.4.0/24
91.217.64.0/23
91.220.79.0/24
91.222.198.0/23
91.224.110.0/23
91.224.176.0/23
91.225.52.0/22
91.228.189.0/24
91.229.214.0/23
91.230.32.0/24
91.232.64.0/22
91.232.68.0/23
91.232.72.0/22
91.233.56.0/22
91.237.254.0/23
91.238.0.0/24
91.239.14.0/24
91.239.108.0/22
91.239.214.0/24
91.240.60.0/22
91.240.180.0/22
91.241.20.0/23
91.241.92.0/24
91.242.44.0/23
91.243.126.0/23
91.243.160.0/20
91.247.66.0/23
93.126.0.0/18
94.232.168.0/21
95.215.160.0/22
95.215.173.0/24
109.95.64.0/21
157.119.188.0/22
176.101.32.0/20
176.101.48.0/21
176.102.224.0/19
176.122.210.0/23
176.123.64.0/18
176.124.64.0/22
178.215.0.0/18
178.219.224.0/20
185.1.77.0/24
188.191.176.0/21
193.0.156.0/24
193.19.144.0/23
193.28.181.0/24
193.32.80.0/23
193.35.62.0/24
193.104.22.0/24
193.104.212.0/24
193.105.2.0/24
193.105.6.0/24
193.178.200.0/22
193.189.122.0/23
193.222.51.0/24
193.242.194.0/23
194.33.104.0/22
194.33.122.0/23
194.33.124.0/24
194.33.125.0/24
194.33.126.0/23
194.143.140.0/23
194.146.148.0/22
195.20.136.0/24
195.88.188.0/23
195.191.74.0/23
195.245.70.0/23
196.3.91.0/24
"""

IRAN_NETS = []
for _r in IRAN_RAW.strip().splitlines():
    try: IRAN_NETS.append(ipaddress.ip_network(_r.strip()))
    except Exception: pass

def is_iran(ip):
    try:
        a = ipaddress.ip_address(ip)
        return any(a in n for n in IRAN_NETS)
    except Exception: return False

def flag(cc):
    if not cc or len(cc)!=2: return "\U0001f30d"
    try: return chr(127397+ord(cc[0].upper()))+chr(127397+ord(cc[1].upper()))
    except Exception: return "\U0001f30d"

def clabel(cc, ip=""):
    if ip and is_iran(ip): return flag("IR"),"Iran","IR"
    cc=(cc or "").upper().replace("UK","GB")
    return flag(cc), CNAMES.get(cc,cc or "?"), cc or "?"

# ── Startup ───────────────────────────────────────────────────────────────────
REG_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_NAME = "IPBar"

def _autostart_command():
    exe    = sys.executable
    script = os.path.abspath(__file__)
    pyw    = exe.replace("python.exe","pythonw.exe")
    if not os.path.exists(pyw):
        pyw = os.path.join(os.path.dirname(exe),"pythonw.exe")
    if not os.path.exists(pyw): pyw = exe
    return f'"{pyw}" "{script}"'

def reg_current():
    """The command autostart runs today, or None when autostart is off."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,REG_KEY) as k:
            value,_=winreg.QueryValueEx(k,REG_NAME)
            return value
    except OSError:
        return None

def reg_set(cmd):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,REG_KEY,0,winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k,REG_NAME,0,winreg.REG_SZ,cmd)
        return True
    except OSError:
        return False

def reg_remove():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,REG_KEY,0,winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k,REG_NAME)
        return True
    except OSError:
        return False

def reg_repoint():
    """Keep an EXISTING autostart entry pointing at this copy of the script.

    Deliberately never creates the entry. Enabling autostart is the installer's
    job (or the tray menu's), so a plain `python ip_bar.py` cannot silently
    install itself into HKCU\\...\\Run — that is the user's decision, not the
    script's. Moving an already-enabled script to a new folder still repoints
    itself with no manual registry edit.
    """
    current = reg_current()
    if current is None:
        return
    wanted = _autostart_command()
    if current != wanted:
        reg_set(wanted)

# ── Net ───────────────────────────────────────────────────────────────────────
# Every URL below is a literal constant, but the fetch helpers still check the
# scheme before opening: urllib happily opens file:// and ftp://, so a single
# future edit that let a fetched value reach a URL would turn into a local file
# read. The guard makes that impossible rather than merely unlikely.
#
# Two enrichment endpoints are plain http:// because their free tier offers no
# https (ip-api.com, api.runflare.com). They carry no credentials and only ever
# receive a public IP address, which the network path already knows; their
# answers feed display fields, never the geo gate's decision to send anything.
_ALLOWED_SCHEMES = ("http", "https")


def _urlopen(req, timeout):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    if urllib.parse.urlparse(url).scheme not in _ALLOWED_SCHEMES:
        raise ValueError("refusing non-http(s) URL")
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - scheme checked above


def jget(url,t=6):
    try:
        r=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
        with _urlopen(r,t) as resp:
            return json.loads(resp.read().decode())
    except Exception: return None

def _valid_ip(value):
    """Return the value only if it really is an IP address.

    Everything downstream — the toast text, the lookup URLs, the VPN
    comparison — trusts this field, and it arrives from a third-party JSON
    response. Rejecting a non-address here is what stops a hostile response
    from reaching a PowerShell string or a URL path.
    """
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return ""


def fetch_ip():
    for url in ["https://myip.wtf/json","https://ipwho.is/"]:
        d=jget(url)
        if d:
            ip=_valid_ip(d.get("YourFuckingIPAddress") or d.get("ip",""))
            if not ip:
                continue
            cc=d.get("YourFuckingCountryCode") or d.get("country_code","?")
            isp=d.get("YourFuckingISP","") or (d.get("connection") or {}).get("isp","")
            return {"ip":ip,"cc":cc,"isp":isp}
    return {"ip":"Error","cc":"?","isp":""}

def fetch_rf():
    try:
        r=urllib.request.Request("http://api.runflare.com/ip",headers={"User-Agent":"Mozilla/5.0"})
        with _urlopen(r,6) as resp:
            return _valid_ip(json.loads(resp.read().decode()).get("ip","")) or "Error"
    except Exception: return "Error"

def fetch_vpn(ip):
    d=jget(f"http://ip-api.com/json/{ip}?fields=status,proxy,hosting,org,isp,as,countryCode",t=8)
    if d and d.get("status")=="success": return d
    return None

def fetch_host(ip):
    d=jget(f"https://ipinfo.io/{ip}/json",t=6)
    if d: return d.get("hostname",""),d.get("city","")
    return "",""

def local_ip():
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        s.connect(("8.8.8.8",80)); ip=s.getsockname()[0]; s.close(); return ip
    except Exception: return "?"

def gateway():
    """Default gateway, from the routing table rather than a PowerShell call.

    The previous implementation spawned powershell.exe for Get-NetRoute, which
    costs several hundred milliseconds of interpreter startup on every refresh
    cycle for a value the routing table can answer in microseconds. That cost
    was paid three times a second.
    """
    try:
        gw = netfast.link_state().get("gateway")
        if gw:
            return gw
    except Exception:
        pass
    return "?"


def ping(host):
    try:
        r=subprocess.run(["ping","-n",str(PING_N),host],
            capture_output=True,text=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
        lost=0; ms=None
        for line in r.stdout.splitlines():
            if "Lost" in line:
                try: lost=int(line.split("(")[1].split("%")[0])
                except Exception: pass
            if "Average" in line:
                try: ms=int(line.split("=")[-1].strip().replace("ms",""))
                except Exception: pass
        return (f"{max(1,ms)}ms",lost) if ms is not None else ("Timeout",100)
    except Exception: return "Err",100

def pms(v):
    try: return int(str(v).replace("ms",""))
    except Exception: return None

def pcol(v):
    """Latency is white until the link is actually failing."""
    ms=pms(v)
    if ms is None: return RED
    return WHT if ms<180 else ORG if ms<400 else RED

def hw():
    cpu=psutil.cpu_percent(interval=None)
    ram=psutil.virtual_memory().percent
    gpu="N/A"
    try:
        r=subprocess.run(["nvidia-smi","--query-gpu=utilization.gpu","--format=csv,noheader,nounits"],
            capture_output=True,text=True,timeout=3,creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode==0: gpu=r.stdout.strip().split("\n")[0].strip()+"%"
    except Exception: pass
    return cpu,ram,gpu

def ucol(v):
    """Apple restraint: neutral until the number actually matters."""
    try:
        x=float(str(v).replace("%",""))
    except Exception:
        return MUT
    return SEC if x<70 else ORG if x<90 else RED

def spark(vals):
    B="\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    out=""
    for v in vals:
        try: out+=B[max(0,min(7,int(float(v)/100*7)))]
        except Exception: out+=B[0]
    return out

def dns_servers():
    srv=[]
    try:
        r=subprocess.run(["ipconfig","/all"],capture_output=True,text=True,timeout=4,
            creationflags=subprocess.CREATE_NO_WINDOW)
        for ip in re.findall(r"DNS Servers[^:]*:[^0-9]*([0-9]+(?:\.[0-9]+){3})",r.stdout):
            if ip not in srv: srv.append(ip)
    except Exception: pass
    return srv[:3]

def dns_leak(ip_cc,servers):
    if not servers or not ip_cc or ip_cc in ("?",""): return False,[]
    info=[]; leak=False
    for s in servers[:2]:
        try:
            a=ipaddress.ip_address(s)
            if a.is_private: info.append(f"{s} (private)"); continue
            d=jget(f"http://ip-api.com/json/{s}?fields=status,countryCode,country",t=4)
            if d and d.get("status")=="success":
                dc=d.get("countryCode","?"); dn=d.get("country","?")
                info.append(f"{s} {flag(dc)} {dn}")
                if dc not in (ip_cc,"?",""): leak=True
            else: info.append(s)
        except Exception: info.append(s)
    return leak,info

def net_score(ms,loss,dleak):
    s=100
    if ms is None: s-=30
    elif ms>250: s-=35
    elif ms>150: s-=20
    elif ms>80: s-=8
    s-=min(40,int(loss or 0)*4)
    if dleak: s-=15
    return max(0,min(100,s))

def get_tz():
    try:
        r=subprocess.run(["powershell","-Command","(Get-TimeZone).Id"],
            capture_output=True,text=True,timeout=4,creationflags=subprocess.CREATE_NO_WINDOW)
        return r.stdout.strip()
    except Exception: return ""

NET_STATE_FILE = _expand(env_str(
    "NET_STATE_FILE", os.path.join(os.path.expanduser("~"), ".ipbar_net_state.json")))

# Adapters the cut must never touch, matched against InterfaceDescription.
# Cutting a hypervisor's virtual switch takes the VMs and containers bridged to
# it down with it, and VPN tunnel adapters are owned by their client, which
# misbehaves when something else disables them out from under it. Only real,
# physically present NICs are in scope. Override with NET_SKIP in .env; the
# defaults suit a typical Windows desktop.
NET_SKIP_DESC = tuple(s.strip().lower() for s in env_str(
    "NET_SKIP",
    "hyper-v;vmware;virtualbox;openvpn;tap-windows;wintun;bluetooth;"
    "loopback;wan miniport;teredo").split(";") if s.strip())


def _ps(script, timeout=25):
    """Run PowerShell without a console window; returns (returncode, stdout)."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW)
        return r.returncode, (r.stdout or "").strip()
    except Exception:
        return -1, ""


def _ps_elevated(script, timeout=120):
    """Run PowerShell elevated behind a single UAC prompt.

    Enabling and disabling a network adapter is an administrative act, and this
    widget runs — and should keep running — as a normal user. So the privilege
    is acquired per action instead of by launching the whole program elevated.

    Start-Process -Verb RunAs raises the consent dialog, -Wait blocks until the
    child exits and -PassThru yields its exit code, which is what separates the
    two failure modes worth telling apart: a refused prompt throws in the outer
    shell and is reported as "cancelled", while a script that ran and failed
    comes back with a non-zero code. Declining is a normal user choice, not an
    error, and callers treat it as a no-op.

    The payload travels as -EncodedCommand (UTF-16LE base64) so quoting,
    adapter names containing spaces, and non-ASCII characters survive the trip
    through the outer shell unmangled.
    """
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    outer = (
        "$ErrorActionPreference='Stop';"
        "try{"
        "  $p=Start-Process powershell -Verb RunAs -WindowStyle Hidden -PassThru -Wait"
        "     -ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','" + enc + "';"
        "  Write-Output ('RC=' + $p.ExitCode)"
        "}catch{ Write-Output 'RC=CANCELLED' }"
    )
    rc, out = _ps(outer, timeout=timeout)
    if "RC=CANCELLED" in out:
        return "cancelled"
    for line in out.splitlines():
        if line.startswith("RC="):
            try:
                return int(line[3:].strip())
            except ValueError:
                return "cancelled"
    return -1


def _psq(name):
    """Quote an adapter name for a single-quoted PowerShell string literal."""
    return name.replace("'", "''")


def net_adapters():
    """Physical, present NICs as (name, status); virtual and tunnel ones excluded."""
    rc, out = _ps(
        "Get-NetAdapter | Where-Object { -not $_.Virtual } | "
        "ForEach-Object { $_.Name + '|' + $_.Status + '|' + $_.InterfaceDescription }")
    res = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name, status, desc = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not name or status.lower() == "not present":
            continue
        if any(s in desc.lower() for s in NET_SKIP_DESC):
            continue
        res.append((name, status))
    return res


def _net_state_save(names):
    try:
        _write_json_atomic(NET_STATE_FILE, {"cut": names, "at": time.time()})
    except Exception:
        pass


def _net_state_load():
    try:
        with open(NET_STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get("cut", []) or []
    except Exception:
        return []


def net_cut():
    """Disable every physical NIC, wired and wireless. Returns (ok, message)."""
    live = [n for n, s in net_adapters() if s.lower() != "disabled"]
    if not live:
        return True, "already down"
    body = "; ".join("Disable-NetAdapter -Name '%s' -Confirm:$false" % _psq(n)
                     for n in live)
    rc = _ps_elevated("$ErrorActionPreference='Stop'; " + body)
    if rc == "cancelled":
        return False, "needs admin"
    if rc != 0:
        return False, "cut failed"
    # Record what was taken down so a later restore puts back exactly that set,
    # even across a restart of the widget.
    _net_state_save(live)
    return True, "down"


def net_restore():
    """Re-enable the NICs and clear any lingering WLAN block. Returns (ok, msg)."""
    names = list(_net_state_load())
    for n, s in net_adapters():
        if s.lower() == "disabled" and n not in names:
            names.append(n)
    if not names:
        names = [n for n, _ in net_adapters()]
    if not names:
        return False, "no adapters"
    body = "; ".join("Enable-NetAdapter -Name '%s' -Confirm:$false" % _psq(n)
                     for n in names)
    # Wireless needs a second undo. `netsh wlan set autoconfig enabled=no` is
    # the other common way to cut Wi-Fi, it persists across reboots, and it
    # keeps the interface from associating even once the adapter is enabled
    # again — an adapter that is Up but will not connect. Clearing it here is a
    # no-op when it was never set, and saves a baffling diagnosis when it was.
    body += ("; Get-NetAdapter | Where-Object { $_.Status -ne 'Not Present' } |"
             " ForEach-Object { netsh wlan set autoconfig enabled=yes"
             " interface=\"$($_.Name)\" 2>$null | Out-Null }")
    rc = _ps_elevated("$ErrorActionPreference='Continue'; " + body)
    if rc == "cancelled":
        return False, "needs admin"
    _net_state_save([])
    return True, "up"


def net_is_down():
    """True when every physical NIC is disabled."""
    ads = net_adapters()
    return bool(ads) and all(s.lower() == "disabled" for _, s in ads)
def clip(txt):
    try:
        subprocess.run(["clip"],input=txt.strip().encode(),check=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception: pass

def log(old,new):
    try:
        with open(LOG_FILE,"a",encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {old} -> {new}\n")
    except Exception: pass

def toast(title,msg):
    """Windows toast notification.

    The text is passed through the environment rather than interpolated into
    the command string: msg carries an IP address taken from a third-party
    API, and a hostile response containing a quote would otherwise close the
    string and run whatever followed it as PowerShell.
    """
    try:
        ps=('[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;'
            '$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);'
            '$t.GetElementsByTagName("text")[0].AppendChild($t.CreateTextNode($env:IPBAR_TOAST_TITLE))|Out-Null;'
            '$t.GetElementsByTagName("text")[1].AppendChild($t.CreateTextNode($env:IPBAR_TOAST_MSG))|Out-Null;'
            '$n=[Windows.UI.Notifications.ToastNotification]::new($t);'
            '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("IPBar").Show($n);')
        env=dict(os.environ,
                 IPBAR_TOAST_TITLE=str(title)[:120],
                 IPBAR_TOAST_MSG=str(msg)[:200])
        subprocess.Popen(["powershell","-WindowStyle","Hidden","-Command",ps],
            env=env,creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception: pass

def ourl(url):
    import webbrowser; webbrowser.open(url)

# ── AI USAGE (Claude Max + ChatGPT Plus) ──────────────────────────────────────
# Subscription usage via each CLI's own OAuth token. No keys are ever stored
# in this file; tokens are read from the CLI credential files at runtime.
#   Claude : GET https://api.anthropic.com/api/oauth/usage
#   ChatGPT: GET https://chatgpt.com/backend-api/wham/usage
# HARD RULE: if the public IP is (or might be) Iranian, NOTHING is sent.

AI_ENABLED      = True
AI_POLL_BASE    = 900      # seconds between polls (15 min)
AI_POLL_JITTER  = 0.30     # +/- 30% random jitter
AI_POLL_MIN     = 300      # never poll faster than this, ever
AI_FAIL_BACKOFF = 1800     # after an error, wait at least this long
AI_GEO_TTL      = 120      # geo verdict cache (seconds)
AI_HTTP_TIMEOUT = 20

# Refresh a token this long BEFORE it expires rather than waiting for it to
# die. Access tokens live hours; refresh tokens weeks. Refreshing only at the
# moment of use works solely while the widget is running with the network up,
# so a long idle or offline stretch could let the refresh token lapse and
# force a genuine interactive re-login.
AI_REFRESH_LEAD = 3600     # seconds of headroom before expiry
AI_KEEPALIVE    = 1800     # keepalive thread check interval

# A credential file older than the refresh-token lifetime cannot be revived,
# so refreshing from one is pointless. It is also actively misleading: see
# _pick_cred for why a stale copy must be dropped rather than merely ranked.
AI_CRED_MAX_STALE = 16 * 86400

CLAUDE_UA    = "claude-cli/2.0.32 (external, cli)"
CLAUDE_USAGE = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_TOKEN = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT= "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

CODEX_UA     = "codex_cli_rs/0.20.0"
CODEX_USAGE  = "https://chatgpt.com/backend-api/wham/usage"
CODEX_TOKEN  = "https://auth.openai.com/oauth/token"
CODEX_CLIENT = "app_EMoamEEZ73f0CkXaXp7hrann"

_HOME = os.path.expanduser("~")

# Where the CLI tools keep their OAuth tokens. The default is the standard
# location in the Windows user profile; CLAUDE_CRED_PATHS / CODEX_CRED_PATHS
# in .env prepend extra locations (semicolon-separated), which is how users
# who run the CLIs inside WSL point the widget at their Linux home.
CLAUDE_CRED_PATHS = cred_paths(
    "CLAUDE_CRED_PATHS", os.path.join(_HOME, ".claude", ".credentials.json"))
CODEX_CRED_PATHS = cred_paths(
    "CODEX_CRED_PATHS", os.path.join(_HOME, ".codex", "auth.json"))

# Shown in place of a usage figure when the CLI has never been logged in.
# The widget works fine without either CLI; these rows simply stay empty.
NOT_CONFIGURED_CLAUDE = "please run 'claude' and log in"
NOT_CONFIGURED_CODEX  = "please run 'codex' and log in"
NOT_CONFIGURED = (NOT_CONFIGURED_CLAUDE, NOT_CONFIGURED_CODEX)

# Command used to re-run a CLI's interactive login, launched only by an
# explicit click on the "login expired" note. The default assumes the CLI is
# on PATH; users who run the CLIs elsewhere (a WSL distro, a version manager,
# a non-PATH install) override it in .env, e.g.
#   AI_LOGIN_CMD_CLAUDE=wsl.exe -d Ubuntu -- bash -lic claude
AI_LOGIN_CMD = {
    "claude": env_str("AI_LOGIN_CMD_CLAUDE", "claude"),
    "codex":  env_str("AI_LOGIN_CMD_CODEX", "codex login"),
}

# ── geo gate ──────────────────────────────────────────────────────────────────
_geo = {"ts": 0.0, "ok": False, "cc": "?", "why": "not checked"}
_geo_lock = threading.Lock()
AI_LATCH = {"blocked": False, "reason": ""}   # hard tripwire, never auto-clears


def _trace_cc(url, timeout=6):
    """Cloudflare cdn-cgi/trace -> loc=XX. Independent of any geo-IP API."""
    try:
        r = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _urlopen(r, timeout) as resp:
            txt = resp.read().decode("utf-8", "replace")
        for line in txt.splitlines():
            if line.startswith("loc="):
                return line[4:].strip().upper()
    except Exception:
        pass
    return None


def geo_verdict(known_ip=None, known_cc=None, force=False):
    """FAIL CLOSED. Returns (ok, cc, why). ok=True only when we are confident
    the egress is NOT Iran."""
    now = time.time()
    with _geo_lock:
        if not force and now - _geo["ts"] < AI_GEO_TTL:
            return _geo["ok"], _geo["cc"], _geo["why"]

    ok, cc, why = False, "?", "unknown"
    try:
        # 1. IP the widget already resolved (free, no extra request)
        if known_ip and known_ip not in ("?", "Error", "", "\u2026"):
            if is_iran(known_ip):
                ok, cc, why = False, "IR", "ip in Iranian range"
                raise StopIteration
        if known_cc and known_cc.upper() == "IR":
            ok, cc, why = False, "IR", "geoip says IR"
            raise StopIteration

        # 2. independent Cloudflare edge check (must agree)
        loc = _trace_cc("https://www.cloudflare.com/cdn-cgi/trace")
        if loc is None:
            loc = _trace_cc("https://one.one.one.one/cdn-cgi/trace")
        if loc is None:
            ok, cc, why = False, "?", "geo check failed"      # fail closed
        elif loc == "IR":
            ok, cc, why = False, "IR", "cloudflare says IR"
        elif not known_cc or known_cc in ("?", ""):
            ok, cc, why = True, loc, "cloudflare only"
        elif known_cc.upper() != loc:
            # split tunnel / disagreement -> refuse
            ok, cc, why = False, loc, "geo mismatch %s/%s" % (known_cc.upper(), loc)
        else:
            ok, cc, why = True, loc, "ok"
    except StopIteration:
        pass
    except Exception as e:
        ok, cc, why = False, "?", "geo error: %s" % type(e).__name__

    with _geo_lock:
        _geo.update(ts=now, ok=ok, cc=cc, why=why)
    return ok, cc, why


def _geo_blocked_body(code, body):
    """Detect a provider-side country block -> latch off permanently."""
    b = (body or "")[:2000].lower()
    if code in (403, 451) and any(k in b for k in (
            "unsupported_country", "country", "region", "not available in your",
            "unsupported region", "restricted")):
        return True
    return False


# ── credential handling ───────────────────────────────────────────────────────
def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_json_atomic(path, doc):
    """Replace a credential file in one step, leaving no extra copy behind.

    os.replace is already atomic, so a sidecar backup buys nothing and costs a
    permanent second copy of a long-lived refresh token at rest. The temp file
    is created 0600 and fsynced, so a crash can never leave the CLI with a
    truncated credential file it can no longer parse.
    """
    tmp = path + ".tmp"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(tmp, flags, 0o600)
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise
        with f:
            json.dump(doc, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _pick_cred(paths, keyfn, expiry_ms=False):
    """Return (path, doc, why) — the credential file with the latest expiry.

    With expiry_ms=True the key is a real epoch-ms expiry, and copies older
    than the refresh-token lifetime are dropped outright instead of merely
    ranked last.

    That matters whenever the same account is stored in more than one place —
    a Windows profile and a WSL home, two machines on a synced folder, an old
    copy left behind by a migration. Ranking alone is safe only while every
    path is readable. As soon as the fresh file becomes unreachable (a stopped
    WSL distro, an unmounted share, a permissions change) the stale copy is
    silently promoted to best candidate, and refreshing from its long-dead
    token yields "login expired" for a login that is perfectly healthy. The
    real fault is an unreadable file; reporting it as an expired session sends
    the user off to redo a login that was never broken.
    """
    best = None
    stale = 0
    for p in paths:
        d = _read_json(p)
        if not d:
            continue
        try:
            exp = keyfn(d)
        except Exception:
            continue
        if exp is None:
            continue
        if expiry_ms and exp / 1000.0 < time.time() - AI_CRED_MAX_STALE:
            stale += 1
            continue
        if best is None or exp > best[2]:
            best = (p, d, exp)
    if best:
        return best[0], best[1], None
    return None, None, ("credentials stale or unreadable" if stale else None)


def _http_json(url, headers, data=None, timeout=AI_HTTP_TIMEOUT):
    """Returns (status, parsed_json_or_None, raw_body). Never raises."""
    try:
        req = urllib.request.Request(
            url, data=data, method="POST" if data else "GET", headers=headers)
        with _urlopen(req, timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw), raw
            except Exception:
                return resp.status, None, raw
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        try:
            return e.code, json.loads(raw), raw
        except Exception:
            return e.code, None, raw
    except Exception as e:
        return 0, None, "%s: %s" % (type(e).__name__, e)


# ── Claude ────────────────────────────────────────────────────────────────────
def _claude_token(lead=AI_REFRESH_LEAD):
    path, doc, why = _pick_cred(
        CLAUDE_CRED_PATHS, lambda d: d.get("claudeAiOauth", {}).get("expiresAt", 0),
        expiry_ms=True)
    if not doc:
        return None, why or NOT_CONFIGURED_CLAUDE
    o = doc["claudeAiOauth"]
    if "user:profile" not in (o.get("scopes") or []):
        return None, "token lacks user:profile"
    # Rotation is destructive, so refresh on a deadline rather than on every
    # call — but with real headroom (lead), not seconds. The keepalive thread
    # passes a large lead so the swap happens well before anything expires.
    if o.get("expiresAt", 0) / 1000.0 - lead > time.time():
        return o.get("accessToken"), None
    body = json.dumps({"grant_type": "refresh_token",
                       "refresh_token": o.get("refreshToken"),
                       "client_id": CLAUDE_CLIENT}).encode()
    st, p, raw = _http_json(CLAUDE_TOKEN, {
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": CLAUDE_UA}, data=body)
    if st == 200 and p and p.get("access_token"):
        o["accessToken"] = p["access_token"]
        if p.get("refresh_token"):
            o["refreshToken"] = p["refresh_token"]
        if p.get("expires_in"):
            o["expiresAt"] = int(time.time() * 1000 + p["expires_in"] * 1000)
        _write_json_atomic(path, doc)
        return o["accessToken"], None
    # Only a refusal of the grant itself proves the login is gone. A transport
    # failure (st == 0: no route, DNS, a dropped tunnel) says nothing about the
    # credential, and calling that "login expired" sends the user off to redo a
    # login that was never broken.
    if st == 0:
        return None, "offline (%s)" % (raw or "")[:40]
    if st in (400, 401) and any(k in (raw or "").lower()
                                for k in ("expired", "invalid_grant", "revoked")):
        return None, "LOGIN_EXPIRED:claude"
    return None, "refresh failed (%s)" % st


def fetch_claude_usage():
    tok, err = _claude_token()
    if not tok:
        return {"err": err}
    st, p, raw = _http_json(CLAUDE_USAGE, {
        "Authorization": "Bearer " + tok,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": CLAUDE_UA, "Accept": "application/json"})
    if _geo_blocked_body(st, raw):
        AI_LATCH.update(blocked=True, reason="Anthropic geo-block")
        return {"err": "geo blocked"}
    if st == 429:
        return {"err": "rate limited", "retry": 3600}
    if st != 200 or not p:
        return {"err": "http %s" % st}
    out = {"err": None}
    fh = p.get("five_hour") or {}
    sd = p.get("seven_day") or {}
    out["session_pct"] = fh.get("utilization")
    out["session_reset"] = fh.get("resets_at")
    out["week_pct"] = sd.get("utilization")
    out["week_reset"] = sd.get("resets_at")
    # per-model scoped limit (new `limits` array shape)
    best = None
    for lim in (p.get("limits") or []):
        if lim.get("kind") == "weekly_scoped":
            pct = lim.get("percent")
            if pct is None:
                continue
            name = ((lim.get("scope") or {}).get("model") or {}).get("display_name") or "model"
            if best is None or pct > best[1] or lim.get("is_active"):
                best = (name, pct, lim.get("resets_at"), bool(lim.get("is_active")))
    if best:
        out["model_name"], out["model_pct"] = best[0], best[1]
        out["model_reset"] = best[2]
    # legacy flat keys as fallback
    if out.get("model_pct") is None:
        for k, nm in (("seven_day_opus", "Opus"), ("seven_day_sonnet", "Sonnet")):
            v = p.get(k)
            if isinstance(v, dict) and v.get("utilization") is not None:
                out["model_name"], out["model_pct"] = nm, v["utilization"]
                out["model_reset"] = v.get("resets_at")
                break
    out["plan"] = "MAX"
    return out


# ── ChatGPT ───────────────────────────────────────────────────────────────────
def _codex_token(lead=AI_REFRESH_LEAD):
    path, doc, why = _pick_cred(CODEX_CRED_PATHS,
                                lambda d: len(json.dumps(d.get("tokens", {}))))
    if not doc or not doc.get("tokens"):
        return None, None, why or NOT_CONFIGURED_CODEX
    t = doc["tokens"]
    acct = t.get("account_id") or ""
    exp = 0
    try:  # id_token exp claim
        seg = t.get("id_token", "").split(".")[1]
        seg += "=" * (-len(seg) % 4)
        exp = json.loads(base64.urlsafe_b64decode(seg)).get("exp", 0)
    except Exception:
        pass
    if exp and exp - lead > time.time():
        return t.get("access_token"), acct, None
    body = json.dumps({"grant_type": "refresh_token",
                       "refresh_token": t.get("refresh_token"),
                       "client_id": CODEX_CLIENT,
                       "scope": "openid profile email"}).encode()
    st, p, raw = _http_json(CODEX_TOKEN, {
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": CODEX_UA}, data=body)
    if st == 200 and p and p.get("access_token"):
        t["access_token"] = p["access_token"]
        if p.get("refresh_token"):
            t["refresh_token"] = p["refresh_token"]
        if p.get("id_token"):
            t["id_token"] = p["id_token"]
        doc["last_refresh"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        _write_json_atomic(path, doc)
        return t["access_token"], acct, None
    if st == 0:
        return None, None, "offline (%s)" % (raw or "")[:40]
    if st in (400, 401):
        return None, None, "LOGIN_EXPIRED:codex"
    return None, None, "refresh failed (%s)" % st


def fetch_gpt_usage():
    tok, acct, err = _codex_token()
    if not tok:
        return {"err": err}
    h = {"Authorization": "Bearer " + tok, "User-Agent": CODEX_UA,
         "Accept": "application/json"}
    if acct:
        h["chatgpt-account-id"] = acct
    st, p, raw = _http_json(CODEX_USAGE, h)
    if _geo_blocked_body(st, raw):
        AI_LATCH.update(blocked=True, reason="OpenAI geo-block")
        return {"err": "geo blocked"}
    if st == 429:
        return {"err": "rate limited", "retry": 3600}
    if st != 200 or not p:
        return {"err": "http %s" % st}
    rl = p.get("rate_limit") or {}
    cr = p.get("credits") or {}
    out = {"err": None, "plan": (p.get("plan_type") or "plus").upper()}
    # Map windows by their declared length, never by primary/secondary position:
    # OpenAI currently sends primary=5h (18000s) and secondary=weekly (604800s),
    # but that order is not contractual.
    for key in ("primary_window", "secondary_window"):
        w = rl.get(key) or {}
        if not w:
            continue
        secs = w.get("limit_window_seconds") or 0
        slot = "sess" if secs and secs <= 86400 else "week"
        out[slot + "_pct"] = w.get("used_percent")
        out[slot + "_reset_ts"] = w.get("reset_at")
        out[slot + "_window"] = secs
    out["limit_reached"] = rl.get("limit_reached")
    out["credits"] = cr.get("balance")
    return out


# ── proactive keepalive + interactive re-login ────────────────────────────────
def ai_keepalive_once():
    """Roll both tokens forward while they are still valid.

    Runs on its own slow schedule, independent of the usage poll: the poll
    refreshes only what it is about to use, so a token can still lapse during a
    long idle stretch. Gated on the same geo verdict as everything else — a
    refresh is an outbound request to the provider and must never leave a
    blocked IP.
    """
    ok, _cc, _why = geo_verdict()
    if not ok or AI_LATCH["blocked"]:
        return
    for fn in (_claude_token, _codex_token):
        try:
            fn(lead=AI_REFRESH_LEAD)
        except Exception:
            pass


def ai_login_launch(which):
    """Open a console running the CLI so the user can complete its OAuth login.

    The login is a browser round-trip with a pasted code; it cannot be done
    headlessly, and nothing here handles a password. All this saves is opening
    a terminal and typing the command. The command comes from AI_LOGIN_CMD, is
    split with shlex, and is launched without a shell, so a value in .env
    cannot smuggle in a second command.
    """
    raw = (AI_LOGIN_CMD.get(which) or "").strip()
    if not raw:
        return False
    try:
        argv = shlex.split(raw)
    except ValueError:
        return False
    if not argv:
        return False
    try:
        # A visible, independent console: the whole point is that the user
        # interacts with it, so no CREATE_NO_WINDOW here.
        subprocess.Popen(argv, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return True
    except Exception:
        return False


# ── helpers for display ───────────────────────────────────────────────────────
def _until(ts):
    """ts = epoch seconds or ISO8601 -> compact 'in 3d 4h' / 'in 42m'."""
    try:
        if ts is None:
            return ""
        if isinstance(ts, (int, float)):
            dt = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
        else:
            dt = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
        s = (dt - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
        if s <= 0:
            return "now"
        d, r = divmod(int(s), 86400)
        h, r = divmod(r, 3600)
        m = r // 60
        if d:
            return "%dd %dh" % (d, h)
        if h:
            return "%dh %dm" % (h, m)
        return "%dm" % m
    except Exception:
        return ""


def ai_col(pct):
    """Quota bars are blue until nearly spent — colour only means 'act now'."""
    try:
        v = float(pct)
    except Exception:
        return MUT
    return ACC if v < 90 else ORG if v < 97 else RED


def tehran_now():
    """Iran has no DST since 2022 -> fixed UTC+03:30."""
    return _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=3, minutes=30)


BAR_W = 76    # every bar in the widget is exactly this wide

# Quick Checks values wrap past this width instead of being truncated, so a
# long ASN or hostname costs a second line rather than losing its tail.
Q_WRAP_PX = 190
BAR_H = 5     # ...and this tall. No exceptions — uniformity is the design.


class Bar(tk.Canvas):
    """Capsule progress track: rounded grey rail, rounded blue fill.

    A zero value still paints a small dot at the left end so an idle row
    reads as "nothing used yet" rather than as a broken or dead row.
    """

    _DOT = 5   # px of fill shown at 0% — the capsule's own end cap

    def __init__(self, parent, bg, w=BAR_W, h=BAR_H):
        tk.Canvas.__init__(self, parent, width=w, height=h,
                           bg=bg, highlightthickness=0, bd=0)
        self._bw, self._bh = w, h
        self._frac = 0.0
        try:
            _register_scalable_bar(self, w, h)
        except Exception:
            pass
        self._rail = self._capsule(0, w, TRK)
        self._fill = self._capsule(0, self._DOT, ACC)

    def _capsule(self, x0, x1, colour):
        """One pill shape: a rectangle with a half-circle cap at each end."""
        r = self._bh / 2.0
        items = [
            self.create_oval(x0, 0, x0 + self._bh, self._bh,
                             fill=colour, outline=""),
            self.create_oval(x1 - self._bh, 0, x1, self._bh,
                             fill=colour, outline=""),
            self.create_rectangle(x0 + r, 0, x1 - r, self._bh,
                                  fill=colour, outline=""),
        ]
        return items

    def _reshape(self, items, x1, colour):
        left, right, mid = items
        r = self._bh / 2.0
        self.coords(left, 0, 0, self._bh, self._bh)
        self.coords(right, x1 - self._bh, 0, x1, self._bh)
        self.coords(mid, r, 0, max(r, x1 - r), self._bh)
        for i in items:
            self.itemconfigure(i, fill=colour)

    def set(self, pct, col=None):
        try:
            value = max(0.0, min(100.0, float(pct)))
        except (TypeError, ValueError):
            value, col = 0.0, MUT
        # Remembered so a DPI rescale can redraw at the same fill level
        # without waiting for the next poll to supply the value again.
        self._frac = value / 100.0
        self._col = col
        width = max(self._DOT, int(self._bw * value / 100.0))
        self._reshape(self._fill, width, col or ai_col(value))

    def rescale(self, w, h):
        """Redraw this bar at a new pixel size, preserving its fill level.

        The capsule geometry is derived from the bar's height, so a resized
        bar has to be rebuilt rather than merely stretched: scaling the canvas
        alone would leave the end caps as ellipses of the old radius.
        """
        w = max(8, int(w))
        h = max(2, int(h))
        if (w, h) == (self._bw, self._bh):
            return
        self._bw, self._bh = w, h
        self._DOT = max(3, int(round(h)))
        self.configure(width=w, height=h)
        self._reshape(self._rail, w, TRK)
        pct = getattr(self, "_frac", 0.0) * 100.0
        self.set(pct, getattr(self, "_col", None))


class IPBar:
    def __init__(self, run=True):
        # DPI awareness must be declared before Tk creates the HWND.  Doing it
        # during monitor enumeration is too late and mixes virtualised Tk
        # coordinates with physical Win32 monitor rectangles on a 150% screen.
        screen_geom.initialize_dpi_awareness()
        self.root=tk.Tk()
        # Fonts become live objects BEFORE any widget is built, so a later
        # monitor change resizes the whole UI by reconfiguring them in place
        # rather than rebuilding it.
        self._fonts=_promote_fonts()
        self._scale=1.0
        # Rendered width at scale 1.0; the yardstick the closed loop aims at.
        self._base_px=None
        self.root.overrideredirect(True)
        self.root.attributes("-topmost",True)
        self.root.attributes("-alpha",0.97)
        self.root.configure(bg=BG)
        self.root.config(highlightthickness=1,highlightbackground=BG3)

        self._ip=None; self._ip2=None
        self._hist=[]; self._lock=False
        self._cmp=False; self._dx=self._dy=0; self._dragging=False
        self._data={}; self._tz=""
        # Assume online and correct asynchronously in _net_adopt: probing
        # adapters costs a PowerShell round-trip and must not delay the
        # first paint.
        self._net=True; self._net_busy=False; self._wiface=None
        self._fjob=None; self._spin=False; self._sjob=None
        self._ai_last=0.0; self._ai_fails=0
        self._ch=deque([0]*20,maxlen=20)
        self._rh=deque([0]*20,maxlen=20)
        self._gh=deque([0]*20,maxlen=20)

        self._build()
        # Scale before the first placement, never after: _repos measures the
        # window, and measuring it before the rescale would park a
        # differently-sized widget at the wrong corner offset.
        self._sync_scale()
        self._repos()
        reg_repoint()
        psutil.cpu_percent(interval=None)
        threading.Thread(target=self._tz_init,daemon=True).start()
        threading.Thread(target=self._net_adopt,daemon=True).start()
        # Both watchers are cheap pollers on their own daemon threads. They
        # are started after the UI exists because both call back into it.
        try:
            screen_geom.watch_displays(self._on_display_change, interval=2.0)
        except Exception:
            pass
        try:
            self._netwatch=netfast.NetWatcher(on_change=self._on_link_change,
                                              interval=1.0)
            self._netwatch.start()
        except Exception:
            self._netwatch=None
        threading.Thread(target=self._loop,daemon=True).start()
        threading.Thread(target=self._hw_loop,daemon=True).start()
        threading.Thread(target=self._ai_loop,daemon=True).start()
        threading.Thread(target=self._ai_keepalive_loop,daemon=True).start()
        if run:
            self.run()

    def run(self):
        """Enter the Tk event loop.

        Split out of __init__ so the widget can be constructed and inspected
        without blocking forever -- construction that never returns cannot be
        tested, and an untestable constructor is how the DPI bug survived as
        long as it did.
        """
        self.root.mainloop()

    # ── UI ─────────────────────────────────────────────────────────────────────
    def _build(self):
        self.card=tk.Frame(self.root,bg=BG)
        self.card.pack(fill="both",expand=True)
        self._mk_title()
        self._mk_compact()
        self._mk_full()
        self._mk_footer()
        self._mk_menu()

    def _db(self,w):
        w.bind("<ButtonPress-1>",self._ds)
        w.bind("<B1-Motion>",self._dm)
        w.bind("<ButtonRelease-1>",self._de)

    def _ds(self,e):
        if not self._lock:
            self._dragging=True
            self._dx=e.x_root-self.root.winfo_x()
            self._dy=e.y_root-self.root.winfo_y()

    def _dm(self,e):
        if not self._lock:
            self.root.geometry(f"+{e.x_root-self._dx}+{e.y_root-self._dy}")

    def _de(self,e):
        """Drag finished: match the scale of whatever panel it landed on.

        Deliberately on release rather than during motion. Rescaling mid-drag
        would resize the window under the pointer while the pointer is holding
        it, so the grab point would slide out from under the cursor as it
        crossed the monitor boundary.
        """
        if not self._lock:
            self._dragging=False
            self._sync_scale()

    GUT = 10   # side gutter, both modes
    PAD = 4    # vertical padding inside a group

    def _sep(self,p):
        tk.Frame(p,bg=HAIR,height=1).pack(fill="x")

    def _row(self,p,bg,py=4):
        # anchor="w", not fill="x": a full-width row would stretch the panel to
        # whatever the widest band needs, which is what was padding this out.
        f=tk.Frame(p,bg=SURF,padx=self.GUT,pady=py)
        f.pack(anchor="w"); self._db(f); return f

    def _group(self, parent):
        """One divider-separated group split into two content-sized columns.

        Neither column is weighted or made uniform: each ends up as wide as
        its own widest child, which keeps the group from padding itself out
        to an arbitrary share of the panel. A 1px vertical hairline sits
        between them, mirroring the horizontal dividers between groups.
        """
        self._sep(parent)
        band = tk.Frame(parent, bg=SURF)
        band.pack(anchor="w")
        self._db(band)
        left = tk.Frame(band, bg=SURF, padx=self.GUT, pady=self.PAD)
        left.grid(row=0, column=0, sticky="nw")
        tk.Frame(band, bg=HAIR, width=1).grid(row=0, column=1, sticky="ns")
        right = tk.Frame(band, bg=SURF, padx=self.GUT, pady=self.PAD)
        right.grid(row=0, column=2, sticky="nw")
        for f in (left, right):
            self._db(f)
        return left, right

    def _num(self, parent, font=F_NUM, fg=WHT, width=0, anchor="e"):
        """A refreshing number: tabular face, fixed cell, right-aligned."""
        return tk.Label(parent, text="\u2014", bg=SURF, fg=fg, font=font,
                        width=width, anchor=anchor)

    def _pill(self, parent, text, command):
        """Outlined pill button — thin stroke, generous even padding."""
        shell = tk.Frame(parent, bg=STRK, padx=1, pady=1)
        face = tk.Label(shell, text=text, bg=SURF, fg=SEC, font=F_CAP,
                        padx=6, pady=3, cursor="hand2")
        face.pack(fill="both", expand=True)
        for w in (shell, face):
            w.bind("<Button-1>", lambda e: command())
        face.bind("<Enter>", lambda e: face.config(bg=HOV, fg=WHT))
        face.bind("<Leave>", lambda e: face.config(bg=SURF, fg=SEC))
        return shell

    # Hit area is decoupled from the drawn ring so circles can sit close
    # together without shrinking the target. Height is 34: a 44pt-tall square
    # would make the footer taller than it was before, which is the opposite
    # of the goal — and vertically nothing crowds it, since the footer bar
    # itself is the only thing above and below.
    HIT_H = 34
    RING = 20               # visible circle diameter
    FSTEP = 27              # centre-to-centre spacing of footer circles;
                            # visible gap between rings is FSTEP - RING = 7px.
    #
    # Each hit target is FSTEP wide: its ring plus the full gap on either
    # side, which is the widest a target can be without covering its
    # neighbour. Genuinely overlapping canvases do not work — the one stacked
    # on top swallows clicks aimed at the ring underneath it, so buttons at
    # the edges of the row stop responding on one side.

    # Padlock glyphs. Segoe MDL2 Assets is the only family on this machine
    # that draws BOTH states as outline art matching the other footer marks;
    # the emoji codepoints render as filled blobs and the U+1F5xx escapes
    # render as tofu. Verified by rendering each candidate.
    # Read F_LCK at call time, not here: the class body executes at import,
    # before _promote_fonts swaps the module global for a live Font, so a
    # class attribute would permanently capture the unscalable tuple.
    LOCK_CLOSED = "\uE72E"
    LOCK_OPEN   = "\uE785"

    def _circle(self, parent, glyph, command, hover=WHT, font=None):
        """Footer control: small outlined circle, oversized invisible target."""
        c = tk.Canvas(parent, width=self.FSTEP, height=self.HIT_H, bg=CHR,
                      highlightthickness=0, bd=0, cursor="hand2")
        cx, cy, r = self.FSTEP / 2, self.HIT_H / 2, self.RING / 2
        ring = c.create_oval(cx - r, cy - r, cx + r, cy + r,
                             outline=STRK, fill=CHR)
        mark = c.create_text(cx, cy, text=glyph, fill=MUT,
                             font=font or F_GLY)
        # Rest colour lives on the widget so a stateful button (the lock) can
        # change it; otherwise <Leave> would reset every button to MUT and
        # wipe the active colour as soon as the pointer moved away.
        c.rest = MUT
        c.bind("<Button-1>", lambda e: command())
        c.bind("<Enter>", lambda e: (c.itemconfigure(mark, fill=hover),
                                     c.itemconfigure(ring, fill=HOV)))
        c.bind("<Leave>", lambda e: (c.itemconfigure(mark, fill=c.rest),
                                     c.itemconfigure(ring, fill=CHR)))
        c.mark, c.ring = mark, ring
        # Registered so a DPI change resizes the footer with everything else.
        # These circles are drawn geometry, not text, so neither the font pass
        # nor the padding pass touches them -- left alone they stay a fixed
        # pixel size and so shrink, relative to the card, on a dense panel.
        _SCALABLE_CIRCLES.append(c)
        return c

    @staticmethod
    def _rescale_circle(c, scale):
        """Redraw one footer circle at `scale`, from its authored geometry."""
        step = max(8, int(round(IPBar.FSTEP * scale)))
        hit = max(8, int(round(IPBar.HIT_H * scale)))
        r = max(3.0, IPBar.RING * scale / 2.0)
        cx, cy = step / 2.0, hit / 2.0
        c.configure(width=step, height=hit)
        c.coords(c.ring, cx - r, cy - r, cx + r, cy + r)
        c.coords(c.mark, cx, cy)

    def _cap(self,parent,bg,text):
        """Apple section caption: quaternary label, tight, all-caps."""
        return tk.Label(parent,text=text,bg=bg,fg=LBL,font=F_LBL,anchor="w")

    def _mk_title(self):
        self.tf=tk.Frame(self.card,bg=CHR,padx=self.GUT,pady=5)
        self.tf.pack(fill="x"); self._db(self.tf)
        tk.Label(self.tf,text="Net Watch",bg=CHR,fg=WHT,
            font=F_TTL).pack(side="left")
        self.dot=tk.Label(self.tf,text="\u25cf",bg=CHR,fg=MUT,font=F_DOT)
        self.dot.pack(side="right")

    def _mk_compact(self):
        self.cf=tk.Frame(self.card,bg=SURF)
        self._db(self.cf)

        # Row 1 — country, latency, VPN, then the IP. Everything packs left so
        # the panel width follows the content instead of stretching to an edge.
        r1=tk.Frame(self.cf,bg=SURF,padx=self.GUT); r1.pack(anchor="w",pady=(6,0))
        self._db(r1)
        self.c_fl=tk.Label(r1,text="\u2026",bg=SURF,fg=WHT,font=F_CMP,anchor="w")
        self.c_fl.pack(side="left")
        self.c_pg=tk.Label(r1,text="",bg=SURF,fg=SEC,font=F_NUS)
        self.c_pg.pack(side="left",padx=(7,0))
        self.c_vp=tk.Label(r1,text="",bg=SURF,fg=SEC,font=F_CAP)
        self.c_vp.pack(side="left",padx=(7,0))
        self.c_ip=tk.Label(r1,text="\u2026",bg=SURF,fg=WHT,font=F_NUM,cursor="hand2")
        self.c_ip.pack(side="left",padx=(7,0))
        self.c_ip.bind("<Button-1>",lambda e:self._cpyflash(self.c_ip,self._d("ip")))

        # Row 2 — ISP, secondary text under the headline.
        r2=tk.Frame(self.cf,bg=SURF,padx=self.GUT); r2.pack(anchor="w",pady=(2,0))
        self._db(r2)
        self.c_isp=tk.Label(r2,text="",bg=SURF,fg=SEC,font=F_CAP,anchor="w")
        self.c_isp.pack(side="left")

        # Row 3 — hardware.
        r3=tk.Frame(self.cf,bg=SURF,padx=self.GUT); r3.pack(anchor="w",pady=(6,0))
        self._db(r3)
        def hw(cap):
            tk.Label(r3,text=cap,bg=SURF,fg=LBL,font=F_SM).pack(side="left")
            v=tk.Label(r3,text="\u2014",bg=SURF,fg=WHT,font=F_NUS,width=4,anchor="w")
            v.pack(side="left",padx=(4,9))
            return v
        self.c_cpu=hw("CPU"); self.c_ram=hw("RAM"); self.c_gpu=hw("GPU")

        # Row 4 — AI subscription, current 5h window for each provider.
        r4=tk.Frame(self.cf,bg=SURF,padx=self.GUT); r4.pack(anchor="w",pady=(6,7))
        self._db(r4)
        def aichip(cap,first):
            f=tk.Frame(r4,bg=SURF); f.pack(side="left",padx=(0 if first else 10,0))
            self._db(f)
            tk.Label(f,text=cap,bg=SURF,fg=SEC,font=F_SM).pack(side="left")
            pct=tk.Label(f,text="\u2014",bg=SURF,fg=WHT,font=F_NUS,width=4,anchor="e")
            pct.pack(side="left",padx=(4,4))
            bar=Bar(f,SURF,w=44); bar.pack(side="left",pady=1)
            rst=tk.Label(f,text="",bg=SURF,fg=MUT,font=F_CAP)
            rst.pack(side="left",padx=(5,0))
            return pct,bar,rst
        self.c_cl,self.c_clb,self.c_clr = aichip("Claude",True)
        self.c_gt,self.c_gtb,self.c_gtr = aichip("GPT",False)

    def _mk_full(self):
        self.ff=tk.Frame(self.card,bg=SURF)
        self.ff.pack(anchor="w")
        p=self.ff

        # ── IP ADDRESS │ RUNFLARE ──
        left, right = self._group(p)
        self._cap(left,SURF,"IP ADDRESS").pack(anchor="w")
        self.ip_l=tk.Label(left,text="\u2026",bg=SURF,fg=WHT,font=F_NUM,
                           cursor="hand2",anchor="w")
        self.ip_l.pack(anchor="w",pady=(4,0))
        self.ip_l.bind("<Button-1>",lambda e:self._cpyflash(self.ip_l,self._d("ip")))
        self.ip_l.bind("<Button-3>",self._smenu)
        self.c1_l=tk.Label(left,text="\u2026",bg=SURF,fg=SEC,font=F_CTY,anchor="w")
        self.c1_l.pack(anchor="w",pady=(1,0))

        self._cap(right,SURF,"RUNFLARE").pack(anchor="w")
        self.ip2_l=tk.Label(right,text="\u2026",bg=SURF,fg=WHT,font=F_NUM,
                            cursor="hand2",anchor="w")
        self.ip2_l.pack(anchor="w",pady=(2,0))
        self.ip2_l.bind("<Button-1>",lambda e:self._cpyflash(self.ip2_l,self._d("ip2")))
        self.c2_l=tk.Label(right,text="\u2026",bg=SURF,fg=SEC,font=F_CTY,anchor="w")
        self.c2_l.pack(anchor="w",pady=(1,0))

        # ── LOCAL / GATEWAY │ both ping targets ──
        left, right = self._group(p)
        self._cap(left,SURF,"LOCAL / GATEWAY").pack(anchor="w")
        self.loc_l=tk.Label(left,text="\u2026",bg=SURF,fg=WHT,font=F_NUM,anchor="w")
        self.loc_l.pack(anchor="w",pady=(2,0))
        gwf=tk.Frame(left,bg=SURF); gwf.pack(anchor="w",pady=(2,0)); self._db(gwf)
        tk.Label(gwf,text="GW",bg=SURF,fg=LBL,font=F_SM).pack(side="left")
        self.gw_l=tk.Label(gwf,text="\u2026",bg=SURF,fg=MUT,font=F_NUS)
        self.gw_l.pack(side="left",padx=(6,0))

        def ping_target(host, first):
            """Target name, then latency and loss on the line beneath it."""
            f=tk.Frame(right,bg=SURF); f.pack(anchor="w",pady=(0,0) if first else (4,0))
            self._db(f)
            tk.Label(f,text=host,bg=SURF,fg=WHT,font=F_NUM,anchor="w").pack(anchor="w")
            line=tk.Frame(f,bg=SURF); line.pack(anchor="w",pady=(1,0)); self._db(line)
            ms=tk.Label(line,text="\u2014",bg=SURF,fg=WHT,font=F_NUS,
                        width=6,anchor="w",cursor="hand2")
            ms.pack(side="left")
            tk.Frame(line,bg=HAIR,width=1,height=10).pack(side="left",padx=4)
            loss=tk.Label(line,text="",bg=SURF,fg=MUT,font=F_NUS,anchor="w")
            loss.pack(side="left")
            return ms,loss

        self.p1_l,self.l1_l = ping_target(PING_HOST_1,True)
        self.p2_l,self.l2_l = ping_target(PING_HOST_2,False)
        self.p1_l.bind("<Button-1>",lambda e:self._cpyflash(self.p1_l,self._d("p1")))
        self.p2_l.bind("<Button-1>",lambda e:self._cpyflash(self.p2_l,self._d("p2")))

        # ── HARDWARE │ TIMEZONE ──
        # Both use a fraction of the width, so they share one band instead of
        # taking a full-width stripe each. That removes a whole section.
        left, right = self._group(p)
        self._cap(left,SURF,"HARDWARE").pack(anchor="w")
        def hwfull(cap):
            f=tk.Frame(left,bg=SURF); f.pack(anchor="w",pady=(1,0)); self._db(f)
            tk.Label(f,text=cap,bg=SURF,fg=LBL,font=F_SM,
                     width=4,anchor="w").pack(side="left")
            v=tk.Label(f,text="\u2014",bg=SURF,fg=WHT,font=F_NUS,width=4,anchor="w")
            v.pack(side="left",padx=(3,0))
            return v
        self.cpu_l=hwfull("CPU"); self.ram_l=hwfull("RAM"); self.gpu_l=hwfull("GPU")

        self._cap(right,SURF,"TIMEZONE").pack(anchor="w")
        self.tz_l=tk.Label(right,text="checking\u2026",bg=SURF,fg=SEC,font=F_SM,anchor="w")
        self.tz_l.pack(anchor="w",pady=(2,0))
        self.tzm_l=tk.Label(right,text="",bg=SURF,fg=MUT,font=F_SM,anchor="w")
        self.tzm_l.pack(anchor="w",pady=(1,0))

        # ── AI SUBSCRIPTION USAGE ──
        self._sep(p); r=self._row(p,SURF,py=5)
        hdr=tk.Frame(r,bg=SURF); hdr.pack(fill="x"); self._db(hdr)
        self._cap(hdr,SURF,"AI USAGE").pack(side="left")
        self.ai_st=tk.Label(hdr,text="\u2026",bg=SURF,fg=MUT,font=F_CAP)
        self.ai_st.pack(side="right")
        self.ai_rf=tk.Label(hdr,text="\u21bb",bg=SURF,fg=MUT,font=F_GLY,
                            cursor="hand2")
        self.ai_rf.pack(side="right",padx=(0,7))
        self.ai_rf.bind("<Button-1>",lambda e:self._ai_go())
        self.ai_rf.bind("<Enter>",lambda e:self.ai_rf.config(fg=ACC))
        self.ai_rf.bind("<Leave>",lambda e:self.ai_rf.config(fg=MUT))

        def airow(name):
            """Name, percent, bar, reset — each column sized to its content.

            Widths come from font.measure() on the longest string each column
            can ever hold, so nothing is padded out to an arbitrary constant.
            """
            f=tk.Frame(r,bg=SURF); f.pack(anchor="w",pady=(1,0)); self._db(f)
            tk.Label(f,text=name,bg=SURF,fg=SEC,font=F_SM,
                     width=12,anchor="w").pack(side="left")
            pl=self._num(f,font=F_NUS,width=4)
            pl.pack(side="left",padx=(0,5))
            b=Bar(f,SURF); b.pack(side="left",pady=1)
            # Packed against the bar, not the panel edge: a right-anchored
            # reset label would stretch the whole widget to fit dead space.
            rl=tk.Label(f,text="",bg=SURF,fg=MUT,font=F_CAP,anchor="w")
            rl.pack(side="left",padx=(6,0))
            return pl,rl,b

        self.ai_cl_s,self.ai_cl_sr,self.ai_cl_sb = airow("Claude \u00b7 5h")
        self.ai_cl_w,self.ai_cl_wr,self.ai_cl_wb = airow("Claude \u00b7 week")
        self.ai_cl_m,self.ai_cl_mr,self.ai_cl_mb = airow("Claude \u00b7 model")
        self.ai_gt_s,self.ai_gt_sr,self.ai_gt_sb = airow("GPT \u00b7 5h")
        self.ai_gt_w,self.ai_gt_wr,self.ai_gt_wb = airow("GPT \u00b7 week")
        self.ai_note=tk.Label(r,text="",bg=SURF,fg=SEC,font=F_SM,anchor="w")
        self.ai_note.pack(anchor="w",pady=(3,0))
        # Becomes clickable only while _ai_login_which is set (see _ai_show).
        self._ai_login_which=None
        self.ai_note.bind("<Button-1>",self._ai_login_click)

        # ── QUICK CHECKS — two columns of readouts, then one row of pills ──
        self._sep(p); r=self._row(p,SURF,py=5)
        hdr=tk.Frame(r,bg=SURF); hdr.pack(fill="x"); self._db(hdr)
        self._cap(hdr,SURF,"QUICK CHECKS").pack(side="left")
        rb=tk.Label(hdr,text="Run",bg=SURF,fg=ACC,font=F_CAP,cursor="hand2")
        rb.pack(side="right")
        rb.bind("<Button-1>",lambda e:threading.Thread(target=self._checks,daemon=True).start())
        cb=tk.Label(hdr,text="Clear",bg=SURF,fg=MUT,font=F_CAP,cursor="hand2")
        cb.pack(side="right",padx=(0,8))
        cb.bind("<Button-1>",lambda e:self._checks_clear())
        cb.bind("<Enter>",lambda e:cb.config(fg=ACC))
        cb.bind("<Leave>",lambda e:cb.config(fg=MUT))

        # Columns are sized to their own content, not stretched to share the
        # panel: no weight and no uniform, so each is as wide as its widest
        # label plus value, with one fixed gutter between them.
        cols=tk.Frame(r,bg=SURF); cols.pack(anchor="w",pady=(3,0)); self._db(cols)
        qL=tk.Frame(cols,bg=SURF); qL.grid(row=0,column=0,sticky="nw",padx=(0,8))
        tk.Frame(cols,bg=HAIR,width=1).grid(row=0,column=1,sticky="ns")
        qR=tk.Frame(cols,bg=SURF); qR.grid(row=0,column=2,sticky="nw",padx=(8,0))
        for f in (qL,qR): self._db(f)

        def crow(col,lbl):
            """Label left, value right.

            The value cell has no fixed width: empty it is one em-dash wide, so
            the section stays small before a run, and it grows to whatever the
            result needs afterwards. Values past Q_WRAP_PX wrap to a second
            line rather than being cut, so a string is never silently clipped.
            """
            f=tk.Frame(col,bg=SURF); f.pack(anchor="w",pady=1); self._db(f)
            tk.Label(f,text=lbl,bg=SURF,fg=SEC,font=F_SM,
                     width=11,anchor="w").pack(side="left")
            v=tk.Label(f,text="\u2014",bg=SURF,fg=MUT,font=F_NUS,
                       anchor="w",justify="left")
            v.pack(side="left")
            return v

        self.q_asn=crow(qL,"ASN");        self.q_isp=crow(qL,"ISP")
        self.q_prx=crow(qL,"Proxy / VPN"); self.q_dc=crow(qL,"Datacenter")
        self.q_hst=crow(qR,"Hostname");   self.q_dns=crow(qR,"DNS")
        self.q_sc=crow(qR,"Score")

        # Pills size to their own text. Stretching them to fill the row was
        # setting a floor on the whole panel's width.
        pills=tk.Frame(r,bg=SURF); pills.pack(anchor="w",pady=(6,1)); self._db(pills)
        targets=[
            ("BGP",    lambda:f"https://bgpview.io/ip/{self._d('ip')}"),
            ("IQPS",   lambda:f"https://ipqualityscore.com/free-ip-lookup-proxy-vpn-test/lookup/{self._d('ip')}"),
            ("Scam",   lambda:f"https://scamalytics.com/ip/{self._d('ip')}"),
            ("bgp.he", lambda:f"https://bgp.he.net/ip/{self._d('ip')}#_bgpmap"),
            ("Ping",   lambda:f"https://ping.pe/{self._d('ip')}"),
        ]
        for i,(name,url) in enumerate(targets):
            self._pill(pills,name,lambda u=url:ourl(u())).pack(
                side="left",padx=(0 if i==0 else 4,0))

    def _mk_footer(self):
        tk.Frame(self.card,bg=HAIR,height=1).pack(side="bottom",fill="x")
        # Tighter than the body gutter: five 44px hit areas already set this
        # bar's width floor, so every spare pixel here is width for the panel.
        ft=tk.Frame(self.card,bg=CHR,padx=6,pady=0)
        ft.pack(side="bottom",fill="x"); self._db(ft); self.ft=ft

        self.t_l=tk.Label(ft,text="--:--:--",bg=CHR,fg=SEC,font=F_NUS)
        self.t_l.pack(side="left")
        tk.Label(ft,text="\u00b7",bg=CHR,fg=LBL,font=F_SM).pack(side="left",padx=3)
        self.thr_l=tk.Label(ft,text="",bg=CHR,fg=MUT,font=F_NUS)
        self.thr_l.pack(side="left")
        self.v_l=tk.Label(ft,text="",bg=CHR,fg=ACC,font=F_SM)
        self.v_l.pack(side="left",padx=(4,0))

        # Abutting FSTEP-wide targets, left to right: mode, lock, net,
        # refresh, close. They tile with no padding, so every pixel of the
        # row belongs to exactly one button and none can shadow another.
        self.mb=self._circle(ft,"\u25a3",self._tog_cmp)
        self.lk=self._circle(ft,self.LOCK_OPEN,self._tog_lock,
                             font=F_LCK)
        self.nb=self._circle(ft,"\u25c9",
            lambda:threading.Thread(target=self._net_tog,daemon=True).start())
        self.rb=self._circle(ft,"\u21bb",self._refresh_go)
        self.cb=self._circle(ft,"\u2715",self.root.destroy,hover=RED)
        for w in (self.cb,self.rb,self.nb,self.lk,self.mb):
            w.pack(side="right")
        self._net_paint()
        tk.Frame(ft,bg=HAIR,width=1,height=16).pack(side="right",padx=(0,2))

    def _mk_menu(self):
        self._m=tk.Menu(self.root,tearoff=0,bg=BG2,fg=WHT,
            activebackground=ACC,activeforeground=WHT,font=F_BTN,bd=0)
        self._m.add_command(label="Copy Public IP",command=lambda:clip(self._d("ip")))
        self._m.add_command(label="Copy Local IP", command=lambda:clip(self._d("local")))
        self._m.add_command(label="Open on Map",   command=lambda:ourl(f"https://www.openstreetmap.org/search?query={self._d('ip')}"))
        self._m.add_command(label="View History",  command=self._history)
        self._m.add_command(label="Open Log",      command=self._open_log)
        self._m.add_separator()
        self._m.add_command(label="Compact / Full",command=self._tog_cmp)
        self._m.add_command(label="Refresh",       command=self._refresh_go)
        self._m.add_command(label="Toggle Lock",   command=self._tog_lock)
        self._m.add_separator()
        self._autostart_index = self._m.index("end") + 1
        self._m.add_command(command=self._tog_autostart)
        self._sync_autostart_label()
        self._m.add_separator()
        self._m.add_command(label="Quit",          command=self.root.destroy)
        self.root.bind("<Button-3>",self._smenu)
        self.card.bind("<Button-3>",self._smenu)

    # ── Actions ────────────────────────────────────────────────────────────────
    def _d(self,k): return self._data.get(k,"?")

    def _repos(self):
        """Park the widget at the bottom-right corner of its own monitor.

        Size is read AFTER any rescale, never cached across one: _apply_scale
        changes the widget's pixel dimensions, and a corner computed from a
        stale size hangs off the screen edge by exactly the scale factor.
        """
        self.root.update_idletasks()
        w=self.root.winfo_reqwidth(); h=self.root.winfo_reqheight()
        try:
            px,py=self._scaled_pad()
            x,y=screen_geom.bottom_right(w,h,pad_x=px,pad_y=py,
                                         mon=self._target_monitor())
        except Exception:
            sw=self.root.winfo_screenwidth(); sh=self.root.winfo_screenheight()
            x,y=sw-w-PAD_R, sh-h-PAD_B
        self.root.geometry(f"+{x}+{y}")

    def _target_monitor(self):
        """The monitor the widget sits on, or the primary if it is stranded.

        Placement follows the window rather than always snapping to primary,
        so dragging the widget to another screen and letting it re-park keeps
        it there instead of yanking it back.
        """
        try:
            x,y=self.root.winfo_x(), self.root.winfo_y()
            w=self.root.winfo_width() or self.root.winfo_reqwidth()
            h=self.root.winfo_height() or self.root.winfo_reqheight()
            if screen_geom.rect_visible_on(x,y,w,h):
                return screen_geom.monitor_at(x+w//2, y+h//2)
        except Exception:
            pass
        return screen_geom.primary()

    def _scaled_pad(self):
        """Corner margins in the target monitor's own pixels.

        A flat 16px gap is physically twice as tight on a 200 ppi panel as on
        a 100 ppi one, so the padding scales with everything else or the
        widget visibly hugs the corner on the denser screen.
        """
        s=getattr(self,"_scale",1.0)
        return (max(1,int(round(PAD_R*s))), max(1,int(round(PAD_B*s))))

    def _apply_scale(self, scale, force=False):
        """Resize every font and bar so the widget is the same PHYSICAL size.

        Returns True when something actually changed, so callers can skip a
        needless reposition. Scale is clamped: a bad EDID reading should make
        the widget slightly wrong, never make it fill or vanish from the
        screen.
        """
        scale=max(0.6,min(4.0,float(scale)))
        if not force and abs(scale-getattr(self,"_scale",0.0))<0.02:
            return False
        self._scale=scale
        if not hasattr(self,"_base_fonts"):
            # Captured once from the authored values. Rescaling from the
            # CURRENT sizes would compound rounding error on every monitor
            # change until the text drifted permanently out of proportion.
            self._base_fonts={n:f.cget("size") for n,f in self._fonts.items()}
        for name,f in self._fonts.items():
            base=self._base_fonts[name]
            size=max(1,int(round(abs(base)*scale)))
            f.configure(size=size if base>0 else -size)
        for circle in _SCALABLE_CIRCLES:
            try:
                self._rescale_circle(circle, scale)
            except Exception:
                pass
        for canvas,bw,bh in _SCALABLE_BARS:
            try:
                canvas.rescale(int(round(bw*scale)), int(round(bh*scale)))
            except Exception:
                pass
        if not hasattr(self,"_base_pads"):
            self._base_pads=_capture_pads(self.root)
        _scale_pads(self._base_pads, scale)
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        return True

    def _sync_scale(self):
        """Match the target monitor's scale without undoing a manual drag.

        Scaling changes the requested pixel dimensions. Preserve the dropped
        window centre across that resize and clamp only when the larger card
        would cross the target monitor's working area.
        """
        try:
            self.root.update_idletasks()
            old_w=self.root.winfo_width() or self.root.winfo_reqwidth()
            old_h=self.root.winfo_height() or self.root.winfo_reqheight()
            centre=(self.root.winfo_x()+old_w/2,
                    self.root.winfo_y()+old_h/2)
            mon=self._target_monitor()
            if mon is None:
                return
            want=target_scale(mon)
            if not self._apply_scale(want):
                return
            # Font sizes are whole points, so the rendered width lands a few
            # percent either side of the request -- enough to be visible as
            # "this panel looks bigger". Measure what we actually got and
            # correct once, closing the loop instead of trusting the request.
            if self._base_px is None:
                # Measure the unscaled width for THIS layout. _apply_scale has
                # already moved us, so drop back to 1.0 to read the yardstick,
                # otherwise the target compounds with the current scale and
                # the widget grows every single sync.
                self._apply_scale(1.0, force=True)
                self._base_px=self.root.winfo_reqwidth()
                self._apply_scale(want, force=True)
            target=self._base_px*want
            rendered=self.root.winfo_reqwidth()
            if rendered:
                # One measured correction avoids walking the live HWND through
                # eight visibly different sizes on every monitor transition.
                correction=max(0.95,min(1.05,target/rendered))
                corrected=self._scale*correction
                if abs(corrected-self._scale)>=0.005:
                    self._apply_scale(corrected, force=True)

            self.root.update_idletasks()
            new_w=self.root.winfo_reqwidth(); new_h=self.root.winfo_reqheight()
            x=int(round(centre[0]-new_w/2)); y=int(round(centre[1]-new_h/2))
            max_x=mon["wx"]+max(0,mon["ww"]-new_w)
            max_y=mon["wy"]+max(0,mon["wh"]-new_h)
            x=max(mon["wx"],min(x,max_x))
            y=max(mon["wy"],min(y,max_y))
            self.root.geometry(f"+{x}+{y}")
        except Exception:
            pass


    def _clamp_to_work_area(self):
        """Keep post-refresh content growth inside the current work area."""
        if getattr(self,"_dragging",False):
            return
        try:
            self.root.update_idletasks()
            mon=self._target_monitor()
            if mon is None:
                return
            x=self.root.winfo_x(); y=self.root.winfo_y()
            w=self.root.winfo_width() or self.root.winfo_reqwidth()
            h=self.root.winfo_height() or self.root.winfo_reqheight()
            max_x=mon["wx"]+max(0,mon["ww"]-w)
            max_y=mon["wy"]+max(0,mon["wh"]-h)
            nx=max(mon["wx"],min(x,max_x)); ny=max(mon["wy"],min(y,max_y))
            if (nx,ny)!=(x,y):
                self.root.geometry(f"+{nx}+{ny}")
        except Exception:
            pass

    def _rescue_offscreen(self):
        """Drag the window back onto a live monitor if its own is gone.

        Unplugging the HDMI cable does not move the windows that were on that
        monitor: Windows leaves them at coordinates that now belong to no
        display, so the widget is simply invisible and unreachable with the
        mouse. Nothing about the widget's own state changes, which is why it
        cannot notice on its own and has to be told by a display watcher.

        A position that is still reachable is left exactly where the user put
        it. Only a stranded one is moved, and never while the window is being
        dragged, which would fight the pointer.
        """
        try:
            self.root.update_idletasks()
            x,y=self.root.winfo_x(), self.root.winfo_y()
            w,h=self.root.winfo_width() or self.root.winfo_reqwidth(), \
                self.root.winfo_height() or self.root.winfo_reqheight()
            if screen_geom.rect_visible_on(x,y,w,h):
                return
            px,py=self._scaled_pad()
            nx,ny=screen_geom.bottom_right(w,h,pad_x=px,pad_y=py)
            self.root.geometry(f"+{nx}+{ny}")
            toast("Display changed","Widget moved back to the main screen")
        except Exception:
            pass

    def _on_display_change(self):
        """Display-watcher callback. Arrives on a worker thread; Tk is not
        thread-safe, so the real work is marshalled onto the UI thread.

        The delay lets the desktop settle: immediately after a hotplug Windows
        is still resizing work areas, and a position computed mid-flight can be
        wrong by a taskbar's height.
        """
        try:
            self.root.after(400, self._rescue_offscreen)
            # Rescale after the rescue, so the panel measured is the one the
            # window has actually landed on.
            self.root.after(600, self._sync_scale)
        except Exception:
            pass

    def _smenu(self,e):
        try: self._m.tk_popup(e.x_root,e.y_root)
        finally: self._m.grab_release()

    def _cpyflash(self,lbl,txt):
        clip(txt); orig=lbl.cget("fg"); lbl.config(fg=WHT)
        self.root.after(200,lambda:lbl.config(fg=orig))

    def _tog_lock(self):
        self._lock=not self._lock
        self.lk.rest = ACC if self._lock else MUT
        self.lk.itemconfigure(
            self.lk.mark,
            text=self.LOCK_CLOSED if self._lock else self.LOCK_OPEN,
            fill=self.lk.rest)

    def _open_log(self):
        """Open the log with the shell, but only if it is really a log file.

        LOG_FILE comes from .env, and os.startfile runs the shell verb for
        whatever it is handed — a .env copied from a gist that points this at
        an .exe would launch it. The extension check keeps that from working.
        """
        if not os.path.exists(LOG_FILE):
            return
        if os.path.splitext(LOG_FILE)[1].lower() not in (".txt", ".log"):
            return
        os.startfile(LOG_FILE)

    def _sync_autostart_label(self):
        on = reg_current() is not None
        self._m.entryconfigure(
            self._autostart_index,
            label="Disable Autostart" if on else "Enable Autostart")

    def _tog_autostart(self):
        if reg_current() is None:
            reg_set(_autostart_command())
        else:
            reg_remove()
        self._sync_autostart_label()

    def _tog_cmp(self):
        self._cmp=not self._cmp
        if self._cmp:
            self.ff.pack_forget()
            self.cf.pack(fill="x",after=self.tf)
        else:
            self.cf.pack_forget()
            self.ff.pack(fill="x",after=self.tf)
        # Compact and full mode are different widths, so the yardstick the
        # scale loop measures against no longer applies -- drop it and let the
        # next sync re-derive it for the mode now on screen.
        self._base_px=None
        self.root.after(30,self._sync_scale)

    def _net_adopt(self):
        """Correct the net dot from the machine's real state, off the UI thread.

        The widget may be starting up after a cut made in a previous run, and a
        green dot over a dead network would make the first click cut nothing
        and look broken.
        """
        try:
            down = net_is_down()
        except Exception:
            return
        if down and self._net and not self._net_busy:
            self._net = False
            self.root.after(0, self._net_paint)

    def _net_paint(self):
        """Green connected, red cut, amber while a toggle is in flight."""
        col = ORG if self._net_busy else (GRN if self._net else RED)
        self.nb.rest = col
        self.nb.itemconfigure(self.nb.mark, fill=col)

    def _net_tog(self):
        """Cut or restore every physical NIC, wired and wireless alike.

        This used to call `netsh wlan set autoconfig enabled=no`, which reaches
        Wi-Fi and nothing else — so on a machine sitting on an Ethernet cable
        the button appeared to do nothing whatsoever. Disabling the adapters
        covers both, at the cost of needing administrator rights the widget
        does not have, so each toggle raises one UAC prompt. Declining it is an
        ordinary outcome: the network is left exactly as it was.
        """
        if self._net_busy:
            return
        self._net_busy = True
        self.root.after(0, self._net_paint)
        try:
            ok, msg = net_cut() if self._net else net_restore()
        except Exception:
            ok, msg = False, "error"
        finally:
            self._net_busy = False

        if ok:
            self._net = not self._net
        else:
            # Re-read the world rather than trusting the flag: a partial
            # failure can leave some adapters down and others up.
            try:
                self._net = not net_is_down()
            except Exception:
                pass
            self.root.after(0, lambda: toast("Network", msg))

        self.root.after(0, self._net_paint)
        if ok and self._net:
            # Adapters need a few seconds to associate and take a DHCP lease.
            self.root.after(6000, self._refresh_go)

    def _flash(self,steps=None):
        if steps is None:
            steps=FLASH_N*2
            if self._fjob: self.root.after_cancel(self._fjob)
        if steps<=0:
            self.root.config(highlightthickness=1,highlightbackground=BG3)
            self._restore(); return
        self.root.config(highlightthickness=1,
            highlightbackground=ACC if steps%2==0 else BG3)
        self._fjob=self.root.after(FLASH_MS,lambda:self._flash(steps-1))

    def _hw_alert(self):
        """Pulse the hairline orange when CPU/RAM/GPU >90%."""
        if self._fjob: return  # already flashing
        def do(steps=6):
            if steps<=0:
                self.root.config(highlightthickness=1,highlightbackground=BG3)
                return
            col=ORG if steps%2==0 else BG3
            self.root.config(highlightthickness=1,highlightbackground=col)
            self.root.after(200,lambda:do(steps-1))
        self.root.after(0,do)

    def _restore(self):
        try:
            p1=self._data.get("p1","?"); p2=self._data.get("p2","?")
            ip=self._data.get("ip","?"); ip2=self._data.get("ip2","?")
            vpn=ip not in ("?","Error") and ip2 not in ("?","Error") and ip!=ip2
            self.ip_l.config(fg=WHT); self.ip2_l.config(fg=WHT)
            self.c1_l.config(fg=SEC); self.c2_l.config(fg=SEC)
            self.p1_l.config(fg=pcol(p1)); self.p2_l.config(fg=pcol(p2))
            self.dot.config(fg=RED if (pcol(p1)==RED or pcol(p2)==RED) else MUT)
            self.v_l.config(text="VPN" if vpn else "Direct",
                            fg=ACC if vpn else MUT)
        except Exception: pass

    def _history(self):
        w=tk.Toplevel(self.root); w.title("IP History"); w.configure(bg=BG)
        w.attributes("-topmost",True); w.resizable(False,False)
        tk.Label(w,text="IP Change History",bg=BG,fg=WHT,
            font=F_TST,padx=14,pady=10).pack(anchor="w")
        if not self._hist:
            tk.Label(w,text="No changes yet.",bg=BG,fg=MUT,font=F_SM,padx=14,pady=6).pack(anchor="w")
        for ts,ip in reversed(self._hist):
            row=tk.Frame(w,bg=BG); row.pack(fill="x",padx=14,pady=2)
            tk.Label(row,text=ts,bg=BG,fg=MUT,font=F_MN,width=20,anchor="w").pack(side="left")
            tk.Label(row,text=ip,bg=BG,fg=ACC,font=F_MN).pack(side="left",padx=6)
        tk.Button(w,text="Close",command=w.destroy,bg=BG2,fg=WHT,
            activebackground=BG3,activeforeground=WHT,font=F_BTN,
            relief="flat",bd=0,padx=14,pady=5,cursor="hand2").pack(pady=10)

    # ── Quick Checks ───────────────────────────────────────────────────────────
    def _qset(self, label, text, fg):
        """Show a result in full, wrapping past Q_WRAP_PX instead of cutting."""
        label.config(text=text, fg=fg,
                     wraplength=Q_WRAP_PX if text else 0)
        self._repos()

    def _checks_clear(self):
        """Wipe results back to em-dashes and shrink the section to its
        pre-run size. wraplength=0 turns wrapping off so each cell collapses
        back to a single dash."""
        for lbl in (self.q_asn, self.q_isp, self.q_prx, self.q_dc,
                    self.q_hst, self.q_dns, self.q_sc):
            lbl.config(text="\u2014", fg=MUT, wraplength=0)
        self._repos()

    def _checks(self):
        ip=self._d("ip")
        if ip in ("?","Error","\u2026",""): return
        def ui(fn): self.root.after(0,fn)
        for lbl in [self.q_asn,self.q_isp,self.q_prx,self.q_dc,self.q_hst,self.q_dns,self.q_sc]:
            ui(lambda l=lbl:l.config(text="loading...",fg=MUT))
        res=fetch_vpn(ip); hn,cy=fetch_host(ip)
        def show(r=res,h=hn,c=cy):
            if r is None:
                # Every readout gets the failure, not just the left column:
                # leaving the right column on stale values misreports the run.
                for lbl in [self.q_asn,self.q_isp,self.q_prx,self.q_dc,
                            self.q_hst,self.q_dns,self.q_sc]:
                    lbl.config(text="failed",fg=RED)
                self._repos(); return
            asn=r.get("as","") or ""
            self._qset(self.q_asn, asn or "\u2014", SEC if asn else MUT)
            isp=r.get("isp","") or r.get("org","") or ""
            self._qset(self.q_isp, isp or "\u2014", SEC if isp else MUT)
            self._qset(self.q_prx, "Proxy" if r.get("proxy") else "Clean",
                       RED if r.get("proxy") else SEC)
            self._qset(self.q_dc, "Datacenter" if r.get("hosting") else "Residential",
                       ORG if r.get("hosting") else SEC)
            hv=h or c or ""
            self._qset(self.q_hst, hv or "\u2014", SEC if hv else MUT)
            cc=r.get("countryCode","?")
            srv=dns_servers(); dleak,dinfo=dns_leak(cc,srv)
            dt=" | ".join(dinfo) if dinfo else "\u2014"
            self._qset(self.q_dns, ("LEAK: "+dt) if dleak else dt,
                       RED if dleak else SEC)
            ms=pms(self._data.get("p1","?")); lo=self._data.get("loss1",0)
            sc=net_score(ms,lo,dleak); sc_c=SEC if sc>=55 else ORG if sc>=35 else RED
            self._qset(self.q_sc, f"{sc}/100", sc_c)
        ui(show)

    # ── Timezone ───────────────────────────────────────────────────────────────
    def _tz_init(self):
        self._tz=get_tz(); self.root.after(0,self._tz_show)

    def _tz_show(self):
        tz=self._tz
        if not tz: self.tz_l.config(text="Unknown",fg=MUT); return
        sus=tz in SUSPECT
        icon="\u26a0 " if sus else "\u25f4 "
        self.tz_l.config(text=f"{icon}{tz}",fg=RED if sus else SEC)
        self._tz_match()

    def _tz_match(self):
        tz=self._tz; ip_cc=self._data.get("code","?")
        if not tz or ip_cc in ("?",""): self.tzm_l.config(text="",fg=MUT); return
        tz_cc=TZ_CC.get(tz,"")
        if not tz_cc: self.tzm_l.config(text="TZ: region unknown",fg=MUT); return
        if tz_cc==ip_cc:
            self.tzm_l.config(text=f"TZ matches IP ({ip_cc})",fg=MUT)
        else:
            self.tzm_l.config(text=f"\u26a0 TZ={tz_cc} \u2260 IP={ip_cc}",fg=ORG)

    # ── Hardware ───────────────────────────────────────────────────────────────
    def _hw_loop(self):
        while True:
            cpu,ram,gpu=hw()
            self._ch.append(cpu); self._rh.append(ram)
            try: self._gh.append(float(str(gpu).replace("%","")))
            except Exception: self._gh.append(0)
            ch=list(self._ch); rh=list(self._rh); gh=list(self._gh)
            def upd(c=cpu,r=ram,g=gpu,ch=ch,rh=rh,gh=gh):
                self.cpu_l.config(text=f"{c:.0f}%",fg=ucol(c))
                self.ram_l.config(text=f"{r:.0f}%",fg=ucol(r))
                self.gpu_l.config(text=g,fg=ucol(g) if g!="N/A" else MUT)
                gpu_pct_str = str(g).replace("%","").strip()
                gpu_short   = f"{gpu_pct_str}%" if gpu_pct_str.isdigit() else (g if g!="N/A" else "N/A")
                try:
                    gv=float(gpu_pct_str)
                except Exception:
                    gv=0
                self.c_cpu.config(text=f"{c:.0f}%",fg=ucol(c))
                self.c_ram.config(text=f"{r:.0f}%",fg=ucol(r))
                self.c_gpu.config(text=gpu_short,fg=ucol(gpu_pct_str))
                # Alert if any >90%
                if c>90 or r>90 or gv>90:
                    self._hw_alert()
            self.root.after(0,upd)
            time.sleep(2)

    # ── AI usage ───────────────────────────────────────────────────────────────
    def _ai_go(self):
        """Manual refresh (respects the same guards + min interval)."""
        threading.Thread(target=lambda:self._ai_tick(manual=True),daemon=True).start()

    def _ai_loop(self):
        time.sleep(8)  # let the first IP refresh land so geo is already known
        while True:
            wait=self._ai_tick()
            time.sleep(max(AI_POLL_MIN,wait))

    def _ai_tick(self,manual=False):
        """One poll cycle. Returns seconds to wait before the next one."""
        if not AI_ENABLED:
            return 3600
        now=time.time()
        if manual and now-self._ai_last<AI_POLL_MIN:
            self._ai_ui_status("wait %ds"%int(AI_POLL_MIN-(now-self._ai_last)),YLW)
            return AI_POLL_BASE

        if AI_LATCH["blocked"]:
            self._ai_ui_off("\u26d4 "+AI_LATCH["reason"]+" \u2014 disabled")
            return 3600

        ok,cc,why=geo_verdict(self._data.get("ip"),self._data.get("code"),force=manual)
        if not ok:
            msg=("\u26a0 VPN required" if cc=="IR"
                 else "\u26a0 VPN required (%s)"%why)
            self._ai_ui_off(msg)
            return 120 if cc!="IR" else 90

        self._ai_last=now
        self._ai_ui_status("polling\u2026",MUT)
        cl=fetch_claude_usage()
        time.sleep(_rnd.uniform(1.5,5.0))   # don't fire both at the same instant
        gt=fetch_gpt_usage()

        # re-verify geo: if the tunnel dropped mid-cycle, throw the data away
        ok2,cc2,_=geo_verdict(force=True)
        if not ok2:
            self._ai_ui_off("\u26a0 VPN required")
            return 90

        self._ai_show(cl,gt)
        # Both CLIs unconfigured: nothing to poll. Check back slowly in case
        # the user logs in, but do not treat it as a failure to back off from.
        if cl.get("err") in NOT_CONFIGURED and gt.get("err") in NOT_CONFIGURED:
            self._ai_fails=0
            return 900
        bad=(cl.get("err") or gt.get("err"))
        retry=max(cl.get("retry",0),gt.get("retry",0))
        if retry:
            self._ai_fails+=1
            return retry
        if bad:
            self._ai_fails+=1
            return min(AI_FAIL_BACKOFF*self._ai_fails,4*3600)
        self._ai_fails=0
        return AI_POLL_BASE*(1+_rnd.uniform(-AI_POLL_JITTER,AI_POLL_JITTER))

    def _ai_keepalive_loop(self):
        """Roll tokens forward on a slow clock, independent of the usage poll.

        Separate from _ai_loop on purpose: the poll backs off hard after
        failures and stops entirely when both CLIs are unconfigured, which is
        exactly when a token would be left to rot.
        """
        time.sleep(20)
        while True:
            try: ai_keepalive_once()
            except Exception: pass
            time.sleep(AI_KEEPALIVE)

    def _ai_login_click(self,_e=None):
        """Open the CLI's login console, then poll until it lands."""
        which=self._ai_login_which
        if not which: return
        if not ai_login_launch(which):
            self.ai_note.config(text="cannot launch %s login \u2014 set AI_LOGIN_CMD_%s in .env"
                                     %(which,which.upper()),fg=RED)
            return
        self.ai_note.config(text="finish sign-in in the console window\u2026",
                            fg=ORG,cursor="")
        self._ai_login_which=None
        def watch():
            # The CLI writes new credentials only after the browser round-trip,
            # so poll rather than assuming the launch succeeded.
            for _ in range(60):
                time.sleep(10)
                fn=_claude_token if which=="claude" else _codex_token
                try: res=fn()
                except Exception: continue
                if res[0]:
                    # Fresh token: clear the failure backoff the dead login
                    # built up and poll immediately, rather than waiting out
                    # the multi-hour retry interval.
                    self._ai_fails=0
                    try: self._ai_tick()
                    except Exception: pass
                    return
        threading.Thread(target=watch,daemon=True).start()

    def _ai_ui_status(self,txt,col):
        def go():
            try: self.ai_st.config(text=txt,fg=col)
            except Exception: pass
        self.root.after(0,go)

    def _ai_ui_off(self,msg):
        """Geo gate closed: show the warning, send nothing, keep last values dim."""
        def go():
            try:
                self.ai_st.config(text="OFF",fg=RED)
                self.ai_note.config(text=msg,fg=ORG)
                for l in (self.c_cl,self.c_gt):
                    l.config(text="VPN?",fg=ORG)
                for b in (self.c_clb,self.c_gtb):
                    b.set(0,ORG)
                self.c_clr.config(text="")
                self.c_gtr.config(text="")
            except Exception: pass
        self.root.after(0,go)

    def _ai_show(self,cl,gt):
        def go():
            try:
                notes=[]
                # A CLI that was never logged in is not an error: show the
                # one-line setup hint in muted text, not a red failure.
                cl_setup = cl.get("err") in NOT_CONFIGURED
                gt_setup = gt.get("err") in NOT_CONFIGURED
                # Claude
                if cl.get("err"):
                    notes.append("Claude: "+cl["err"])
                    self.ai_cl_s.config(text="\u2014",fg=MUT); self.ai_cl_sb.set(0,MUT)
                    self.ai_cl_w.config(text="\u2014",fg=MUT); self.ai_cl_wb.set(0,MUT)
                    self.ai_cl_m.config(text="\u2014",fg=MUT); self.ai_cl_mb.set(0,MUT)
                    if cl_setup:
                        self.c_cl.config(text="setup",fg=MUT); self.c_clb.set(0,MUT)
                    else:
                        self.c_cl.config(text="err",fg=RED); self.c_clb.set(0,RED)
                    self.c_clr.config(text="")
                else:
                    def put(lbl,rl,bar,pct,reset):
                        if pct is None:
                            lbl.config(text="\u2014",fg=MUT); rl.config(text=""); bar.set(0,MUT); return
                        lbl.config(text="%d%%"%round(pct),fg=ai_col(pct))
                        u=_until(reset); rl.config(text=("resets "+u) if u else "")
                        bar.set(pct)
                    put(self.ai_cl_s,self.ai_cl_sr,self.ai_cl_sb,cl.get("session_pct"),cl.get("session_reset"))
                    put(self.ai_cl_w,self.ai_cl_wr,self.ai_cl_wb,cl.get("week_pct"),cl.get("week_reset"))
                    put(self.ai_cl_m,self.ai_cl_mr,self.ai_cl_mb,cl.get("model_pct"),cl.get("model_reset"))
                    if cl.get("model_name"):
                        self.ai_cl_mr.config(text="%s \u2022 %s"%(cl["model_name"][:9],
                                             _until(cl.get("model_reset")) or ""))
                    # compact = current session (5h) only
                    sp=cl.get("session_pct")
                    if sp is None:
                        self.c_cl.config(text="\u2014",fg=MUT); self.c_clb.set(0,MUT)
                        self.c_clr.config(text="")
                    else:
                        self.c_cl.config(text="%d%%"%round(sp),fg=ai_col(sp))
                        self.c_clb.set(sp)
                        u=_until(cl.get("session_reset"))
                        self.c_clr.config(text=u or "")
                # ChatGPT
                if gt.get("err"):
                    notes.append("GPT: "+gt["err"])
                    self.ai_gt_w.config(text="\u2014",fg=MUT); self.ai_gt_wb.set(0,MUT)
                    self.ai_gt_s.config(text="\u2014",fg=MUT); self.ai_gt_sb.set(0,MUT)
                    if gt_setup:
                        self.c_gt.config(text="setup",fg=MUT); self.c_gtb.set(0,MUT)
                    else:
                        self.c_gt.config(text="err",fg=RED); self.c_gtb.set(0,RED)
                    self.c_gtr.config(text="")
                else:
                    p=gt.get("sess_pct")
                    if p is None:
                        self.c_gt.config(text="\u2014",fg=MUT); self.c_gtb.set(0,MUT)
                        self.c_gtr.config(text="")
                    else:
                        self.c_gt.config(text="%d%%"%round(p),fg=ai_col(p))
                        self.c_gtb.set(p)
                        self.c_gtr.config(text=_until(gt.get("sess_reset_ts")) or "")
                    # full mode rows
                    w=gt.get("week_pct")
                    if w is None:
                        self.ai_gt_w.config(text="\u2014",fg=MUT); self.ai_gt_wb.set(0,MUT)
                        self.ai_gt_wr.config(text="")
                    else:
                        self.ai_gt_w.config(text="%d%%"%round(w),fg=ai_col(w))
                        u=_until(gt.get("week_reset_ts"))
                        self.ai_gt_wr.config(text=("resets "+u) if u else "")
                        self.ai_gt_wb.set(w)
                    if p is None:
                        self.ai_gt_s.config(text="\u2014",fg=MUT); self.ai_gt_sb.set(0,MUT)
                        self.ai_gt_sr.config(text="")
                    else:
                        self.ai_gt_s.config(text="%d%%"%round(p),fg=ai_col(p))
                        u=_until(gt.get("sess_reset_ts"))
                        self.ai_gt_sr.config(text=("resets "+u) if u else "")
                        self.ai_gt_sb.set(p)
                    if gt.get("limit_reached"): notes.append("GPT limit reached!")
                plans=[]
                if not cl.get("err"): plans.append("Claude "+str(cl.get("plan","MAX")))
                if not gt.get("err"): plans.append("GPT "+str(gt.get("plan","PLUS")))
                # Setup hints are informational; only real failures go orange.
                only_setup = bool(notes) and (cl_setup or not cl.get("err")) \
                                          and (gt_setup or not gt.get("err"))
                # A dead login is the one error the user can act on from here,
                # so it becomes a clickable prompt instead of a bare message.
                self._ai_login_which=None
                for n,note in enumerate(notes):
                    m=re.search(r"LOGIN_EXPIRED:(\w+)",note)
                    if m:
                        if self._ai_login_which is None:
                            self._ai_login_which=m.group(1)
                        notes[n]=note[:m.start()]+"login expired \u2014 click to sign in"
                self.ai_note.config(text=" | ".join(notes) if notes else " \u00b7 ".join(plans),
                                    fg=(MUT if only_setup else ORG) if notes else SEC,
                                    cursor="hand2" if self._ai_login_which else "")
                self.ai_st.config(text="\u25f4 "+time.strftime("%H:%M"),
                                  fg=MUT if (not notes or only_setup) else ORG)
            except Exception: pass
        self.root.after(0,go)

    # ── Refresh ────────────────────────────────────────────────────────────────
    def _refresh_go(self):
        threading.Thread(target=self._spin_refresh,daemon=True).start()

    def _spin_refresh(self):
        FR=["\u21bb","\u21ba","\u21bb","\u21ba"]; self._spin=True
        def spin(i=0):
            if not self._spin: return
            self.rb.itemconfigure(self.rb.mark,text=FR[i%len(FR)],fill=ACC)
            self._sjob=self.root.after(150,lambda:spin(i+1))
        self.root.after(0,spin)
        self._do_refresh()
        threading.Thread(target=self._tz_init,daemon=True).start()
        self._spin=False
        if self._sjob: self.root.after(0,lambda:self.root.after_cancel(self._sjob))
        self.root.after(0,lambda:self.rb.itemconfigure(
            self.rb.mark,text="\u21bb",fill=MUT))

    def _loop(self):
        self._do_refresh()
        while True:
            time.sleep(REFRESH)
            self._do_refresh()

    def _on_link_change(self, old, new):
        """React to a local link transition without waiting for the slow cycle.

        _do_refresh cannot be quick: it joins two public-IP lookups with six
        second timeouts and a ping with a fifteen second one, so on a real
        outage every one of them blocks for its full timeout and the display
        keeps showing a connection that died twenty seconds ago. The local link
        state, by contrast, is known immediately and for free, so the dot and
        the ping fields are corrected right away and the expensive enrichment
        is merely kicked off behind it.
        """
        try:
            up = bool(new.get("up"))
            if not up:
                def dead():
                    self.dot.config(fg=RED)
                    for lbl in (self.p1_l, self.p2_l):
                        lbl.config(text="No link", fg=RED)
                    try: self.c_pg.config(text="No link", fg=RED)
                    except Exception: pass
                self.root.after(0, dead)
                toast("Network", "Link down")
            else:
                toast("Network", "Link restored")
            # A returning link or a new local address both imply the public IP
            # may have changed, and that is exactly what the user wants to be
            # told about promptly.
            if netfast.should_refresh_now(old, new):
                self._refresh_go()
            else:
                threading.Thread(target=self._do_refresh, daemon=True).start()
        except Exception:
            pass

    def _do_refresh(self):
        res={}
        def _i():  res["ip"]=fetch_ip()
        def _i2(): res["ip2"]=fetch_rf()
        def _p1(): res["p1"]=ping(PING_HOST_1)
        def _p2(): res["p2"]=ping(PING_HOST_2)
        def _lo(): res["lo"]=local_ip()
        def _gw(): res["gw"]=gateway()
        ts=[threading.Thread(target=fn,daemon=True) for fn in (_i,_i2,_p1,_p2,_lo,_gw)]
        for t in ts: t.start()
        for t in ts: t.join()

        info=res.get("ip",{}); nip=info.get("ip","?"); nip2=res.get("ip2","?")
        cc=info.get("cc","?"); isp=info.get("isp","")
        p1,l1=res.get("p1",("?",100)); p2,l2=res.get("p2",("?",100))
        lo=res.get("lo","?"); gw=res.get("gw","?")

        f1,n1,c1=clabel(cc,nip); f2,n2,c2=clabel(cc,nip2)

        chg=(self._ip is not None and nip!=self._ip and nip not in ("?","Error"))
        if chg:
            self._hist.append((time.strftime("%Y-%m-%d %H:%M:%S"),nip))
            if len(self._hist)>HIST_MAX: self._hist.pop(0)
            log(self._ip,nip); toast("IP Changed",f"{self._ip} -> {nip}")
        if nip  not in ("?","Error"): self._ip=nip
        if nip2 not in ("?","Error"): self._ip2=nip2

        vpn=nip not in ("?","Error") and nip2 not in ("?","Error") and nip!=nip2
        bp=p1 if pcol(p1)!=RED else p2

        self._data=dict(ip=nip,ip2=nip2,p1=p1,p2=p2,loss1=l1,loss2=l2,
                        local=lo,gw=gw,code=c1,isp=isp)

        def upd():
            self.ip_l.config(text=nip)
            self.c1_l.config(text=f"{f1}  {n1}")
            self.ip2_l.config(text=nip2)
            self.c2_l.config(text=f"{f2}  {n2}")
            self.loc_l.config(text=lo)
            self.gw_l.config(text=gw)
            self.p1_l.config(text=p1,fg=pcol(p1))
            self.l1_l.config(text=f"{l1}% loss",fg=RED if l1>0 else MUT)
            self.p2_l.config(text=p2,fg=pcol(p2))
            self.l2_l.config(text=f"{l2}% loss",fg=RED if l2>0 else MUT)
            self.dot.config(fg=RED if (pcol(p1)==RED or pcol(p2)==RED) else MUT)
            self.t_l.config(text=time.strftime("%H:%M:%S"))
            try: self.thr_l.config(text="THR "+tehran_now().strftime("%H:%M"))
            except Exception: pass
            self.v_l.config(text="VPN" if vpn else "Direct",
                            fg=ACC if vpn else MUT)
            # compact bar
            try:
                self.c_fl.config(text=f"{f1} {n1[:16]}",fg=WHT)
                self.c_pg.config(text=bp,fg=pcol(bp))
                self.c_vp.config(text="VPN" if vpn else "Direct",
                                 fg=ACC if vpn else MUT)
                self.c_ip.config(text=nip,fg=WHT)
                self.c_isp.config(text=f"{nip2}  \u2022  {isp[:32]}" if isp else nip2)
            except Exception: pass
            self._tz_match()
            # Real ISP/country strings can widen the card after startup.  Clamp
            # only after Tk has recomputed the requested size, never mid-drag.
            self.root.after_idle(self._clamp_to_work_area)
            if chg: self._flash()
        self.root.after(0,upd)


if __name__=="__main__":
    exe=sys.executable
    if exe.lower().endswith("python.exe"):
        pyw=exe.replace("python.exe","pythonw.exe")
        if not os.path.exists(pyw):
            pyw=os.path.join(os.path.dirname(exe),"pythonw.exe")
        if os.path.exists(pyw):
            import subprocess as _sp
            _sp.Popen([pyw,os.path.abspath(__file__)]+sys.argv[1:],
                      creationflags=_sp.CREATE_NO_WINDOW|_sp.DETACHED_PROCESS)
            sys.exit(0)
    IPBar()
