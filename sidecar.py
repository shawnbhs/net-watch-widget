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
         {"t": "ai_cred_sources", ...}  discovered logins, never their tokens
         {"t": "ai_windsurf_cached", ...}  cached quota plus its age
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
         {"cmd": "ai_login_cancel"}
         {"cmd": "ai_accounts_list"}                 the vault, no network
         {"cmd": "ai_providers"}                     the provider registry
         {"cmd": "ai_account_add", "provider": …, "label": …}
         {"cmd": "ai_account_login", "id": …}        re-auth a dead account
         {"cmd": "ai_account_remove", "id": …}
         {"cmd": "ai_account_rename", "id": …, "label": …}
         {"cmd": "ai_account_refresh", "id": …}      poll one account now
         {"cmd": "ai_cred_scan"}                     logins already on this PC
         {"cmd": "ai_cred_import", "provider": …, "path": …, "label": …}
         {"cmd": "ai_windsurf_cached"}               cached quota, no network
         {"cmd": "open_log"}
         {"cmd": "quit"}

The `ai` message gained an `accounts` array — one entry per registered
account, each with id, provider, label, status, usage, error, expires_at and
needs_login. The older `claude` and `codex` keys are still sent, filled from
the first healthy account of each provider, because the cards that read them
live in the React app and are replaced on their own schedule.

Percentages and colour thresholds are sent as raw numbers. Deciding that 92% is
red is a presentation choice and belongs in CSS, not here.
"""

import io
import inspect
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


def clock12(when=None, seconds=False):
    """Format a time of day as a short 12-hour clock: '1:30 pm', '12:04 am'.

    The hour is built arithmetically rather than with strftime's `%-I`
    (unpadded hour), because `%-I` is a glibc extension: on Windows, where
    this sidecar runs, it raises ValueError. `%I` works everywhere but pads
    to '01:30', which reads wrong for a clock, and `%p` is locale-dependent
    and upper-case. Doing it by hand gives the same answer on every platform.

    `when` is a struct_time or a datetime (core.tehran_now() returns one);
    None means now. The output is deliberately terse -- no padding, lower-case
    am/pm, no extra words -- because the widget measures these strings and
    resizes its window to fit them.
    """
    if when is None:
        when = time.localtime()
    tm = when.timetuple() if hasattr(when, "timetuple") else when
    suffix = "am" if tm.tm_hour < 12 else "pm"
    hour = tm.tm_hour % 12 or 12
    if seconds:
        return "%d:%02d:%02d %s" % (hour, tm.tm_min, tm.tm_sec, suffix)
    return "%d:%02d %s" % (hour, tm.tm_min, suffix)


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
         clock=clock12(seconds=True))

    try:
        emit("tehran", time=clock12(core.tehran_now()))
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
    # The vault is the source of truth when it exists, because it is the only
    # one that can hold two accounts for the same provider. With no vault, or
    # an empty one, this falls straight back to the two credential files the
    # widget has always read, so nothing about a fresh install changes.
    rows = []
    try:
        rows = core.ai_poll_accounts()
    except Exception as exc:
        emit("error", where="ai_accounts", err=str(exc))
        rows = []
    if rows:
        claude, codex = core.ai_legacy_pair(rows)
    else:
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
        accounts=rows,
        polled_at=clock12(),
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


# ── account management ────────────────────────────────────────────────────────
# Each of these answers on the same channel with a {"t": "ai_accounts"} or
# {"t": "ai_providers"} message. They are deliberately total: every failure
# path ends in an `err` string rather than an exception, because an exception
# escaping into the stdin loop would take the stream down and the whole widget
# goes blank when that happens, not just the card that asked.

def ai_accounts_emit(err=None, note=None, **extra):
    """Send the current vault contents, with no network traffic."""
    try:
        rows = core.ai_accounts_snapshot()
    except Exception as exc:
        rows, err = [], err or str(exc)
    emit("ai_accounts", accounts=rows, err=err or "", note=note or "", **extra)


def ai_providers_emit():
    """Send the provider registry so the picker names no vendor itself."""
    try:
        provs = core.ai_providers_list()
    except Exception as exc:
        emit("ai_providers", providers=[], err=str(exc))
        return
    emit("ai_providers", providers=provs, err="" if provs else "unavailable")


def _vault_or_err(where):
    """True when the vault is usable; otherwise says so and returns False."""
    if core.ai_vault_ready():
        return True
    emit("ai_accounts", accounts=[], err="account vault unavailable",
         note=where)
    return False


def ai_account_add(provider, label):
    """Add an account by signing in inside an isolated sandbox.

    The old path drove a login that wrote into the user's real shared
    credential file, which is precisely why a second account could never get
    in: the CLI found the first login already sitting there and reused it. The
    work now happens in `isolated_login`, and this is kept only as the name the
    command table already spells.
    """
    isolated_login(provider, label=label)


def ai_account_login(aid):
    """Re-authenticate an account whose refresh token finally died.

    Also isolated: re-signing in through the shared credential file would
    overwrite whichever account the CLI happens to hold, so fixing one account
    could quietly break another.
    """
    isolated_login("", account_id=aid)


def _vault_write_err(exc, fallback):
    """One error string for a vault write that raised, refusal or not.

    A refusal (the vault could not be read, so it was not overwritten) is
    named in words the user can act on; anything else falls back to the
    caller's verb. str(exc) is used only as a last resort and only for
    non-refusals, because these exceptions come from a layer whose messages
    are written for a traceback, not for a status line.
    """
    refused = core._vault_refusal(exc) if hasattr(core, "_vault_refusal") else None
    if refused:
        return refused
    return str(exc) or fallback


def ai_account_remove(aid):
    """Remove an account, and only say so once the vault says it is gone.

    remove_account() -> forget_account() returns a bool that is True only
    after the vault has been written AND read back and the tombstone has
    landed. Discarding it is not a cosmetic slip: on a failed write the UI is
    told "removed", the pane clears its pending row, and the account is still
    on disk and reappears at the next process start -- the user believes a
    deletion happened that did not. _vault_store_cred has always checked its
    bool; these two paths now agree that a vault write can fail.
    """
    if not _vault_or_err("remove"):
        return
    try:
        known = bool(core._vault.get_account(aid))
    except Exception:
        known = False
    if not known:
        ai_accounts_emit(err="no such account")
        return
    try:
        gone = bool(core._vault.remove_account(aid))
    except Exception as exc:
        ai_accounts_emit(err=_vault_write_err(exc, "remove failed"))
        return
    if not gone:
        # The row is still in the vault. Emitting the current roster with an
        # error rather than a note is what puts the account back in front of
        # the user instead of quietly leaving a stale pane behind.
        ai_accounts_emit(err="remove failed: the account is still stored")
        return
    ai_accounts_emit(note="removed")
    _ai_now.set()


def ai_account_rename(aid, label):
    """Rename an account, and only say so once the new label is on disk.

    Same contract as remove above: rename_account() reports whether save()
    proved the document landed, so a discarded False leaves the pane showing a
    label that reverts at the next process start.
    """
    if not _vault_or_err("rename"):
        return
    if not label:
        ai_accounts_emit(err="a label is required")
        return
    try:
        known = bool(core._vault.get_account(aid))
    except Exception:
        known = False
    if not known:
        ai_accounts_emit(err="no such account")
        return
    try:
        renamed = bool(core._vault.rename_account(aid, label))
    except Exception as exc:
        ai_accounts_emit(err=_vault_write_err(exc, "rename failed"))
        return
    if not renamed:
        ai_accounts_emit(err="rename failed: the stored label is unchanged")
        return
    ai_accounts_emit(note="renamed")


def ai_account_refresh(aid):
    """Poll exactly one account now, behind the same fail-closed geo gate.

    A single-account poll is still an outbound request to a provider that
    blocks this country, so it gets the identical verdict every other request
    gets. Refusing here is cheap; being seen from a blocked address is not.
    """
    if not _vault_or_err("refresh"):
        return
    try:
        acct = core._vault.get_account(aid)
    except Exception:
        acct = None
    if not acct:
        ai_accounts_emit(err="no such account")
        return
    if core.AI_LATCH["blocked"]:
        ai_accounts_emit(err=core.AI_LATCH["reason"] or "blocked")
        return
    ok, cc, why = core.geo_verdict(_state.get("ip"), _state.get("code"),
                                   force=True)
    if not ok:
        ai_accounts_emit(err="VPN required" if cc == "IR"
                         else "VPN required (%s)" % why)
        return
    try:
        row = core.ai_account_poll(acct)
    except Exception as exc:
        ai_accounts_emit(err=str(exc) or "poll failed")
        return
    emit("ai_accounts", accounts=[row], err=row.get("error") or "",
         note="single")


def ai_migrate():
    """First-run adoption of whatever the CLIs already hold.

    Runs once per process on a thread of its own: reading the credential files
    can mean waking a stopped WSL distro, which takes seconds the startup path
    cannot afford to spend.
    """
    try:
        n, note = core.ai_migrate_once()
    except Exception as exc:
        emit("error", where="ai_migrate", err=str(exc))
        return
    if n:
        emit("ai_accounts", accounts=core.ai_accounts_snapshot(),
             err="", note="%s %d" % (note, n))



# ── credential discovery ───────────────────────────────────────────────
# `aicredsources` locates vendor logins that are already sitting on this
# machine, so the add-account flow can offer the user a list to pick from
# instead of demanding they find and paste a token by hand. Everything it does
# is a read of a local file, which is why none of the commands below touch the
# geographic gate: the gate exists to stop outbound requests from a blocked
# country, and discovery makes none. The one function in that module that does
# spawn a process (`copilot_token_via_gh`) is opt-in and is never reached from
# here, because a background scan must not start subprocesses.

try:
    import aicredsources  # noqa: E402  (new module; may legitimately be absent)
except Exception:
    # Identical reasoning to `ailogin` above: a module that is missing or
    # half-written must cost the user one command, not the entire widget. A
    # sidecar that fails to import leaves Electron with a blank pane and no
    # explanation, so no new import is ever allowed to be load-bearing.
    aicredsources = None


# ── durable account identity ──────────────────────────────────────────────────
# A fingerprint is this file's answer to "is this the same account". For codex
# it always was one: `tokens.account_id` is a real id. For claude it was
# `subscriptionType + expiresAt`, and `expiresAt` is a token expiry rewritten
# on every single issue -- so signing in again as the SAME person produced a
# fingerprint matching nothing stored, and the account was filed as new. That
# is the reported bug, and it also meant the re-authentication guard below
# could never fire, because a positive match was unreachable.
#
# `aiproviders` now resolves a durable id for claude (`oauthAccount.accountUuid`,
# read from the CLI's own local state file -- a local read, no network) and
# publishes a matcher that answers to the durable form AND the legacy one, so
# rows written by older builds keep matching without rewriting the vault.
#
# None of this is load-bearing. Every helper here degrades to the single
# legacy string when the durable API is absent, which is exactly what this
# file did before, and a missing module costs one comparison rather than the
# whole widget.

try:
    import aiproviders  # noqa: E402  (durable identity; may be absent)
except Exception:
    aiproviders = None


# The stored form of a claude durable fingerprint. Public by construction:
# `is_durable_fingerprint` answers from this prefix alone, so a caller holding
# only the string can read it back.
_ACCT_FP_PREFIX = "claude:acct:"


def _ident_fn(name):
    """The durable-identity helper called `name`, or None.

    The vault is asked first and the provider registry second, deliberately.
    The vault owns what counts as the same account for storage, so if it has
    adopted the durable scheme its answer is the one every path in this file
    must agree with; falling through to `aiproviders` only covers the window
    where the registry has the capability and the vault has not adopted it
    yet. Bound by name at each call rather than once at import, because both
    modules are owned elsewhere and may gain the function after this one has
    already loaded.
    """
    for mod in (getattr(core, "_vault", None), aiproviders):
        fn = getattr(mod, name, None) if mod is not None else None
        if callable(fn):
            return fn
    return None


def _legacy_fp(provider, cred):
    """The vault's own single-string fingerprint, or "" when it has none.

    The vault owns the definition; taking it from anywhere else would let the
    guard below and the import path disagree about what counts as the same
    account.
    """
    try:
        fp = core._vault.cred_fingerprint(provider, cred)
    except Exception:
        return ""
    return str(fp) if fp else ""


def _fp_candidates(provider, cred, cred_path=None):
    """Every fingerprint form this credential answers to, best first.

    Matching a set rather than one string is the back-compatibility mechanism:
    a row stored before durable identity existed carries the legacy string, a
    row stored since carries the durable one, and comparing candidate sets
    matches either without migrating a single file.

    `cred_path` is the file this credential was read out of, and for claude it
    is how the durable id is found at all -- the CLI writes the account id
    beside the credential, not inside it. Pass it ONLY for a credential just
    read off disk. For a credential already held in the vault, pass nothing:
    a stored row's recorded path is usually the user's shared home, whose
    state file names whichever account the CLI is signed in as *now* rather
    than the account that row holds. Resolving a stored row through it would
    attribute row B to account A, and the guard below would then refuse A's
    own legitimate re-authentication -- the exact failure it must not cause.
    """
    fn = _ident_fn("cred_fingerprint_candidates")
    if fn is not None:
        try:
            out = fn(provider, cred, cred_path)
        except TypeError:
            # A vault that exposes the name with the older two-argument shape.
            try:
                out = fn(provider, cred)
            except Exception:
                out = None
        except Exception:
            out = None
        if out:
            return [str(f) for f in out if f]
    fp = _legacy_fp(provider, cred)
    return [fp] if fp else []


def _capture_identity(provider, cred, cred_path):
    """Staple the durable account id onto a credential just captured.

    This is the one moment the answer is knowable. The id lives beside the
    credential rather than inside it, so it can only be resolved while the
    home it was written into still belongs to this account: a login sandbox is
    destroyed within seconds of the sign-in, and a shared home starts
    answering for whichever account signs in next. Writing the id onto the
    credential makes the row that goes into the vault self-describing, so
    every later check -- the next duplicate verdict, the next re-auth guard --
    reads it straight off the object with no file access and no ambiguity
    about which home it came from.

    Only the opaque account id is copied. The same state file carries an email
    address, which is personal data this widget has no reason to store and
    which no caller needs, since equality is the only question ever asked.

    Returns a stapled copy when there is something to staple and the original
    otherwise. Never raises, never rewrites an id the credential already
    carries, and never touches token material.
    """
    if not isinstance(cred, dict):
        return cred
    fn = _ident_fn("cred_identity")
    if fn is None:
        return cred
    try:
        ident = fn(provider, cred, cred_path) or {}
    except TypeError:
        try:
            ident = fn(provider, cred) or {}
        except Exception:
            return cred
    except Exception:
        return cred
    if not isinstance(ident, dict) or ident.get("kind") != "account_uuid":
        # codex needs nothing stapled: its id is already inside the credential.
        # An email-only or legacy answer is deliberately not persisted -- the
        # first is personal data, the second is a token expiry that would be
        # stale the moment it was written.
        return cred
    fp = str(ident.get("fp") or "")
    if not fp.startswith(_ACCT_FP_PREFIX):
        return cred
    uid = fp[len(_ACCT_FP_PREFIX):].strip()
    if not uid:
        return cred
    if cred.get("accountUuid") or isinstance(cred.get("oauthAccount"), dict):
        return cred
    out = dict(cred)
    out["oauthAccount"] = {"accountUuid": uid}
    return out


def _discovery_or_err(where):
    """True when the discovery layer is importable; otherwise says so."""
    if aicredsources is not None:
        return True
    emit("ai_cred_sources", providers=[], found=[], missing=[],
         err="credential discovery unavailable (aicredsources not importable)",
         note=where)
    return False


def _safe_locations(locs):
    """Candidate locations with nothing but path-shaped metadata in them.

    `aicredsources` documents its `key` field as a key NAME and never a value,
    and the rest of the record is a path, a size and an existence flag, so
    this is a whitelist rather than a filter: a field the discovery layer adds
    later cannot smuggle a secret across the bridge unless it is added here
    too.
    """
    out = []
    for loc in (locs or []):
        if not isinstance(loc, dict):
            continue
        rec = {}
        for k in ("path", "kind", "exists", "confidence", "key", "note",
                  "size", "mtime"):
            if k in loc:
                rec[k] = loc[k]
        out.append(rec)
    return out


def _vault_fingerprints():
    """{fingerprint: account id} and {(provider, cred_path): id} for the vault.

    Both indexes come from the vault's own definition of identity, deliberately
    rather than from a second implementation written here: `core._import_sweep`
    already reuses that helper, and a third opinion about what counts as the
    same login would eventually disagree with the other two and either
    duplicate a row or hide one.

    A row is indexed under EVERY form it answers to, not just its best one, so
    one lookup matches a row stored before durable identity existed and a row
    stored since. Resolved from the stored object alone -- see
    `_fp_candidates` for why a stored row's own path is the wrong thing to
    consult.
    """
    by_fp, by_path = {}, {}
    try:
        accts = core._vault.list_accounts() or []
    except Exception:
        return by_fp, by_path
    for a in accts:
        if not isinstance(a, dict):
            continue
        aid = a.get("id")
        for fp in _fp_candidates(a.get("provider"), a.get("cred")):
            # setdefault, not assignment: if two rows really do answer to one
            # fingerprint the first wins, so the report is stable rather than
            # dependent on vault order.
            by_fp.setdefault(fp, aid)
        path = a.get("cred_path")
        if path:
            by_path[(a.get("provider"), path)] = aid
    return by_fp, by_path


def _in_vault(provider, source_path, by_fp, by_path):
    """(already_registered, account id, how it was matched).

    The fingerprint is the real answer, because it survives a token refresh
    and survives the same login being found under two different homes. It only
    exists for the two providers whose credential shapes the vault knows, so
    the path match is the fallback for the rest: it is weaker -- two logins
    could in principle share a file -- but it is still better than offering
    the user an "add" button for a row they already added.
    """
    cred = None
    if core.ai_vault_ready():
        try:
            cred, _why = aicredsources.read_cred(provider)
        except Exception:
            cred = None
    # Resolved against the file it was actually read from: for claude the
    # account id sits beside the credential, so without the path this degrades
    # to the legacy expiry string and a known account reads as unknown.
    cands = _fp_candidates(provider, cred, source_path) if cred is not None \
        else []
    # The credential object dies with this frame. It is read only to compute a
    # fingerprint and is never returned, never stored and never logged, so it
    # cannot reach a renderer log line or a crash report.
    del cred
    for fp in cands:
        if fp in by_fp:
            return True, by_fp[fp], "fingerprint"
    if source_path and (provider, source_path) in by_path:
        return True, by_path[(provider, source_path)], "path"
    return False, None, None


def ai_cred_scan():
    """Report every vendor login discoverable on this machine.

    Runs on a thread of its own because the scan opens files that may live on
    a WSL share, and reaching one can mean waking a stopped distro: seconds
    that the polling loop and the stdin reader cannot afford to spend blocked.
    """
    if not _discovery_or_err("scan"):
        return
    try:
        # include_creds=False is the whole safety argument for this command:
        # the payload is free of token material by construction rather than
        # because this function remembered to strip it afterwards.
        report = aicredsources.scan_all(include_creds=False)
    except Exception as exc:
        emit("ai_cred_sources", providers=[], found=[], missing=[],
             err=str(exc) or "scan failed", note="scan")
        return
    by_fp, by_path = _vault_fingerprints()
    rows = []
    for slug, rec in sorted((report.get("providers") or {}).items()):
        rec = rec if isinstance(rec, dict) else {}
        source = rec.get("source")
        found = bool(rec.get("found"))
        known, aid, how = (False, None, None)
        if found:
            known, aid, how = _in_vault(slug, source, by_fp, by_path)
        rows.append({
            "provider": slug,
            "found": found,
            "reason": rec.get("reason"),
            "source": source,
            "source_kind": rec.get("source_kind"),
            "locations": _safe_locations(rec.get("locations")),
            "in_vault": known,
            "account_id": aid,
            "matched_by": how,
        })
    emit("ai_cred_sources",
         providers=rows,
         found=sorted(r["provider"] for r in rows if r["found"]),
         missing=sorted(r["provider"] for r in rows if not r["found"]),
         generated_at=report.get("generated_at"),
         err="", note="scan")


def _import_label(provider, existing):
    """A unique default name for an imported row.

    The vault's own helper is used when it is exposed, so an imported account
    is named the same way whether it arrived through the first-run sweep or
    through this command; the local fallback exists only because that helper
    is private and could be renamed.
    """
    fn = getattr(core._vault, "_import_label", None)
    if callable(fn):
        try:
            return fn(provider, None, existing)
        except Exception:
            pass
    used = {a.get("label") for a in existing if isinstance(a, dict)}
    if provider not in used:
        return provider
    n = 2
    while "%s %d" % (provider, n) in used:
        n += 1
    return "%s %d" % (provider, n)


def ai_cred_import(provider, path=None, label=None):
    """Import ONE discovered login into the vault.

    The first-run sweep is all-or-nothing and runs once; this is the path for
    a user who wants a single account off this machine and not every login on
    it. It deliberately does not consult and does not stamp the first-run
    marker: that marker exists so a login the user deleted is never silently
    resurrected by an automatic pass, and an explicit click on one named row
    is not an automatic pass. Leaving the marker untouched also means this
    command can never suppress or trigger the bulk import as a side effect.
    """
    if not _discovery_or_err("import"):
        return
    provider = provider if isinstance(provider, str) else ""
    provider = provider.strip()
    if not provider:
        ai_accounts_emit(err="a provider is required", note="import")
        return
    try:
        slugs = list(aicredsources.provider_slugs() or [])
    except Exception:
        slugs = []
    if slugs and provider not in slugs:
        ai_accounts_emit(err="unknown provider: %s" % provider, note="import")
        return
    if not _vault_or_err("import"):
        return
    try:
        locs = aicredsources.cred_locations(provider) or []
    except Exception:
        locs = []
    known_paths = [l.get("path") for l in locs if isinstance(l, dict)]
    if path and path not in known_paths:
        # Refusing an unrecognised path is not pedantry: the caller is meant
        # to be echoing back a location this sidecar itself reported, and a
        # path from anywhere else is a bug in the caller or an attempt to make
        # the sidecar read an arbitrary file.
        ai_accounts_emit(err="unknown location for %s" % provider,
                         note="import")
        return
    try:
        cred, why = aicredsources.read_cred(provider)
    except Exception as exc:
        cred, why = None, "%s reader raised %s" % (provider, type(exc).__name__)
    if cred is None:
        ai_accounts_emit(err=why or "no credential found for %s" % provider,
                         note="import")
        return
    by_fp, by_path = _vault_fingerprints()
    source = path or next((l.get("path") for l in locs
                           if isinstance(l, dict) and l.get("exists")), None)
    # `source` is resolved before the comparison rather than after, because it
    # is an input to it now: the durable id for claude is read from beside the
    # credential file, so the path is what makes this check able to recognise
    # an account whose token has rotated since it was stored.
    cands = _fp_candidates(provider, cred, source)
    hit = next((by_fp[f] for f in cands if f in by_fp), None)
    if hit:
        ai_accounts_emit(err="that account is already in the vault",
                         note="import", account_id=hit)
        return
    if not cands and source and (provider, source) in by_path:
        # Without a fingerprint the identity claim is weaker, so the path is
        # the only duplicate signal available; refusing on it is the safer of
        # the two mistakes, because a duplicate row polls twice and shows the
        # same quota under two names.
        ai_accounts_emit(err="that login is already in the vault",
                         note="import", account_id=by_path[(provider, source)])
        return
    try:
        existing = core._vault.list_accounts() or []
    except Exception:
        existing = []
    name = (label or "").strip() or _import_label(provider, existing)
    try:
        acct = core._vault.add_account(provider, name, cred, source="import",
                                       cred_path=source)
    except Exception as exc:
        # A refusal is named from its type rather than its text: the message
        # aisecrets builds carries the vault path, and this string goes to
        # stdout. Everything else keeps the previous str(exc) wording.
        ai_accounts_emit(err=_vault_write_err(exc, "import failed"),
                         note="import")
        return
    finally:
        # Same rule as the scan: the credential was needed to write the vault
        # and for nothing else, so it is dropped before anything is emitted.
        del cred
    acct = acct if isinstance(acct, dict) else {}
    # Only the non-secret half of the new row is echoed back. `add_account`
    # returns the stored record, credential included, and forwarding it whole
    # would put a token in the renderer for the sake of a UI that only needs
    # the id to select the row it just created.
    safe = {k: acct.get(k) for k in ("id", "provider", "label", "added_at",
                                     "source", "cred_path")}
    ai_accounts_emit(note="imported", account=safe)
    _ai_now.set()


def ai_windsurf_cached():
    """The Windsurf quota the editor already cached, plus how old it is.

    Worth a command of its own because it is the only quota in the widget that
    costs no network request at all, which matters a great deal in a region
    where the live request may simply be impossible. The age travels with it
    because the number is only as fresh as the last time the Windsurf editor
    ran, and a three-week-old reading presented as current is a silent
    wrong-data bug -- worse than showing nothing.

    No freshness threshold is applied here. What counts as too old is a
    presentation decision in the same sense as a colour threshold, so the age
    and whatever staleness flag the discovery layer supplies are forwarded and
    the UI decides.
    """
    if aicredsources is None:
        emit("ai_windsurf_cached", ok=False,
             err="credential discovery unavailable "
                 "(aicredsources not importable)")
        return
    try:
        doc = aicredsources.windsurf_cached_plan()
    except Exception as exc:
        emit("ai_windsurf_cached", ok=False, err=str(exc) or "read failed")
        return
    if not isinstance(doc, dict):
        emit("ai_windsurf_cached", ok=False, err="malformed discovery result")
        return
    # Both spellings of the staleness fields are accepted because the module
    # documents them with a leading underscore in one place and without in
    # another; taking whichever is present avoids a silent None when the other
    # side settles on a name.
    def pick(*names):
        for n in names:
            if doc.get(n) is not None:
                return doc.get(n)
        return None
    emit("ai_windsurf_cached",
         ok=bool(doc.get("ok")),
         plan=doc.get("plan"),
         path=doc.get("path"),
         key=doc.get("key"),
         written_at=pick("written_at", "_source_mtime"),
         written_at_iso=doc.get("written_at_iso"),
         age_seconds=pick("age_seconds", "_age_seconds"),
         stale=pick("stale", "_stale"),
         cached=True,
         err=doc.get("reason") or "")


# ── isolated per-account login ────────────────────────────────────────────────
# A vendor CLI keeps exactly one login under the user's home directory and
# reuses it without asking, which is why signing in with a second email keeps
# handing back the first account. `ailogin` runs each sign-in inside a
# disposable sandbox directory that acts as a fake home, so the CLI has no
# previous login to find and is forced to prompt for real credentials.
#
# This file only orchestrates that: it claims the single login slot, applies
# the same fail-closed geographic gate every other outbound path uses, reports
# progress while a human is in a browser, and guarantees the sandbox is
# destroyed afterwards. That last point is not housekeeping -- until teardown
# runs, the sandbox holds a live credential in a plain file on disk.

try:
    import ailogin  # noqa: E402  (new module; may legitimately be absent)
except Exception:
    # A missing or half-written module must never be fatal. A sidecar that
    # fails to import blanks the whole widget, which is a far worse outcome
    # than one command reporting that it cannot run yet.
    ailogin = None


# The module is authored separately and its exact function names are not
# guaranteed, so each role is resolved by trying the plausible spellings in
# turn. Binding by role rather than by one hardcoded name means a reasonable
# naming choice on the other side does not silently disable the feature; an
# unreasonable one still degrades to a clear "isolated login not available"
# message rather than an exception.
_LOGIN_ROLES = (
    ("supported", ("isolated_login_supported", "supports", "supported",
                   "is_supported", "can_isolate", "provider_supported",
                   "is_available", "available")),
    ("sandbox", ("create_sandbox", "make_sandbox", "new_sandbox",
                 "prepare_sandbox", "open_sandbox", "sandbox")),
    ("launch", ("launch_login", "launch", "start_login", "spawn_login",
                "start", "spawn")),
    ("wait", ("wait_for_cred", "wait_for_credential", "wait",
              "await_credential", "collect", "harvest", "capture")),
    ("teardown", ("destroy_sandbox", "teardown", "destroy", "cleanup",
                  "remove_sandbox", "discard", "close")),
    ("duplicate", ("classify_account", "is_duplicate", "duplicate",
                   "detect_duplicate", "find_duplicate", "same_account",
                   "match_account", "classify", "is_new_account", "is_new")),
)

# How long a whole sign-in may take before it is abandoned, and how large a
# slice of that is spent inside one `wait` call. The wait is sliced rather than
# made in one blocking call so a cancel command is noticed within seconds
# instead of at the end of a five-minute timeout.
_LOGIN_WAIT = getattr(core, "AI_LOGIN_WAIT", 300)
_LOGIN_SLICE = 5.0

# The single login slot. Two concurrent sign-ins would race two sandboxes and
# put two console windows in front of a user who can only be in one of them,
# so the second is refused rather than queued.
_login_lock = threading.Lock()
_login = {"busy": False, "proc": None, "cancel": None, "what": ""}


def _login_api():
    """(callables by role, error string). Exactly one of the two is useful."""
    if ailogin is None:
        return None, "isolated login not available (ailogin module missing)"
    api, missing = {}, []
    for role, names in _LOGIN_ROLES:
        fn = None
        for name in names:
            cand = getattr(ailogin, name, None)
            if callable(cand):
                fn = cand
                api[role] = (name, cand)
                break
        if fn is None:
            missing.append(role)
    if missing:
        return None, ("isolated login not available (ailogin is missing: %s)"
                      % ", ".join(missing))
    return api, ""


def _call(api, role, *args, **kw):
    return api[role][1](*args, **kw)


def _takes(api, role, name):
    """Whether this role's function has a parameter of that name.

    The roles are bound by name across a module this file does not own, so the
    optional arguments -- a timeout, the process handle, the provider slug --
    are passed by keyword only when the bound function actually declares them.
    Passing them positionally instead would quietly land a timeout in a
    parameter that means something else entirely, and the failure would look
    like a login that never captured anything.
    """
    try:
        return name in inspect.signature(api[role][1]).parameters
    except (TypeError, ValueError, KeyError):
        return False


def _login_progress(stage, note, provider="", **extra):
    """One progress beat, on the envelope the accounts card already reads.

    A sign-in is minutes of a human in a browser. Without these the card sits
    on its last state and the widget looks frozen exactly when the user most
    needs to be told to go and look at the console window that just opened.
    Nothing secret is ever put on this channel -- it is the IPC stream and it
    reaches the log file on disk.
    """
    ai_accounts_emit(note=note, status="login", stage=stage,
                     provider=provider or "", **extra)


def _sandbox_dir(handle):
    """The sandbox's directory, whatever the handle calls it. For messages."""
    for attr in ("dir", "path", "home", "root", "sandbox_dir"):
        val = getattr(handle, attr, None)
        if isinstance(val, str) and val:
            return val
    if isinstance(handle, dict):
        for key in ("dir", "path", "home", "root", "sandbox_dir"):
            val = handle.get(key)
            if isinstance(val, str) and val:
                return val
    return ""


def _wait_result(raw):
    """Normalise the wait function's answer into (credential, reason).

    The contract says it returns either the captured credential or a reason
    string that distinguishes cancellation from timeout from CLI error, and
    there is more than one reasonable way to express that in Python. Accepting
    all of them here is cheaper than being wrong about one: the mapping is a
    dict, the reason is a string, a pair is taken in that order.
    """
    if raw is None:
        return None, "login not completed"
    if isinstance(raw, tuple) and len(raw) == 2:
        first, second = raw
        if isinstance(first, dict):
            return first, ("" if not second else str(second))
        if isinstance(second, dict):
            return second, ("" if not first else str(first))
        return None, str(second or first or "login not completed")
    if isinstance(raw, dict):
        # A wrapper carrying both is still unambiguous: a credential under a
        # named key, with a reason beside it.
        for key in ("cred", "credential", "creds"):
            if isinstance(raw.get(key), dict):
                return raw[key], str(raw.get("reason") or raw.get("err") or "")
        if raw.get("reason") or raw.get("err"):
            return None, str(raw.get("reason") or raw.get("err"))
        return raw, ""
    if isinstance(raw, str):
        return None, raw
    return None, "login not completed"


def _is_timeout(reason):
    """Whether a reason string means 'nothing happened yet', not 'it failed'.

    Only a timeout is worth waiting through: the wait is called in short
    slices, so its own timeout fires long before ours does and must not be
    mistaken for the user cancelling or the CLI erroring out, both of which
    are final.
    """
    low = (reason or "").lower()
    return ("timeout" in low or "timed out" in low or "pending" in low
            or "waiting" in low or low in ("", "none"))


def _dup_verdict(api, pid, cred, accounts):
    """(is duplicate, matching account id). Conservative when unsure.

    Reported as a duplicate only on a positive signal, because the cost of the
    two mistakes is not symmetric: a missed duplicate adds a redundant row the
    user can delete in one click, while a false duplicate refuses a genuinely
    new account and leaves him exactly where he started.
    """
    name = api["duplicate"][0]
    try:
        if _takes(api, "duplicate", "provider"):
            raw = _call(api, "duplicate", pid, cred, accounts)
        else:
            raw = _call(api, "duplicate", cred, accounts)
    except Exception:
        return False, ""
    inverted = name.startswith("is_new")
    if isinstance(raw, bool):
        return (not raw if inverted else raw), ""
    if raw is None:
        return False, ""
    if isinstance(raw, dict):
        # A three-way verdict is the informative shape: "unknown" means the
        # credential could not be compared at all, and refusing a login on
        # "unknown" would block the very account the user is trying to add.
        status = str(raw.get("status") or "").strip().lower()
        if status in ("duplicate", "same", "existing"):
            return True, str(raw.get("account_id") or raw.get("id") or "")
        if status in ("new", "fresh", "different", "unknown"):
            return False, ""
        dup = raw.get("duplicate")
        if dup is None:
            dup = raw.get("same")
        if dup is None and raw.get("new") is not None:
            dup = not raw.get("new")
        aid = raw.get("account_id") or raw.get("id") or ""
        if dup is None:
            dup = bool(aid)
        return bool(dup), str(aid or "")
    if isinstance(raw, tuple) and len(raw) == 2:
        dup, aid = raw
        if isinstance(dup, bool):
            return (not dup if inverted else dup), str(aid or "")
        return bool(aid), str(dup or aid or "")
    if isinstance(raw, str):
        # A bare string is read as a verdict word when it is one, and as the
        # id of the account that already holds this credential otherwise.
        low = raw.strip().lower()
        if low in ("new", "fresh", "different", ""):
            return False, ""
        if low in ("duplicate", "same", "existing"):
            return True, ""
        return True, raw.strip()
    return False, ""


def _account_label(accounts, aid):
    for acct in accounts or []:
        try:
            if acct.get("id") == aid:
                return acct.get("label") or acct.get("provider") or ""
        except Exception:
            continue
    return ""


# The one message that matters most in this project. The user has two accounts
# on two email addresses and every attempt at the second hands back the first,
# so being told plainly what just happened -- and what to do differently -- is
# the difference between understanding the problem and being stuck in it.
_DUPLICATE_MSG = (
    "This is the same account you are already signed in as%s. "
    "Sign out in your browser, or open a private/incognito window, "
    "before signing in with the other email address.")

# A refused re-authentication needs its own words. "Duplicate" is the wrong
# frame for it: nothing was being added, and the danger runs the other way --
# the write would have replaced this row's credential with a different
# account's, and the provider kills the old refresh token the moment it issues
# a new one, so the account being repaired would have been the one destroyed.
_WRONG_ACCOUNT_MSG = (
    "That sign-in came back as a different account%s, not this one, so "
    "nothing was changed. Sign out in your browser, or open a "
    "private/incognito window, then sign in with this account's own email "
    "address.")


def _login_refused(stage, message, provider=""):
    """A sign-in that finished but must not be written to the vault.

    Sent on the same `ai_accounts` envelope every other account message uses,
    so it arrives in the one reducer case the accounts card already has.
    Inventing a message type for a refusal would need its own consumer on the
    renderer side, and a channel with no consumer is exactly how this verdict
    used to be lost. `status` is deliberately not "login": the sign-in is over
    and the card must stop waiting rather than draw one more progress beat.
    """
    ai_accounts_emit(err=message, note=stage, status="refused", stage=stage,
                     provider=provider or "")


def _fp_owners(pid, cred, accounts, cred_path=None):
    """Ids of stored accounts this credential provably belongs to.

    Computed here instead of relying on the login module's verdict alone,
    because the guard below is protecting an irreversible overwrite and needs
    to know *which* row a match belongs to, not merely that some row matches.
    It is also the only comparison in the login path that sees durable
    identity: the login module fingerprints the old way, so for claude its
    verdict is always "new" no matter who signed in.

    The captured credential is resolved against the file it came out of, so
    its durable id is available; each stored row is resolved from its own
    object only, for the reason spelled out in `_fp_candidates`. Both sides
    answer to their legacy form as well, so a row written before this change
    still matches while its token has not rotated.
    """
    cands = set(_fp_candidates(pid, cred, cred_path))
    if not cands:
        return []
    owners = []
    for acct in accounts or []:
        try:
            if acct.get("provider") != pid:
                continue
            if cands.intersection(_fp_candidates(pid, acct.get("cred"))):
                aid = str(acct.get("id") or "")
                if aid and aid not in owners:
                    owners.append(aid)
        except Exception:
            continue
    return owners


def _reauth_conflict(dup, dup_id, pid, cred, accounts, account_id,
                     cred_path=None):
    """Id of the OTHER account this credential provably belongs to, or "".

    Only a positive match against a *different* row is evidence of a mix-up.
    A fingerprint that merely DIFFERS from the row being re-authenticated is
    the ordinary, expected result -- every sign-in mints a new token, and a
    legacy fingerprint is built from that token's expiry -- so refusing on a
    difference would block every legitimate re-authentication, which is a far
    worse failure than the one this guard exists to prevent. That direction is
    deliberate and must stay this way.

    What durable identity changes is not the direction but the reach. A
    positive match against another row used to be unreachable for claude, so
    this guard was inert for the provider it was written for; resolved through
    the account id it fires exactly when the browser handed back an account
    that is already stored under a different row.

    The same reasoning covers the degenerate "duplicate, but of what" answer:
    a bound duplicate function that returns a bare True names no row, and for
    a provider whose fingerprint is stable that True is usually the target row
    itself. Unattributable is therefore not treated as a conflict.
    """
    owners = _fp_owners(pid, cred, accounts, cred_path)
    if dup and dup_id and str(dup_id) not in owners:
        owners.append(str(dup_id))
    for oid in owners:
        if oid and oid != account_id:
            return oid
    return ""


def _kill_proc_tree(proc):
    """Stop the login console and anything it started. Never raises."""
    if proc is None:
        return
    pid = getattr(proc, "pid", None)
    if pid is None and isinstance(proc, int):
        pid = proc
    for meth in ("kill", "terminate"):
        fn = getattr(proc, meth, None)
        if callable(fn):
            try:
                fn()
                break
            except Exception:
                pass
    if not pid:
        return
    try:
        import psutil
        parent = psutil.Process(int(pid))
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
        try:
            parent.kill()
        except Exception:
            pass
    except Exception:
        # psutil absent, the process already gone, or a permission refusal:
        # the sandbox teardown below is what actually protects the credential,
        # and it runs regardless.
        pass


def _login_gate(where):
    """Why this sign-in must not be attempted, or '' when it may be.

    An authentication request is the single most visible thing a vendor sees
    from an address, so the login path gets the same fail-closed geographic
    verdict every other outbound path gets, checked here at the command
    boundary. `core` may well check again inside its own capture function; a
    redundant check costs one cached lookup and closes the window where a
    request escapes because the two files disagreed about whose job it was.
    """
    if not core.ai_vault_ready():
        return "account vault unavailable (%s)" % where
    try:
        if core.AI_LATCH["blocked"]:
            return core.AI_LATCH["reason"] or "blocked"
    except Exception:
        pass
    try:
        ok, cc, why = core.geo_verdict(_state.get("ip"), _state.get("code"),
                                       force=True)
    except Exception as exc:
        return "geo check failed (%s)" % type(exc).__name__
    if not ok:
        return "VPN required" if cc == "IR" else "VPN required (%s)" % why
    return ""


def _login_claim(what):
    """Take the single login slot, or say who already has it."""
    with _login_lock:
        if _login["busy"]:
            return None
        _login["busy"] = True
        _login["proc"] = None
        _login["what"] = what
        _login["cancel"] = threading.Event()
        return _login["cancel"]


def _login_release():
    with _login_lock:
        _login["busy"] = False
        _login["proc"] = None
        _login["cancel"] = None
        _login["what"] = ""


def isolated_login(provider, label="", account_id=None):
    """Sign into one account inside a disposable sandbox, then vault it.

    Runs on its own thread: the wait is a human doing a browser round-trip and
    pasting a code, which is minutes, and doing that on the stdin loop would
    stop every other command and the entire quota poll for the duration.

    Every exit from here goes through one `finally` that tears the sandbox
    down and frees the login slot, because the only alternative is a directory
    full of live credentials left on disk after a failure nobody saw.
    """
    what = "login" if account_id else "add"
    cancel = _login_claim(what)
    if cancel is None:
        ai_accounts_emit(err="a sign-in is already running; "
                             "finish or cancel it first")
        return

    handle = None
    api = None
    try:
        why = _login_gate(what)
        if why:
            ai_accounts_emit(err=why)
            return

        acct = None
        if account_id:
            try:
                acct = core._vault.get_account(account_id)
            except Exception as exc:
                emit("error", where="ai_account_login", err=str(exc))
                acct = None
            if not acct:
                ai_accounts_emit(err="no such account")
                return
            provider = acct.get("provider") or provider

        pid = (str(provider or "")).strip()
        # Checked before anything is spawned: everything past this point opens
        # a visible console and waits minutes for a human, and doing that for
        # a provider nothing can service is indistinguishable, from the user's
        # side, from a login that silently never finished.
        try:
            bad = core.ai_provider_login_error(pid)
        except Exception as exc:
            bad = str(exc) or "provider check failed"
        if bad:
            ai_accounts_emit(err=bad)
            return

        api, err = _login_api()
        if err:
            ai_accounts_emit(err=err)
            return
        try:
            can = bool(_call(api, "supported", pid))
        except Exception as exc:
            ai_accounts_emit(err="isolated login not available (%s)"
                                 % type(exc).__name__)
            return
        if not can:
            ai_accounts_emit(err="isolated login not available for %s" % pid)
            return

        try:
            handle = _call(api, "sandbox", pid)
        except Exception as exc:
            ai_accounts_emit(err="sandbox could not be created (%s)"
                                 % (str(exc) or type(exc).__name__))
            return
        if handle is None:
            ai_accounts_emit(err="sandbox could not be created")
            return
        _login_progress("sandbox", "isolated sign-in prepared", pid,
                        isolated=True)

        try:
            proc = _call(api, "launch", pid, handle)
        except Exception as exc:
            ai_accounts_emit(err="login could not be launched (%s)"
                                 % (str(exc) or type(exc).__name__))
            return
        with _login_lock:
            _login["proc"] = proc
        _login_progress("console", "a sign-in window is open: complete the "
                                   "sign-in there, then come back", pid,
                        isolated=True)

        cred, reason = None, "login not completed"
        deadline = time.time() + max(10, _LOGIN_WAIT)
        while time.time() < deadline and not cancel.is_set():
            slice_s = min(_LOGIN_SLICE, max(1.0, deadline - time.time()))
            kw = {}
            if _takes(api, "wait", "timeout"):
                kw["timeout"] = slice_s
            if _takes(api, "wait", "proc"):
                # Given the process, the wait can tell "the CLI exited without
                # writing anything" from "still waiting", which turns a silent
                # five-minute timeout into an immediate, accurate answer.
                kw["proc"] = proc
            try:
                raw = _call(api, "wait", handle, **kw) if kw \
                    else _call(api, "wait", handle, slice_s)
            except Exception as exc:
                cred, reason = None, ("login failed (%s)"
                                      % (str(exc) or type(exc).__name__))
                break
            cred, reason = _wait_result(raw)
            if cred or not _is_timeout(reason):
                break
            cred, reason = None, "login timed out"

        if cancel.is_set():
            ai_accounts_emit(err="sign-in cancelled")
            return
        if not cred:
            ai_accounts_emit(err=reason or "login not completed")
            return

        try:
            known = core._vault.list_accounts() or []
        except Exception:
            known = []

        # The sandbox is a private home, so the CLI state file inside it names
        # the account that just signed in -- and teardown deletes it seconds
        # from now. Resolving identity here, against that path, is the only
        # chance to get a correct answer; stapling it onto the credential
        # carries the answer into the vault row so no later check has to go
        # looking for a home that no longer exists.
        cred_path = getattr(handle, "cred_path", None)
        if not cred_path and isinstance(handle, dict):
            cred_path = handle.get("cred_path")
        cred = _capture_identity(pid, cred, cred_path)

        dup, dup_id = _dup_verdict(api, pid, cred, known)
        if account_id:
            # The destructive case, and the reason this branch exists at all.
            # `update_cred` replaces the targeted row's credential wholesale,
            # and the provider invalidates the old refresh token as soon as it
            # issues a new one. If the browser signed the user in as somebody
            # else, writing here would end that account's access with nothing
            # left to recover it from. Checked before the write, never after.
            other = _reauth_conflict(dup, dup_id, pid, cred, known, account_id,
                                     cred_path)
            if other:
                name = _account_label(known, other)
                _login_refused("account-mismatch", _WRONG_ACCOUNT_MSG
                               % (" (%s)" % name if name else ""), pid)
                return
        else:
            # `owners` is consulted alongside the login module's verdict, not
            # instead of it. That module fingerprints the credential the old
            # way, so for claude its answer is always "new" -- a fresh sign-in
            # as the same person mints a new token expiry, and the old scheme
            # read that as a different account. This is the path the reported
            # bug walked down every single time: the browser was still signed
            # in as the first account, the sign-in completed as that account
            # without asking, and a second row was written for it.
            owners = _fp_owners(pid, cred, known, cred_path)
            if dup or owners:
                # Adding a second identical row would hide the problem instead
                # of naming it, and the user would be left with two cards
                # showing one account's numbers twice.
                name = _account_label(known,
                                      dup_id or (owners[0] if owners else ""))
                _login_refused("duplicate", _DUPLICATE_MSG
                               % (" (%s)" % name if name else ""), pid)
                return

        try:
            if account_id:
                # update_cred returns True only once the document has been
                # read back off the disk. A discarded False here would tell
                # the user the login they just completed was kept when the
                # only live copy is the one about to go out of scope -- the
                # provider has already killed the token in the vault.
                if not core._vault.update_cred(account_id, cred):
                    ai_accounts_emit(err=core.AI_UNSAVED_DEFAULT)
                    return
                note = "account re-authenticated"
            else:
                shown = label or _default_login_label(pid)
                core._vault.add_account(pid, shown, cred, source="login")
                note = "account added"
        except Exception as exc:
            # A refusal is named, because the vault was left intact on purpose
            # and only the user can clear that state; its reason string is
            # built from the exception TYPE and contains nothing from the
            # vault or the payload. Anything else keeps the old type-name-only
            # wording, since a generic vault error can carry the path or the
            # credential in its message and this string goes to stdout.
            ai_accounts_emit(err=(core._vault_refusal(exc)
                                  or "vault write failed (%s)"
                                  % type(exc).__name__))
            return
        ai_accounts_emit(note=note)
        _ai_now.set()
    except Exception as exc:  # pragma: no cover - defensive
        ai_accounts_emit(err=str(exc) or "login failed")
    finally:
        # The sandbox holds a real credential until it is gone, so teardown is
        # unconditional: success, failure, cancellation, timeout and the
        # exception nobody predicted all arrive here.
        if handle is not None and api is not None:
            try:
                _call(api, "teardown", handle)
            except Exception as exc:
                emit("error", where="ai_login_teardown",
                     err=type(exc).__name__)
        _login_release()


def _default_login_label(pid):
    """A neutral label when the user supplied none. Never from the token.

    An email address out of a credential is personal data this widget has no
    reason to store, and the user renames an account in one click anyway.
    """
    try:
        return core._default_label(pid)
    except Exception:
        pass
    try:
        n = 1 + sum(1 for a in (core._vault.list_accounts() or [])
                    if a.get("provider") == pid)
    except Exception:
        n = 1
    return "%s %d" % (pid or "account", n)


def ai_login_cancel():
    """Abandon the sign-in in flight: kill its console, drop its sandbox.

    An interactive login waits on a human who may simply have walked away.
    Without this the only way out is killing the widget, which leaves the
    sandbox -- and the credential inside it -- behind on disk.
    """
    with _login_lock:
        if not _login["busy"]:
            ai_accounts_emit(err="no sign-in is running")
            return
        ev = _login["cancel"]
        proc = _login["proc"]
    if ev is not None:
        ev.set()
    _kill_proc_tree(proc)
    _login_progress("cancelling", "cancelling the sign-in")


# ── commands ──────────────────────────────────────────────────────────────────

def handle(cmd):
    # A JSON line that parses but is not an object -- a bare list, a number, a
    # string -- must not reach .get(). It would raise, and the stdin loop would
    # answer a structural mistake with a stack-trace-shaped error instead of
    # naming what was wrong with the message.
    if not isinstance(cmd, dict):
        emit("error", where="stdin", err="command must be a JSON object")
        return
    name = cmd.get("cmd", "")
    if not isinstance(name, str):
        emit("error", where="stdin", err="cmd must be a string")
        return
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
        # The card's "login expired" click used to call the CLI launcher
        # directly, which was both ungated and aimed at the shared credential
        # file. It now takes the same isolated, geo-gated path as every other
        # sign-in, on its own thread so the click cannot stall the stream.
        threading.Thread(target=isolated_login, daemon=True,
                         args=(str(cmd.get("which") or "claude"),)).start()
    elif name == "ai_accounts_list":
        ai_accounts_emit()
    elif name == "ai_providers":
        ai_providers_emit()
    elif name == "ai_account_add":
        threading.Thread(target=ai_account_add, daemon=True,
                         args=(str(cmd.get("provider") or ""),
                               str(cmd.get("label") or "").strip())).start()
    elif name == "ai_account_login":
        threading.Thread(target=ai_account_login, daemon=True,
                         args=(cmd.get("id"),)).start()
    elif name == "ai_login_cancel":
        # Deliberately inline: cancelling only sets an event and kills a
        # process, so it must not be queued behind anything, least of all the
        # sign-in it is trying to stop.
        ai_login_cancel()
    elif name == "ai_account_remove":
        ai_account_remove(cmd.get("id"))
    elif name == "ai_account_rename":
        ai_account_rename(cmd.get("id"), str(cmd.get("label") or "").strip())
    elif name == "ai_account_refresh":
        threading.Thread(target=ai_account_refresh, daemon=True,
                         args=(cmd.get("id"),)).start()
    elif name == "ai_cred_scan":
        # Threaded because the scan reads files that can live on a WSL share,
        # and reaching one may wake a stopped distro; the stdin reader must
        # stay responsive while that happens.
        threading.Thread(target=ai_cred_scan, daemon=True).start()
    elif name == "ai_cred_import":
        prov = cmd.get("provider")
        loc = cmd.get("path")
        lab = cmd.get("label")
        threading.Thread(
            target=ai_cred_import, daemon=True,
            args=(prov if isinstance(prov, str) else "",
                  loc if isinstance(loc, str) else None,
                  lab if isinstance(lab, str) else None)).start()
    elif name == "ai_windsurf_cached":
        threading.Thread(target=ai_windsurf_cached, daemon=True).start()
    elif name == "open_log":
        core.ourl(core.LOG_FILE)
    elif name == "quit":
        _STOP.set()
    else:
        # Silence here used to mean a UI that had sent a command this build
        # does not implement sat waiting forever for a reply that was never
        # going to come. Saying so costs one line and makes the mismatch
        # visible the first time it happens.
        emit("error", where="stdin",
             err="unknown command: %s" % (name or "(missing)"))


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
    for target in (net_loop, hw_loop, ai_loop, tz_once, net_adopt, ai_migrate):
        threading.Thread(target=target, daemon=True).start()
    threading.Thread(target=stdin_loop, daemon=True).start()
    emit("ready")
    while not _STOP.is_set():
        _STOP.wait(0.5)


if __name__ == "__main__":
    main()
