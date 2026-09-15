"""Data sidecar: everything ip_bar.py knows, as JSON lines on stdout.

The widget's UI is React, but none of the knowledge it displays is UI-shaped --
the geo cross-check, the adapter toggle, the AI token handling and the
sixteen-hundred lines of country tables are the actual product. This does not
reimplement any of it: it imports `core` and runs those functions on a schedule,
writing what they return to stdout.

`core.py` is the data half of what used to be ip_bar.py, with the tkinter UI
removed. Nothing it exposes draws anything.

Protocol, one JSON object per line, UTF-8:

    out  {"t": "net",    ...}   public/local IP, ping, VPN verdict
         {"t": "hw",     ...}   cpu / ram / gpu
         {"t": "ai",     ...}   Claude and Codex usage, forwarded raw
         {"t": "checks", ...}   ASN, proxy/datacenter, DNS leak, score
                                (on an IP change, or on demand)
         {"t": "tz",     ...}   system timezone vs the exit IP's
         {"t": "netstate",...}  adapters up or cut, and whether a toggle is live
         {"t": "hist",   ...}   recent public-IP changes
         {"t": "ready"}         first full cycle done

    in   {"cmd": "refresh"}          force a network cycle now
         {"cmd": "checks"}           run the quick checks now
         {"cmd": "ai_refresh"}       poll AI usage now, subject to its floor
         {"cmd": "net_toggle"}       cut or restore the adapters (raises UAC)
         {"cmd": "copy", "text": …}  to the clipboard
         {"cmd": "open", "url": …}   in the default browser
         {"cmd": "ai_login", "which": "claude"|"codex"}
         {"cmd": "open_log"}
         {"cmd": "quit"}

Percentages and colour thresholds are sent as raw numbers. Deciding that 92% is
red is a presentation choice and belongs in CSS, not here.
"""

import io
import json
import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chrome  # noqa: E402  (path must be set before these imports)
import core  # noqa: E402

# The country tables are full of flag emoji and box-drawing characters, and the
# default console encoding on a Windows machine is cp1252, which cannot encode
# them. Without this the first flag raises UnicodeEncodeError and kills the
# stream. Electron reads utf-8 on its side.
_OUT = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                        line_buffering=True)

_LOCK = threading.Lock()
_STOP = threading.Event()


def emit(kind, **fields):
    """Write one message. Never lets a serialisation failure kill the thread."""
    try:
        line = json.dumps(dict(fields, t=kind), ensure_ascii=False, default=str)
    except Exception as exc:  # pragma: no cover - defensive
        line = json.dumps({"t": "error", "where": kind, "err": str(exc)})
    with _LOCK:
        try:
            _OUT.write(line + "\n")
        except Exception:
            # stdout closed: Electron is gone, so there is nobody to tell.
            _STOP.set()


# ── state shared between loops ────────────────────────────────────────────────

_state = {
    "ip": None,         # last good public IP, for change detection
    "ip2": None,
    "checks_ip": None,  # the address the quick checks last ran against
    "code": "?",
    "hist": [],
    "net_up": True,
    "net_busy": False,
    "tz": "",
    "p1": "?",
    "loss1": 0,
}

_refresh_now = threading.Event()


# ── network ───────────────────────────────────────────────────────────────────

def refresh_once():
    """One public-IP / ping / route cycle, mirroring IPBar._do_refresh."""
    res = {}

    def _i():  res["ip"] = core.fetch_ip()
    def _i2(): res["ip2"] = core.fetch_rf()
    def _p1(): res["p1"] = core.ping(core.PING_HOST_1)
    def _p2(): res["p2"] = core.ping(core.PING_HOST_2)
    def _lo(): res["lo"] = core.local_ip()
    def _gw(): res["gw"] = core.gateway()

    ts = [threading.Thread(target=fn, daemon=True)
          for fn in (_i, _i2, _p1, _p2, _lo, _gw)]
    for t in ts: t.start()
    for t in ts: t.join()

    info = res.get("ip", {}) or {}
    nip = info.get("ip", "?")
    nip2 = res.get("ip2", "?")
    cc = info.get("cc", "?")
    isp = info.get("isp", "") or ""
    p1, l1 = res.get("p1", ("?", 100))
    p2, l2 = res.get("p2", ("?", 100))
    lo = res.get("lo", "?")
    gw = res.get("gw", "?")

    f1, n1, c1 = core.clabel(cc, nip)
    f2, n2, c2 = core.clabel(cc, nip2)

    # An IP change is worth a log line and a toast exactly once, and only when
    # we had a previous reading to compare against -- the first cycle after
    # launch is not a change.
    changed = (_state["ip"] is not None and nip != _state["ip"]
               and nip not in ("?", "Error"))
    if changed:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        _state["hist"].append((stamp, nip))
        if len(_state["hist"]) > core.HIST_MAX:
            _state["hist"].pop(0)
        core.log(_state["ip"], nip)
        core.toast("IP Changed", "%s -> %s" % (_state["ip"], nip))
        emit("hist", items=[{"at": a, "ip": i} for a, i in _state["hist"]])

    if nip not in ("?", "Error"):
        _state["ip"] = nip
        # Run the quick checks when, and only when, their answer could have
        # changed: the first time an address is known, and on every change after
        # that. Tying them to the three-second network cycle was abusive, but
        # leaving them blank until the user presses Run means a panel that is
        # empty exactly when it would be most useful -- the moment the widget
        # starts, or the moment the VPN moves you somewhere new.
        if nip != _state["checks_ip"]:
            _state["checks_ip"] = nip
            threading.Thread(target=checks_once, args=(nip,), daemon=True).start()
    if nip2 not in ("?", "Error"):
        _state["ip2"] = nip2
    _state["code"] = c1
    _state["p1"], _state["loss1"] = p1, l1

    # Two independent lookups disagreeing is the VPN signal: a tunnel that
    # rewrites one path and not the other. Agreement is not proof of no VPN,
    # which is why the geo cross-check in `checks` exists as well.
    vpn = (nip not in ("?", "Error") and nip2 not in ("?", "Error")
           and nip != nip2)

    emit("net",
         ip=nip, ip2=nip2, local=lo, gw=gw,
         flag=f1, country=n1, code=c1,
         flag2=f2, country2=n2, code2=c2,
         isp=isp, vpn=vpn, changed=changed,
         ping1=p1, ping2=p2, loss1=l1, loss2=l2,
         ping1_ms=core.pms(p1), ping2_ms=core.pms(p2),
         clock=time.strftime("%H:%M:%S"))

    try:
        emit("tehran", time=core.tehran_now().strftime("%H:%M"))
    except Exception:
        pass

    return nip


def net_loop():
    while not _STOP.is_set():
        try:
            refresh_once()
        except Exception as exc:
            emit("error", where="net", err=str(exc))
        # Wake early on a manual refresh rather than sleeping the full cycle.
        _refresh_now.wait(core.REFRESH)
        _refresh_now.clear()


def checks_once(ip):
    """The quick-checks panel: ASN, proxy/datacenter, DNS leak, score.

    Run on an IP change, and on demand -- never on the network cycle. Two of the
    three lookups here are third-party enrichment endpoints on a free tier, and
    the network cycle runs every three seconds, so wiring this to it would mean
    twelve hundred requests an hour to services that are doing us a favour. An
    address change is the only event that can actually change the answer, and it
    is rare, so that is what triggers it.
    """
    try:
        emit("checks", loading=True)
        res = core.fetch_vpn(ip)
        hostname, city = core.fetch_host(ip)
        if res is None:
            emit("checks", failed=True)
            return
        cc = res.get("countryCode", "?")
        servers = core.dns_servers()
        leak, info = core.dns_leak(cc, servers)
        score = core.net_score(core.pms(_state["p1"]), _state["loss1"], leak)
        emit("checks",
             asn=res.get("as", "") or "",
             isp=res.get("isp", "") or res.get("org", "") or "",
             proxy=bool(res.get("proxy")),
             hosting=bool(res.get("hosting")),
             host=hostname or city or "",
             dns=info or [],
             dns_leak=bool(leak),
             score=score)
    except Exception as exc:
        emit("error", where="checks", err=str(exc))


# ── hardware ──────────────────────────────────────────────────────────────────

def hw_loop():
    hist = {"cpu": [], "ram": [], "gpu": []}
    while not _STOP.is_set():
        try:
            cpu, ram, gpu = core.hw()
            try:
                gpu_val = float(str(gpu).replace("%", "").strip())
            except Exception:
                gpu_val = None
            for key, val in (("cpu", cpu), ("ram", ram), ("gpu", gpu_val or 0)):
                hist[key].append(val)
                if len(hist[key]) > 40:
                    hist[key].pop(0)
            emit("hw", cpu=cpu, ram=ram, gpu=gpu_val, gpu_raw=gpu,
                 hist={k: list(v) for k, v in hist.items()})
        except Exception as exc:
            emit("error", where="hw", err=str(exc))
        _STOP.wait(2)


# ── timezone ──────────────────────────────────────────────────────────────────

def tz_once():
    try:
        tz = core.get_tz()
        _state["tz"] = tz
        emit("tz", tz=tz, suspect=tz in core.SUSPECT,
             tz_code=core.TZ_CC.get(tz, ""))
    except Exception as exc:
        emit("error", where="tz", err=str(exc))


# ── AI usage ──────────────────────────────────────────────────────────────────

AI_CACHE = core._expand(core.env_str(
    "AI_CACHE_FILE", os.path.join(os.path.expanduser("~"), ".ipbar_ai_cache.json")))


def ai_cache_save(payload):
    """Remember the last reading that actually succeeded."""
    try:
        doc = core._read_json(AI_CACHE) or {}
        doc["at"] = time.time()
        doc["ai"] = payload
        core._write_json_atomic(AI_CACHE, doc)
    except Exception:
        pass


def ai_cache_mark(retry=0):
    """Record that a poll was *attempted*, and how long we were told to wait.

    Kept apart from the reading itself because a poll that fails is still a
    poll: it is the request the rate limiter counted, and it is the one fact
    the next launch needs. Remembering only the last good reading means every
    restart begins a brand new schedule -- which is how a fifteen-minute poll
    becomes one per launch, and how an afternoon of restarts turns a one-hour
    lockout into a permanent one, each launch spending its first request
    re-earning the backoff the previous launch threw away.
    """
    try:
        doc = core._read_json(AI_CACHE) or {}
        now = time.time()
        doc["tried"] = now
        if retry:
            doc["retry_until"] = now + retry
        else:
            doc.pop("retry_until", None)
        core._write_json_atomic(AI_CACHE, doc)
    except Exception:
        pass


def ai_cache_hold():
    """Seconds left of a rate-limit backoff, or 0."""
    try:
        doc = core._read_json(AI_CACHE) or {}
    except Exception:
        return 0.0
    return max(0.0, float(doc.get("retry_until") or 0) - time.time())


def ai_cache_due():
    """(seconds to wait before the first poll, when the last one was).

    A restart is not a reason to ask again. The widget is restarted for
    reasons that have nothing to do with usage -- an update, a reboot, a crash,
    an afternoon of development -- and the endpoints charge for the privilege.
    """
    try:
        doc = core._read_json(AI_CACHE) or {}
    except Exception:
        return 0.0, 0.0
    now = time.time()
    last = float(doc.get("tried") or doc.get("at") or 0)
    wait = max(0.0, core.AI_POLL_BASE - (now - last)) if last else 0.0
    hold = float(doc.get("retry_until") or 0)
    return (max(wait, hold - now) if hold > now else wait), last


def ai_cache_emit():
    """Show the last good reading immediately, marked stale.

    The usage endpoints are rate limited, back off for an hour at a time, and
    are only polled every fifteen minutes at best. Without this the panel is
    five empty rows for the whole of that -- and empty reads as "no data
    exists", when what is true is "this is what it was, and we cannot ask again
    yet". A percentage from twenty minutes ago is worth a great deal more than
    a dash, provided it is labelled as old.
    """
    try:
        doc = core._read_json(AI_CACHE) or {}
        payload = doc.get("ai")
        if not payload:
            return
        age = max(0, int(time.time() - doc.get("at", 0)))
        emit("ai", stale=True, age=age, **payload)
    except Exception:
        pass


def ai_loop():
    fails = 0
    # Pick the schedule back up rather than starting a new one. Without this
    # the first act of every launch is a request, and the backoff a previous
    # launch was told to observe is lost with it.
    first, last = ai_cache_due()
    manual = bool(first > 0 and _ai_now.wait(first))
    if manual:
        _ai_now.clear()
    while not _STOP.is_set():
        wait = 3600
        try:
            wait, fails, last = ai_tick(fails, last, manual)
        except Exception as exc:
            emit("error", where="ai", err=str(exc))
            wait = core.AI_FAIL_BACKOFF
        manual = _ai_now.wait(max(5, wait))
        if manual:
            _ai_now.clear()


_ai_now = threading.Event()


def ai_tick(fails, last, manual):
    """One usage poll. Returns (seconds to wait, fails, last-poll time).

    The backoff and the region latch are the reason this is not a plain timer:
    a provider that blocks a region will keep blocking it, and retrying is how
    a status widget turns into something that looks like abuse.
    """
    if not core.AI_ENABLED:
        return 3600, fails, last

    now = time.time()
    if manual and now - last < core.AI_POLL_MIN:
        emit("ai", status="wait", seconds=int(core.AI_POLL_MIN - (now - last)))
        return core.AI_POLL_BASE, fails, last

    # A backoff is not advice that can be overruled by pressing the button
    # harder. A request made during one comes back 429 and renews the hour, so
    # forcing a refresh through a rate limit makes it strictly worse than doing
    # nothing. Say how much is left instead.
    hold = ai_cache_hold()
    if hold > 0:
        emit("ai", status="wait", seconds=int(hold), reason="rate limited")
        return hold, fails, last

    if core.AI_LATCH["blocked"]:
        emit("ai", off=True, reason=core.AI_LATCH["reason"])
        return 3600, fails, last

    ok, cc, why = core.geo_verdict(_state.get("ip"), _state.get("code"),
                                   force=manual)
    if not ok:
        emit("ai", off=True,
             reason="VPN required" if cc == "IR" else "VPN required (%s)" % why)
        return (90 if cc == "IR" else 120), fails, last

    last = now
    emit("ai", status="polling")
    claude = core.fetch_claude_usage()
    time.sleep(random.uniform(1.5, 5.0))  # don't fire both at one instant
    codex = core.fetch_gpt_usage()

    # If the tunnel dropped between the two calls the readings are from the
    # wrong exit, so they are discarded rather than displayed.
    ok2, _, _ = core.geo_verdict(force=True)
    if not ok2:
        # The readings are discarded, but the requests were still made and
        # still counted against us, so the attempt is recorded either way.
        ai_cache_mark()
        emit("ai", off=True, reason="VPN required")
        return 90, fails, last

    payload = dict(
        claude=_ai_payload(claude, ("session_pct", "week_pct", "model_pct"),
                           ("session_reset", "week_reset", "model_reset"),
                           model_name=claude.get("model_name") or ""),
        codex=_ai_payload(codex, ("sess_pct", "week_pct"),
                          ("sess_reset_ts", "week_reset_ts"),
                          limit_reached=bool(codex.get("limit_reached"))),
        polled_at=time.strftime("%H:%M"),
        retry=max(claude.get("retry", 0), codex.get("retry", 0)))
    emit("ai", stale=False, age=0, **payload)
    # Only a reading with something in it is worth remembering: caching a pair
    # of errors would replace a good answer from an hour ago with nothing.
    if not claude.get("err") or not codex.get("err"):
        ai_cache_save(payload)

    if (claude.get("err") in core.NOT_CONFIGURED
            and codex.get("err") in core.NOT_CONFIGURED):  # noqa: E129
        return 900, 0, last

    retry = max(claude.get("retry", 0), codex.get("retry", 0))
    ai_cache_mark(retry)
    if retry:
        return retry, fails + 1, last
    if claude.get("err") or codex.get("err"):
        return min(core.AI_FAIL_BACKOFF * (fails + 1), 4 * 3600), fails + 1, last

    jitter = random.uniform(-core.AI_POLL_JITTER, core.AI_POLL_JITTER)
    return core.AI_POLL_BASE * (1 + jitter), 0, last


def _ai_payload(raw, pct_keys, reset_keys, **extra):
    """Flatten one provider's reply into percentages plus countdown strings.

    `_until` is here rather than in the frontend because it encodes the same
    "a login that was never set up is not an error" distinction the README
    documents, and splitting that across two languages would let the two drift.

    `err` is forwarded verbatim, LOGIN_EXPIRED marker and all: whether that
    becomes a clickable prompt is the frontend's decision, and the marker is
    how it tells a dead login from any other failure.
    """
    err = raw.get("err")
    out = {"err": err or "",
           "setup": err in core.NOT_CONFIGURED,
           "plan": raw.get("plan") or ""}
    out.update(extra)
    for pct_key, reset_key in zip(pct_keys, reset_keys):
        out[pct_key] = raw.get(pct_key)
        out[reset_key] = core._until(raw.get(reset_key)) or ""
    return out


# ── adapters ──────────────────────────────────────────────────────────────────

def net_state_push():
    emit("netstate", up=_state["net_up"], busy=_state["net_busy"])


def net_adopt():
    try:
        _state["net_up"] = not core.net_is_down()
    except Exception:
        _state["net_up"] = True
    net_state_push()


def net_toggle():
    """Cut or restore every physical adapter. One UAC prompt per toggle."""
    if _state["net_busy"]:
        return
    _state["net_busy"] = True
    net_state_push()
    try:
        ok = core.net_restore() if not _state["net_up"] else core.net_cut()
        if ok:
            _state["net_up"] = not _state["net_up"]
    except Exception as exc:
        emit("error", where="net_toggle", err=str(exc))
    finally:
        _state["net_busy"] = False
        net_state_push()
        _refresh_now.set()


# ── pane appearance ───────────────────────────────────────────────────────────

def style_pane(hwnd):
    """Round one backing window's corners, on request from the Electron shell.

    DWM will round a window and clip its acrylic backdrop to the same shape, but
    only through DwmSetWindowAttribute -- and Electron exposes no binding for
    it. SetWindowRgn, the obvious alternative, was measured earlier in this
    project and does not clip an acrylic backdrop at all. `chrome.rounded()` is
    already that exact call and already used by the Tk build, so the shell hands
    over the window handle rather than the project growing a native Node module
    to repeat it.

    The radius is Windows', with no parameter for it: about 8px. That is why the
    cards are drawn at 8px rather than the design's 20px -- the frost cannot be
    made rounder, so the card is made to match it.
    """
    try:
        h = int(hwnd)
    except (TypeError, ValueError):
        return
    if not h:
        return
    shim = type("H", (), {"frame": lambda self, h=h: h})()
    if not chrome.rounded(shim):
        emit("error", where="style_pane", err="rounded refused")


# ── commands ──────────────────────────────────────────────────────────────────

def handle(cmd):
    name = cmd.get("cmd", "")
    if name == "refresh":
        _refresh_now.set()
        _ai_now.set()
    elif name == "checks":
        ip = _state.get("ip")
        if ip and ip not in ("?", "Error"):
            threading.Thread(target=checks_once, args=(ip,), daemon=True).start()
        else:
            emit("checks", failed=True)
    elif name == "ai_refresh":
        _ai_now.set()
    elif name == "style_pane":
        style_pane(cmd.get("hwnd"))
    elif name == "net_toggle":
        threading.Thread(target=net_toggle, daemon=True).start()
    elif name == "copy":
        core.clip(cmd.get("text", ""))
    elif name == "open":
        core.ourl(cmd.get("url", ""))
    elif name == "ai_login":
        core.ai_login_launch(cmd.get("which", "claude"))
    elif name == "open_log":
        core.ourl(core.LOG_FILE)
    elif name == "quit":
        _STOP.set()


def stdin_loop():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as exc:
            emit("error", where="stdin", err=str(exc))
        if _STOP.is_set():
            break
    _STOP.set()


def main():
    emit("hello", pid=os.getpid(), refresh=core.REFRESH)
    ai_cache_emit()
    for target in (net_loop, hw_loop, ai_loop, tz_once, net_adopt):
        threading.Thread(target=target, daemon=True).start()
    threading.Thread(target=stdin_loop, daemon=True).start()
    emit("ready")
    while not _STOP.is_set():
        _STOP.wait(0.5)


if __name__ == "__main__":
    main()
