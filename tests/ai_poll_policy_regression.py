#!/usr/bin/env python3
"""Standalone regression harness for per-provider safe polling of AI usage.

Runs with a bare interpreter: standard library only, no pytest.

    python tests/ai_poll_policy_regression.py

Scope
-----
  * aipolicy.py -- the per-provider interval, Retry-After parsing, the 429 /
    403-challenge / error holds, the manual-refresh floor, and persistence of
    all of that across a restart.
  * aiproviders.py -- the scheduler flags every usage fetch must carry
    (rate_limited + retry_after, blocked_html, offline).
  * core.ai_poll_accounts_due -- a held account is not requested at all, its
    last row comes back marked `held`, and other accounts still poll.
  * wave-4 review fixes -- atomic claim, provider-wide holds, offline and
    dead-login scheduling, stale-key pruning, legacy retry_until migration,
    corrupt/foreign state files, the geo gate on an IP change, and the
    sidecar's spinner and vault-error paths (sidecar imported with emit and
    every fetcher stubbed; its loops are driven one iteration at a time).

Why the harness is paranoid
---------------------------
  * The whole point of the feature is that a vendor never sees one request
    too many. A test run that leaked a real request would be the exact
    failure the feature exists to prevent, so the network is killed at four
    layers (urlopen, OpenerDirector.open, socket.create_connection,
    socket.socket.connect), the kill-switch is PROBED before anything runs,
    and the last test asserts zero real outbound attempts. HTTP responses are
    served by a fake urlopen that answers only the two scripted usage URLs;
    any other URL trips the kill-switch and is counted as a leak.
  * The policy state file is redirected with AI_RL_FILE into a throwaway temp
    directory BEFORE any module under test is imported, and HOME/USERPROFILE
    go too, so even the default path lands in the sandbox. The real per-user
    state file is stat()ed (never read) before and after the run, and the run
    asserts it was not created, modified or removed.
  * No token value, fake or otherwise, is printed. Fake secrets are obvious
    placeholders (FAKE-...-DO-NOT-USE).

Test-design rules
-----------------
Tests are written against the interface CONTRACT for this feature, not
against whatever the implementation happens to do. A disagreement is a
finding: the test stays and the failure is reported. Expected values are
hardcoded from the contract (codex 60 s, claude 180 s, others 300 s; codex
429 backoff 120 -> 240 -> 480 -> 900 cap; ...) rather than recomputed from
the module's own constants, so a wrong constant fails instead of agreeing
with itself.

Time is injected through the `now=` argument wherever the contract offers
it, so nothing here sleeps on a timer and nothing is flaky on a slow box.
"""

import email.utils
import http.client
import io
import json
import math
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import types
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))

FAKE_ACCESS = "FAKE-ACCESS-TOKEN-DO-NOT-USE"
FAKE_REFRESH = "FAKE-REFRESH-TOKEN-DO-NOT-USE"

POLL_ENV = ("AI_POLL_SECONDS", "AI_POLL_SECONDS_CLAUDE", "AI_POLL_SECONDS_OTHER")
OTHERS = ("cursor", "copilot", "windsurf", "devin", "kimi", "cline")


# ---------------------------------------------------------------------------
# result bookkeeping (same conventions as the other suites in tests/)
# ---------------------------------------------------------------------------
class Skip(Exception):
    """Raised by a test body when the thing it covers is not present yet."""


RESULTS = []


def record(status, name, detail=""):
    RESULTS.append((status, name, detail))
    line = "%-5s %s" % (status, name)
    if detail:
        line += "  -- " + detail
    print(line, flush=True)


def check(name, fn):
    try:
        ok, detail = fn()
    except Skip as e:
        record("SKIP", name, str(e))
        return
    except Exception as e:
        record("FAIL", name, "raised %s: %s" % (type(e).__name__, e))
        return
    record("PASS" if ok else "FAIL", name, detail)


def need(mod, *attrs):
    if mod is None:
        raise Skip("module not importable")
    missing = [a for a in attrs if not hasattr(mod, a)]
    if missing:
        raise Skip("missing from module: %s" % ", ".join(missing))


def near(a, b, tol=1.0):
    try:
        return a is not None and abs(float(a) - float(b)) <= tol
    except Exception:
        return False


# ---------------------------------------------------------------------------
# sandbox, installed before anything under test is imported
# ---------------------------------------------------------------------------
# The real default state file, located BEFORE home is redirected. It is only
# ever stat()ed, never opened.
REAL_RL_FILE = os.path.join(os.path.expanduser("~"), ".ipbar_ai_ratelimit.json")


def _stat_sig(path):
    try:
        st = os.stat(path)
        return (True, st.st_size, st.st_mtime_ns)
    except OSError:
        return (False, 0, 0)


REAL_RL_BEFORE = _stat_sig(REAL_RL_FILE)

TMPDIR = os.path.realpath(tempfile.mkdtemp(prefix="nw_ai_policy_"))
FAKE_HOME = os.path.join(TMPDIR, "home")
os.makedirs(FAKE_HOME, exist_ok=True)
os.makedirs(os.path.join(TMPDIR, "appdata"), exist_ok=True)
os.makedirs(os.path.join(TMPDIR, "vault"), exist_ok=True)
os.makedirs(os.path.join(TMPDIR, "rl"), exist_ok=True)

BOOT_RL_FILE = os.path.join(TMPDIR, "rl", "boot.json")
os.environ["AI_RL_FILE"] = BOOT_RL_FILE
os.environ["AI_ACCOUNTS_FILE"] = os.path.join(TMPDIR, "vault", "accounts.json")
for _v in ("HOME", "USERPROFILE", "HOMEPATH"):
    os.environ[_v] = FAKE_HOME
os.environ["HOMEDRIVE"] = ""
os.environ["APPDATA"] = os.path.join(TMPDIR, "appdata")
os.environ["LOCALAPPDATA"] = os.path.join(TMPDIR, "appdata")
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMPDIR, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(TMPDIR, "data")
for _v in POLL_ENV:
    os.environ.pop(_v, None)


def inside_tmp(path):
    try:
        rp = os.path.realpath(path)
    except Exception:
        return False
    return os.path.normcase(rp).startswith(os.path.normcase(TMPDIR) + os.sep)


def cleanup():
    shutil.rmtree(TMPDIR, ignore_errors=True)


_RL_SEQ = [0]


def fresh_rl_path():
    _RL_SEQ[0] += 1
    return os.path.join(TMPDIR, "rl", "state_%03d.json" % _RL_SEQ[0])


# ---------------------------------------------------------------------------
# network kill-switch + scripted transport
# ---------------------------------------------------------------------------
NETWORK_ATTEMPTS = []
PROBING = {"on": False}
LOOPBACK = ("127.0.0.1", "::1", "localhost", "")

_real_connect = socket.socket.connect
_real_create_connection = socket.create_connection


def _trip(what):
    if not PROBING["on"]:
        NETWORK_ATTEMPTS.append(what)
    raise RuntimeError("network access is blocked inside the test harness")


def _blocked_opener_open(self, *a, **k):
    _trip("OpenerDirector.open")


def _blocked_create_connection(addr, *a, **k):
    host = addr[0] if isinstance(addr, tuple) and addr else ""
    if host in LOOPBACK:
        return _real_create_connection(addr, *a, **k)
    _trip("create_connection:%s" % (host,))


def _blocked_connect(self, addr, *a, **k):
    host = addr[0] if isinstance(addr, tuple) and addr else ""
    if host in LOOPBACK:
        return _real_connect(self, addr, *a, **k)
    _trip("connect:%s" % (host,))


# Scripted answers, keyed by a substring of the URL. Anything not scripted is
# a leak and trips the kill-switch.
CLAUDE_URL_PART = "api.anthropic.com/api/oauth/usage"
CODEX_URL_PART = "chatgpt.com/backend-api/wham/usage"
ROUTES = {}
FETCHES = []


def _headers(d):
    m = http.client.HTTPMessage()
    for k, v in (d or {}).items():
        m[k] = v
    return m


class _FakeResp(object):
    def __init__(self, url, status, body, hdrs):
        self.url = url
        self.status = status
        self.code = status
        self.headers = _headers(hdrs)
        self._body = body.encode("utf-8")

    def read(self, *a):
        b, self._body = self._body, b""
        return b

    def getcode(self):
        return self.status

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def info(self):
        return self.headers

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, *a, **k):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    for part, spec in ROUTES.items():
        if part in url:
            FETCHES.append(part)
            kind, status, body, hdrs = spec
            if kind == "offline":
                raise urllib.error.URLError("stubbed: no route to host")
            if status >= 400:
                raise urllib.error.HTTPError(url, status, "stub", _headers(hdrs),
                                             io.BytesIO(body.encode("utf-8")))
            return _FakeResp(url, status, body, hdrs)
    _trip("urlopen:unscripted")


def route(part, status=200, body=None, headers=None, offline=False):
    if offline:
        ROUTES[part] = ("offline", 0, "", {})
        return
    if body is None:
        body = ""
    if not isinstance(body, str):
        body = json.dumps(body)
    hdrs = dict(headers or {})
    if status < 400 or body.lstrip().startswith("{"):
        hdrs.setdefault("Content-Type", "application/json")
    ROUTES[part] = ("http", status, body, hdrs)


def reset_transport():
    ROUTES.clear()
    del FETCHES[:]


urllib.request.urlopen = _fake_urlopen
urllib.request.OpenerDirector.open = _blocked_opener_open
socket.create_connection = _blocked_create_connection
socket.socket.connect = _blocked_connect


def probe_tripwire():
    """Prove the kill-switch fires. Returns the list of layers that refused.

    TEST-NET-1 (192.0.2.0/24) is the target: reserved for documentation, it
    routes nowhere even if a layer were somehow left unpatched.
    """
    armed = []
    PROBING["on"] = True
    try:
        try:
            urllib.request.urlopen("http://192.0.2.1/never")
        except RuntimeError:
            armed.append("urlopen")
        except Exception:
            pass
        s = socket.socket()
        try:
            s.connect(("192.0.2.1", 80))
        except RuntimeError:
            armed.append("socket.connect")
        except Exception:
            pass
        finally:
            try:
                s.close()
            except Exception:
                pass
        try:
            socket.create_connection(("192.0.2.1", 80), timeout=1)
        except RuntimeError:
            armed.append("create_connection")
        except Exception:
            pass
        try:
            urllib.request.build_opener().open("http://192.0.2.1/never")
        except RuntimeError:
            armed.append("opener.open")
        except Exception:
            pass
    finally:
        PROBING["on"] = False
    return armed


TRIPWIRE_ARMED = probe_tripwire()
REQUIRED_LAYERS = {"urlopen", "socket.connect", "create_connection",
                   "opener.open"}


# ---------------------------------------------------------------------------
# import the modules under test
# ---------------------------------------------------------------------------
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

IMPORT_ERRORS = {}


def _imp(name):
    try:
        return __import__(name)
    except Exception as e:
        IMPORT_ERRORS[name] = "%s: %s" % (type(e).__name__, e)
        return None


aipolicy = _imp("aipolicy")
aiproviders = _imp("aiproviders")

# core.py is a Windows module (winreg, and netfast binds iphlpapi at import).
# Off Windows, inert stand-ins let the polling logic be exercised anyway; the
# functions under test touch neither.
CORE_STUBS = []
if os.name != "nt":
    try:
        import winreg  # noqa: F401
    except ImportError:
        sys.modules["winreg"] = types.ModuleType("winreg")
        CORE_STUBS.append("winreg")
    try:
        import netfast  # noqa: F401
    except Exception:
        sys.modules.pop("netfast", None)
        sys.modules["netfast"] = types.ModuleType("netfast")
        CORE_STUBS.append("netfast")
core = _imp("core")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def fresh():
    need(aipolicy, "reset_for_tests")
    p = fresh_rl_path()
    aipolicy.reset_for_tests(p)
    return p


class env_set(object):
    """Set (or with None, remove) the poll env vars for one block."""

    def __init__(self, **kv):
        self.kv = kv
        self.saved = {}

    def __enter__(self):
        for k in POLL_ENV:
            self.saved[k] = os.environ.get(k)
            os.environ.pop(k, None)
        for k, v in self.kv.items():
            if v is not None:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k in POLL_ENV:
            os.environ.pop(k, None)
            if self.saved.get(k) is not None:
                os.environ[k] = self.saved[k]
        return False


def fake_jwt(payload):
    import base64

    def seg(o):
        return base64.urlsafe_b64encode(
            json.dumps(o, separators=(",", ":")).encode()).decode().rstrip("=")
    return "%s.%s.%s" % (seg({"alg": "none", "typ": "JWT"}), seg(payload),
                         "FAKE-SIGNATURE-DO-NOT-USE")


FAR = int(time.time()) + 10 * 86400   # well past core's refresh lead


def claude_cred():
    return {"claudeAiOauth": {"accessToken": FAKE_ACCESS,
                              "refreshToken": FAKE_REFRESH,
                              "expiresAt": FAR * 1000,
                              "scopes": ["user:inference"]}}


def codex_cred():
    tok = fake_jwt({"exp": FAR, "sub": "fake-user"})
    return {"tokens": {"access_token": tok, "id_token": tok,
                       "refresh_token": FAKE_REFRESH,
                       "account_id": "acct-fake-codex"}}


CLAUDE_OK = {"five_hour": {"utilization": 42.0,
                           "resets_at": "2026-10-17T20:00:00Z"},
             "seven_day": {"utilization": 11.0,
                           "resets_at": "2026-10-20T20:00:00Z"}}
CODEX_OK = {"plan_type": "plus",
            "rate_limit": {"primary_window": {"used_percent": 17,
                                              "limit_window_seconds": 18000,
                                              "reset_at": FAR},
                           "secondary_window": {"used_percent": 3,
                                                "limit_window_seconds": 604800,
                                                "reset_at": FAR}}}
CF_HTML = ("<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
           "<body>Checking your browser. cf-ray: 0000fake</body></html>")


def flatten(o, prefix=""):
    out = {}
    if isinstance(o, dict):
        for k, v in o.items():
            out.update(flatten(v, ("%s.%s" % (prefix, k)) if prefix else str(k)))
    elif isinstance(o, (list, tuple)):
        for i, v in enumerate(o):
            out.update(flatten(v, "%s[%d]" % (prefix, i)))
    else:
        out[prefix] = o
    return out


def carries_number(o, value):
    return any(isinstance(v, (int, float)) and not isinstance(v, bool)
               and abs(v - value) < 0.01 for v in flatten(o).values())


# ---------------------------------------------------------------------------
# safety
# ---------------------------------------------------------------------------
def s02_paths_sandboxed():
    bad = [v for v in ("AI_RL_FILE", "AI_ACCOUNTS_FILE", "HOME", "USERPROFILE")
           if not inside_tmp(os.environ.get(v, "") or "/")]
    if bad:
        return False, "not sandboxed: %s" % ", ".join(bad)
    if inside_tmp(REAL_RL_FILE):
        return False, "real state path resolved inside the sandbox?"
    return True, "AI_RL_FILE, vault and every home redirected into the temp dir"


# ---------------------------------------------------------------------------
# aipolicy: intervals
# ---------------------------------------------------------------------------
def t_default_intervals():
    need(aipolicy, "interval")
    with env_set():
        got = {p: aipolicy.interval(p) for p in ("codex", "claude") + OTHERS}
    want = dict({"codex": 60, "claude": 180}, **{p: 300 for p in OTHERS})
    bad = {p: got[p] for p in want if not near(got[p], want[p], 0.001)}
    return not bad, ("codex 60, claude 180, others 300" if not bad
                     else "wrong: %r" % bad)


def t_alias_to_codex():
    need(aipolicy, "interval")
    with env_set():
        got = [aipolicy.interval(p) for p in ("chatgpt", "openai")]
    with env_set(AI_POLL_SECONDS="90"):
        got_env = [aipolicy.interval(p) for p in ("chatgpt", "openai")]
    ok = all(near(g, 60, 0.001) for g in got) and \
        all(near(g, 90, 0.001) for g in got_env)
    return ok, "chatgpt/openai default %r, with AI_POLL_SECONDS=90 %r" % (
        got, got_env)


def t_env_overrides():
    need(aipolicy, "interval")
    with env_set(AI_POLL_SECONDS="90", AI_POLL_SECONDS_CLAUDE="240",
                 AI_POLL_SECONDS_OTHER="600"):
        got = (aipolicy.interval("codex"), aipolicy.interval("claude"),
               aipolicy.interval("cursor"), aipolicy.interval("kimi"))
    ok = all(near(g, w, 0.001) for g, w in zip(got, (90, 240, 600, 600)))
    return ok, "codex/claude/cursor/kimi = %r (want 90/240/600/600)" % (got,)


def t_env_no_crosstalk():
    need(aipolicy, "interval")
    with env_set(AI_POLL_SECONDS="900"):
        got = (aipolicy.interval("claude"), aipolicy.interval("cursor"))
    ok = near(got[0], 180, 0.001) and near(got[1], 300, 0.001)
    return ok, "AI_POLL_SECONDS=900 leaves claude/cursor at %r" % (got,)


def t_env_read_at_call_time():
    need(aipolicy, "interval")
    with env_set(AI_POLL_SECONDS_CLAUDE="200"):
        a = aipolicy.interval("claude")
    with env_set(AI_POLL_SECONDS_CLAUDE="400"):
        b = aipolicy.interval("claude")
    return near(a, 200, 0.001) and near(b, 400, 0.001), \
        "200 then 400 -> %r, %r" % (a, b)


def t_env_floor_and_ceiling_clamp():
    need(aipolicy, "interval")
    cases = [("AI_POLL_SECONDS_CLAUDE", "claude", "30", 120),
             ("AI_POLL_SECONDS_CLAUDE", "claude", "119", 120),
             ("AI_POLL_SECONDS_CLAUDE", "claude", "120", 120),
             ("AI_POLL_SECONDS", "codex", "10", 60),
             ("AI_POLL_SECONDS", "codex", "0", 60),
             ("AI_POLL_SECONDS", "codex", "-500", 60),
             ("AI_POLL_SECONDS_OTHER", "cursor", "5", 120),
             ("AI_POLL_SECONDS_OTHER", "copilot", "99999", 3600),
             ("AI_POLL_SECONDS_CLAUDE", "claude", "3601", 3600)]
    bad = []
    for var, prov, val, want in cases:
        with env_set(**{var: val}):
            got = aipolicy.interval(prov)
        if not near(got, want, 0.001):
            bad.append("%s=%s -> %r (want %r)" % (var, val, got, want))
    return not bad, ("%d floor/ceiling cases clamp" % len(cases)
                     if not bad else "; ".join(bad))


def t_env_garbage_falls_back():
    need(aipolicy, "interval")
    junk = ["nan", "NaN", "inf", "-inf", "abc", "", "   ", "60s", "1e400",
            "0x3c"]
    bad = []
    for j in junk:
        with env_set(AI_POLL_SECONDS=j, AI_POLL_SECONDS_CLAUDE=j,
                     AI_POLL_SECONDS_OTHER=j):
            got = (aipolicy.interval("codex"), aipolicy.interval("claude"),
                   aipolicy.interval("devin"))
        if not all(isinstance(g, (int, float)) and math.isfinite(g)
                   for g in got):
            bad.append("%r -> non-finite %r" % (j, got))
        elif got != (60, 180, 300) and not all(
                near(g, w, 0.001) for g, w in zip(got, (60, 180, 300))):
            bad.append("%r -> %r" % (j, got))
    return not bad, ("%d garbage values fall back to defaults" % len(junk)
                     if not bad else "; ".join(bad))


def t_env_mapping_argument():
    need(aipolicy, "interval")
    with env_set():
        try:
            got = aipolicy.interval("claude",
                                    env={"AI_POLL_SECONDS_CLAUDE": "200"})
        except TypeError:
            return False, "interval() does not accept the env= mapping"
    return near(got, 200, 0.001), "env={'AI_POLL_SECONDS_CLAUDE':'200'} -> %r" % got


def t_interval_junk_provider_never_raises():
    need(aipolicy, "interval")
    with env_set():
        got = [aipolicy.interval(p) for p in (None, "", 42, "CLAUDE", " codex ")]
    ok = all(isinstance(g, (int, float)) and g >= 60 for g in got)
    return ok, "None/''/42/'CLAUDE'/' codex ' -> %r" % (got,)


# ---------------------------------------------------------------------------
# aipolicy: Retry-After
# ---------------------------------------------------------------------------
def t_retry_after_seconds():
    need(aipolicy, "parse_retry_after")
    vals = [120, 120.0, "120", " 120 ", "120.0", b"120"]
    got = [aipolicy.parse_retry_after(v) for v in vals]
    bad = [(v, g) for v, g in zip(vals, got) if not near(g, 120, 0.001)]
    return not bad, ("int/float/str/bytes seconds -> 120" if not bad
                     else "wrong: %r" % bad)


def t_retry_after_http_date():
    need(aipolicy, "parse_retry_after")
    now = 1_800_000_000.0
    hdr = email.utils.formatdate(now + 300, usegmt=True)
    got = aipolicy.parse_retry_after(hdr, now=now)
    return near(got, 300, 1.0), "%r with now fixed -> %r (want 300)" % (hdr, got)


def t_retry_after_zero_negative_past():
    need(aipolicy, "parse_retry_after")
    now = 1_800_000_000.0
    past = email.utils.formatdate(now - 60, usegmt=True)
    vals = [0, "0", 0.0, -5, "-5", past]
    got = [aipolicy.parse_retry_after(v, now=now) for v in vals]
    bad = [(v, g) for v, g in zip(vals, got) if g is not None]
    return not bad, ("0, negative, past date -> None" if not bad
                     else "should be None: %r" % bad)


def t_retry_after_garbage():
    need(aipolicy, "parse_retry_after")
    vals = [None, "", "soon", "nan", "inf", "Mon, 99 Foo 2026", [], {}, object(),
            True, b"\xff\xfe"]
    bad = []
    for v in vals:
        try:
            g = aipolicy.parse_retry_after(v)
        except Exception as e:
            bad.append("%r raised %s" % (v, type(e).__name__))
            continue
        if g is not None:
            bad.append("%r -> %r" % (v, g))
    return not bad, ("%d junk values -> None, none raise" % len(vals)
                     if not bad else "; ".join(bad))


def t_key_shape():
    need(aipolicy, "key")
    a = aipolicy.key("claude", "acct-1")
    b = aipolicy.key("codex", None)
    ok = a == "claude:acct-1" and b == "codex:legacy"
    return ok, "key(claude, acct-1)=%r key(codex, None)=%r" % (a, b)


# ---------------------------------------------------------------------------
# aipolicy: holds
# ---------------------------------------------------------------------------
T0 = 1_800_000_000.0


def _rl_sequence(provider, n, retry_after=None):
    """n consecutive 429s, each recorded just after the previous hold ends."""
    fresh()
    k = aipolicy.key(provider, "seq")
    now = T0
    holds = []
    for _ in range(n):
        h = aipolicy.record_rate_limited(k, provider, retry_after=retry_after,
                                         now=now)
        holds.append(round(float(h), 3))
        now += float(h) + 1.0
    return holds


def t_unknown_key_due():
    need(aipolicy, "due")
    fresh()
    ok1, left = aipolicy.due("claude:never-seen", "claude", now=T0)
    ok2, _ = aipolicy.due("claude:never-seen", "claude", now=T0, manual=True)
    return ok1 is True and ok2 is True, "scheduled %r/%r manual %r" % (
        ok1, left, ok2)


def t_rl_backoff_codex():
    need(aipolicy, "record_rate_limited", "key")
    got = _rl_sequence("codex", 5)
    want = [120, 240, 480, 900, 900]
    return got == want, "codex 429 holds %r (want %r)" % (got, want)


def t_rl_backoff_claude():
    need(aipolicy, "record_rate_limited", "key")
    got = _rl_sequence("claude", 5)
    want = [300, 600, 1200, 1800, 1800]
    return got == want, "claude 429 holds %r (want %r)" % (got, want)


def t_rl_backoff_other():
    need(aipolicy, "record_rate_limited", "key")
    got = _rl_sequence("cursor", 4)
    want = [300, 600, 900, 900]
    return got == want, "cursor 429 holds %r (want %r)" % (got, want)


def t_rl_many_strikes_no_overflow():
    need(aipolicy, "record_rate_limited", "key")
    got = _rl_sequence("claude", 80)
    ok = all(math.isfinite(h) and h <= 1800 for h in got) and got[-1] == 1800
    return ok, "80 strikes: max %r, last %r" % (max(got), got[-1])


def t_rl_retry_after_longer_is_honoured():
    need(aipolicy, "record_rate_limited", "key")
    fresh()
    h = aipolicy.record_rate_limited("codex:ra", "codex", retry_after=3000,
                                     now=T0)
    fresh()
    h2 = aipolicy.record_rate_limited("claude:ra", "claude", retry_after="2400",
                                      now=T0)
    return near(h, 3000, 0.5) and near(h2, 2400, 0.5), \
        "codex RA=3000 -> %r, claude RA='2400' -> %r (server wins above cap)" % (
            h, h2)


def t_rl_retry_after_clamped_6h():
    need(aipolicy, "record_rate_limited", "key")
    fresh()
    h = aipolicy.record_rate_limited("claude:huge", "claude",
                                     retry_after=10 ** 7, now=T0)
    return near(h, 6 * 3600, 0.5), "RA=10^7 -> %r (want 21600)" % h


def t_rl_retry_after_short_or_zero_does_not_shorten():
    need(aipolicy, "record_rate_limited", "key")
    out = []
    for ra in (10, "0", 0, None, "garbage"):
        fresh()
        out.append(aipolicy.record_rate_limited("claude:s", "claude",
                                                retry_after=ra, now=T0))
    ok = all(near(h, 300, 0.5) for h in out)
    return ok, "claude RA 10/'0'/0/None/garbage -> %r (want 300 each)" % (out,)


def t_never_shorten_blocked_then_429():
    need(aipolicy, "record_blocked", "record_rate_limited", "hold_left")
    fresh()
    k = "codex:ns"
    aipolicy.record_blocked(k, "codex", now=T0)
    aipolicy.record_rate_limited(k, "codex", retry_after=5, now=T0 + 10)
    aipolicy.record_error(k, "codex", now=T0 + 20)
    left = aipolicy.hold_left(k, now=T0 + 20)
    return near(left, 3580, 1.0), \
        "after 1 h block, a 429 and an error 10/20 s later: %r left (want 3580)" % left


def t_never_shorten_long_429_then_short_429():
    need(aipolicy, "record_rate_limited", "hold_left")
    fresh()
    k = "claude:ns2"
    aipolicy.record_rate_limited(k, "claude", retry_after=5000, now=T0)
    aipolicy.record_rate_limited(k, "claude", retry_after=1, now=T0 + 100)
    left = aipolicy.hold_left(k, now=T0 + 100)
    return near(left, 4900, 1.0), "%r left (want 4900)" % left


def t_held_key_not_due_scheduled():
    need(aipolicy, "record_rate_limited", "due")
    fresh()
    k = "codex:h"
    aipolicy.record_rate_limited(k, "codex", retry_after=600, now=T0)
    a = aipolicy.due(k, "codex", now=T0 + 599)
    b = aipolicy.due(k, "codex", now=T0 + 601)
    ok = a[0] is False and near(a[1], 1, 0.5) and b[0] is True
    return ok, "at +599 %r, at +601 %r" % (a, b)


def t_manual_cannot_bypass_hold():
    need(aipolicy, "record_rate_limited", "due")
    fresh()
    k = "claude:m"
    aipolicy.record_rate_limited(k, "claude", now=T0)
    res = [aipolicy.due(k, "claude", now=T0 + dt, manual=True)
           for dt in (31, 60, 299)]
    ok = all(r[0] is False for r in res) and near(res[1][1], 240, 0.5)
    return ok, "manual during a 300 s hold at +31/+60/+299: %r" % (res,)


def t_manual_cannot_bypass_block():
    need(aipolicy, "record_blocked", "due")
    fresh()
    k = "codex:mb"
    aipolicy.record_blocked(k, "codex", now=T0)
    r = aipolicy.due(k, "codex", now=T0 + 1800, manual=True)
    return r[0] is False and near(r[1], 1800, 0.5), "manual at +1800: %r" % (r,)


def t_manual_floor_30s():
    # Contract change (wave 4): manual floor = max(30 s, provider floor),
    # so codex is 60 s, not 30 s.
    need(aipolicy, "record_success", "due")
    fresh()
    k = "codex:mf"
    aipolicy.record_success(k, "codex", now=T0)
    a = aipolicy.due(k, "codex", now=T0 + 10, manual=True)
    b = aipolicy.due(k, "codex", now=T0 + 59.9, manual=True)
    c = aipolicy.due(k, "codex", now=T0 + 60, manual=True)
    ok = (a[0] is False and near(a[1], 50, 0.01) and b[0] is False
          and c[0] is True)
    return ok, "+10 %r, +59.9 %r, +60 %r" % (a, b, c)


def t_manual_floor_applies_to_claude_too():
    need(aipolicy, "record_success", "due")
    fresh()
    k = "claude:mf"
    aipolicy.record_success(k, "claude", now=T0)
    a = aipolicy.due(k, "claude", now=T0 + 5, manual=True)
    b = aipolicy.due(k, "claude", now=T0 + 120, manual=True)
    return a[0] is False and b[0] is True, "+5 %r, +120 %r" % (a, b)


def t_scheduled_due_inside_jitter_band():
    need(aipolicy, "record_success", "due")
    fresh()
    bad = []
    for i in range(40):
        k = "codex:j%02d" % i
        aipolicy.record_success(k, "codex", now=T0)
        early = aipolicy.due(k, "codex", now=T0 + 53.9)
        late = aipolicy.due(k, "codex", now=T0 + 66.1)
        if early[0] is not False or late[0] is not True:
            bad.append((k, early, late))
    return not bad, ("40 codex keys: never due before 54 s, always by 66 s"
                     if not bad else "outside +-10%% band: %r" % bad[:3])


def t_scheduled_claude_respects_180():
    need(aipolicy, "record_success", "due")
    fresh()
    bad = []
    for i in range(40):
        k = "claude:j%02d" % i
        aipolicy.record_success(k, "claude", now=T0)
        if aipolicy.due(k, "claude", now=T0 + 161.9)[0] is not False:
            bad.append(k)
        if aipolicy.due(k, "claude", now=T0 + 198.1)[0] is not True:
            bad.append(k)
    return not bad, ("40 claude keys inside [162, 198] s"
                     if not bad else "outside band: %r" % bad[:3])


def t_success_clears_hold_and_strikes():
    need(aipolicy, "record_rate_limited", "record_success", "hold_left")
    fresh()
    k = "codex:clr"
    aipolicy.record_rate_limited(k, "codex", now=T0)          # 120
    aipolicy.record_rate_limited(k, "codex", now=T0 + 121)    # 240
    aipolicy.record_success(k, "codex", now=T0 + 400)
    left = aipolicy.hold_left(k, now=T0 + 400)
    h = aipolicy.record_rate_limited(k, "codex", now=T0 + 500)
    return near(left, 0, 0.001) and near(h, 120, 0.5), \
        "hold after success %r, next 429 %r (want 0, 120)" % (left, h)


def t_blocked_hold_and_strike():
    need(aipolicy, "record_blocked", "record_rate_limited")
    fresh()
    k = "codex:blk"
    h = aipolicy.record_blocked(k, "codex", now=T0)
    h2 = aipolicy.record_rate_limited(k, "codex", now=T0 + 3601)
    return near(h, 3600, 0.5) and near(h2, 240, 0.5), \
        "403 challenge hold %r; next 429 %r (strike counted -> 240)" % (h, h2)


def t_error_backoff():
    need(aipolicy, "record_error", "key")
    out = {}
    for prov, n in (("codex", 6), ("claude", 5)):
        fresh()
        now, hs = T0, []
        for _ in range(n):
            h = aipolicy.record_error(prov + ":e", prov, now=now)
            hs.append(round(float(h), 3))
            now += h + 1
        out[prov] = hs
    want = {"codex": [60, 120, 240, 480, 900, 900],
            "claude": [180, 360, 720, 900, 900]}
    return out == want, "error holds %r (want %r)" % (out, want)


def t_next_wake():
    need(aipolicy, "next_wake", "record_rate_limited", "record_success")
    fresh()
    empty = aipolicy.next_wake(now=T0)
    aipolicy.record_rate_limited("claude:nw", "claude", retry_after=600,
                                 now=T0)
    held = aipolicy.next_wake(now=T0)
    aipolicy.record_success("codex:nw", "codex", now=T0 - 1000)
    floor = aipolicy.next_wake(now=T0)
    ok = near(empty, 0, 0.001) and near(held, 600, 1.0) and near(floor, 5, 0.001)
    return ok, "no keys %r; one 600 s hold %r; plus an overdue key %r" % (
        empty, held, floor)


# ---------------------------------------------------------------------------
# aipolicy: persistence
# ---------------------------------------------------------------------------
def t_hold_survives_restart():
    need(aipolicy, "record_rate_limited", "hold_left", "due")
    p = fresh()
    now = time.time()
    k = "claude:restart"
    aipolicy.record_rate_limited(k, "claude", retry_after=1000, now=now)
    aipolicy.reset_for_tests(p)          # process restart: memory gone
    left = aipolicy.hold_left(k, now=now + 10)
    d = aipolicy.due(k, "claude", now=now + 10, manual=True)
    on_disk = os.path.isfile(p)
    ok = on_disk and near(left, 990, 1.0) and d[0] is False
    return ok, "file %s; after reload hold %r, manual due %r" % (
        "written" if on_disk else "MISSING", left, d)


def t_strikes_survive_restart():
    need(aipolicy, "record_rate_limited")
    p = fresh()
    now = time.time()
    k = "codex:strikes"
    aipolicy.record_rate_limited(k, "codex", now=now)          # 120
    aipolicy.reset_for_tests(p)
    h = aipolicy.record_rate_limited(k, "codex", now=now + 121)
    return near(h, 240, 0.5), "second 429 after a restart -> %r (want 240)" % h


def t_schedule_survives_restart():
    need(aipolicy, "record_success", "due")
    p = fresh()
    now = time.time()
    k = "codex:sched"
    aipolicy.record_success(k, "codex", now=now)
    aipolicy.reset_for_tests(p)
    d = aipolicy.due(k, "codex", now=now + 20)
    return d[0] is False, "scheduled due 20 s after a pre-restart success: %r" % (d,)


def t_env_path_used_when_no_override():
    need(aipolicy, "reset_for_tests", "record_blocked", "hold_left")
    p = fresh_rl_path()
    saved = os.environ.get("AI_RL_FILE")
    os.environ["AI_RL_FILE"] = p
    try:
        aipolicy.reset_for_tests(None)
        now = time.time()
        aipolicy.record_blocked("claude:envp", "claude", now=now)
        written = os.path.isfile(p)
        aipolicy.reset_for_tests(None)
        left = aipolicy.hold_left("claude:envp", now=now)
    finally:
        os.environ["AI_RL_FILE"] = saved
    return written and near(left, 3600, 1.0), \
        "AI_RL_FILE honoured: file %s, reload hold %r" % (
            "written" if written else "MISSING", left)


def t_corrupt_file_is_empty_state():
    need(aipolicy, "reset_for_tests", "due", "hold_left", "snapshot")
    bad = []
    for i, blob in enumerate(["{not json", "", "[1, 2, 3]", "null",
                              '{"keys": "nope"}',
                              '{"keys": {"claude:x": "garbage"}}',
                              '{"keys": {"claude:x": {"hold_until": "soon",'
                              ' "strikes": "many"}}}',
                              "\x00\xff binary"]):
        p = fresh_rl_path()
        with open(p, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(blob)
        try:
            aipolicy.reset_for_tests(p)
            d = aipolicy.due("claude:x", "claude", now=T0)
            left = aipolicy.hold_left("claude:x", now=T0)
            aipolicy.snapshot()
            aipolicy.next_wake(now=T0)
        except Exception as e:
            bad.append("#%d raised %s" % (i, type(e).__name__))
            continue
        if left != 0:
            bad.append("#%d -> hold %r" % (i, left))
    return not bad, ("8 corrupt files load with no hold, nothing raises"
                     if not bad else "; ".join(bad))


def t_corrupt_file_is_repaired_on_write():
    need(aipolicy, "record_blocked", "reset_for_tests", "hold_left")
    p = fresh_rl_path()
    with open(p, "w") as fh:
        fh.write("{definitely not json")
    aipolicy.reset_for_tests(p)
    now = time.time()
    aipolicy.record_blocked("codex:rep", "codex", now=now)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            json.load(fh)
        valid = True
    except Exception:
        valid = False
    aipolicy.reset_for_tests(p)
    left = aipolicy.hold_left("codex:rep", now=now)
    return valid and near(left, 3600, 1.0), \
        "file valid JSON after write: %s; reload hold %r" % (valid, left)


def t_atomic_write_leaves_no_temp_files():
    need(aipolicy, "record_error")
    d = os.path.join(TMPDIR, "rl_atomic")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "state.json")
    aipolicy.reset_for_tests(p)
    for i in range(25):
        aipolicy.record_error("codex:a%d" % i, "codex", now=T0 + i)
    left = sorted(os.listdir(d))
    return left == ["state.json"], "directory after 25 writes: %r" % (left,)


def t_snapshot_json_safe():
    need(aipolicy, "snapshot", "record_rate_limited")
    fresh()
    aipolicy.record_rate_limited("claude:snap", "claude", retry_after=120,
                                 now=time.time())
    snap = aipolicy.snapshot()
    txt = json.dumps(snap)
    ok = "claude:snap" in txt and FAKE_ACCESS not in txt
    return ok, "snapshot serialises (%d bytes) and names the key" % len(txt)


# ---------------------------------------------------------------------------
# aiproviders: scheduler flags
# ---------------------------------------------------------------------------
def _usage(provider, cred):
    need(aiproviders, "fetch_usage")
    return aiproviders.fetch_usage(provider, cred) or {}


def _flag_429(provider, part, cred_fn):
    def t():
        reset_transport()
        route(part, 429, {"error": {"type": "rate_limit_error"}},
              headers={"Retry-After": "120"})
        out = _usage(provider, cred_fn())
        ok = (out.get("rate_limited") is True
              and near(out.get("retry_after"), 120, 0.001)
              and FETCHES.count(part) == 1)
        return ok, "rate_limited=%r retry_after=%r error=%r requests=%d" % (
            out.get("rate_limited"), out.get("retry_after"), out.get("error"),
            FETCHES.count(part))
    return t


def _flag_429_no_header(provider, part, cred_fn):
    def t():
        reset_transport()
        route(part, 429, {"error": "slow down"})
        out = _usage(provider, cred_fn())
        ok = out.get("rate_limited") is True and out.get("retry_after") is None
        return ok, "rate_limited=%r retry_after=%r" % (
            out.get("rate_limited"), out.get("retry_after"))
    return t


def _flag_429_keeps_error_string(provider, part, cred_fn):
    def t():
        reset_transport()
        route(part, 429, {}, headers={"Retry-After": "120"})
        out = _usage(provider, cred_fn())
        return out.get("error") == "rate limited", "error=%r" % out.get("error")
    return t


def _flag_403_html(provider, part, cred_fn):
    def t():
        reset_transport()
        route(part, 403, CF_HTML, headers={"Content-Type": "text/html",
                                           "cf-ray": "0000fake"})
        out = _usage(provider, cred_fn())
        ok = out.get("blocked_html") is True and not out.get("rate_limited")
        return ok, "blocked_html=%r error=%r" % (out.get("blocked_html"),
                                                 out.get("error"))
    return t


def _flag_403_json_not_html(provider, part, cred_fn):
    def t():
        reset_transport()
        route(part, 403, {"error": {"message": "permission denied"}})
        out = _usage(provider, cred_fn())
        return not out.get("blocked_html"), \
            "JSON 403 -> blocked_html=%r error=%r" % (
                out.get("blocked_html"), out.get("error"))
    return t


def _flag_offline(provider, part, cred_fn):
    def t():
        reset_transport()
        route(part, offline=True)
        out = _usage(provider, cred_fn())
        ok = out.get("offline") is True and not out.get("rate_limited")
        return ok, "offline=%r error=%r" % (out.get("offline"), out.get("error"))
    return t


def _flag_ok_carries_no_flags(provider, part, cred_fn, body):
    def t():
        reset_transport()
        route(part, 200, body)
        out = _usage(provider, cred_fn())
        raised = [f for f in ("rate_limited", "blocked_html", "offline",
                              "geo_blocked") if out.get(f)]
        ok = not raised and not out.get("error")
        return ok, "200 -> flags %r error=%r" % (raised, out.get("error"))
    return t


# ---------------------------------------------------------------------------
# core.ai_poll_accounts_due
# ---------------------------------------------------------------------------
ACCTS = [
    {"id": "acct-claude-1", "provider": "claude", "label": "Claude test",
     "cred": None},
    {"id": "acct-codex-1", "provider": "codex", "label": "Codex test",
     "cred": None},
]


class _FakeVault(object):
    """In-memory stand-in for aiaccounts: the accounts, nothing else."""

    def __init__(self, accts):
        self._accts = accts

    def list_accounts(self):
        return [dict(a) for a in self._accts]

    def load(self):
        return {"accounts": self.list_accounts()}


def _accounts():
    out = []
    for a in ACCTS:
        a = dict(a)
        a["cred"] = claude_cred() if a["provider"] == "claude" else codex_cred()
        out.append(a)
    return out


def _with_vault(fn):
    need(core, "ai_poll_accounts_due")
    need(aipolicy, "reset_for_tests", "key", "record_rate_limited")
    if getattr(core, "_prov", None) is None:
        raise Skip("core could not import aiproviders")
    saved = getattr(core, "_vault", None)
    core._vault = _FakeVault(_accounts())
    try:
        return fn()
    finally:
        core._vault = saved


def _clear_last_rows():
    rows = getattr(core, "_AI_LAST_ROWS", None)
    if isinstance(rows, dict):
        rows.clear()


def _split(res):
    if isinstance(res, tuple) and len(res) == 2:
        return res[0] or [], res[1]
    return res, None


def _row(rows, aid):
    for r in rows or []:
        if isinstance(r, dict) and r.get("id") == aid:
            return r
    return None


SCENARIO = {}


def held_scenario():
    """Poll both accounts OK, then hold claude and poll again (run once)."""
    if SCENARIO:
        return SCENARIO

    def run():
        reset_transport()
        _clear_last_rows()
        fresh()
        route(CLAUDE_URL_PART, 200, CLAUDE_OK)
        route(CODEX_URL_PART, 200, CODEX_OK)
        rows1, req1 = _split(core.ai_poll_accounts_due(manual=False))
        first = {"claude": FETCHES.count(CLAUDE_URL_PART),
                 "codex": FETCHES.count(CODEX_URL_PART)}
        # Fresh policy store: codex is unknown (so due) while claude's
        # in-memory last row survives -- then claude goes on hold.
        fresh()
        aipolicy.record_rate_limited(aipolicy.key("claude", "acct-claude-1"),
                                     "claude", retry_after=600)
        reset_transport()
        route(CLAUDE_URL_PART, 500, {"error": "must not be requested"})
        route(CODEX_URL_PART, 200, CODEX_OK)
        rows2, req2 = _split(core.ai_poll_accounts_due(manual=False))
        second = {"claude": FETCHES.count(CLAUDE_URL_PART),
                  "codex": FETCHES.count(CODEX_URL_PART)}
        # And the manual button, while claude is still held.
        reset_transport()
        route(CLAUDE_URL_PART, 500, {"error": "must not be requested"})
        rows3, req3 = _split(core.ai_poll_accounts_due(manual=True))
        third = {"claude": FETCHES.count(CLAUDE_URL_PART)}
        SCENARIO.update(rows1=rows1, req1=req1, first=first, rows2=rows2,
                        req2=req2, second=second, rows3=rows3, third=third)
        return SCENARIO
    return _with_vault(run)


def t_core_first_poll_requests_both():
    s = held_scenario()
    ok = s["first"] == {"claude": 1, "codex": 1} and s["req1"] is True
    return ok, "first tick fetches %r, requested=%r" % (s["first"], s["req1"])


def t_core_held_account_not_requested():
    s = held_scenario()
    n = s["second"]["claude"]
    return n == 0, "claude usage requests while held: %d (want 0)" % n


def t_core_other_provider_still_requested():
    s = held_scenario()
    n = s["second"]["codex"]
    return n == 1 and s["req2"] is True, \
        "codex requests %d, requested=%r (want 1, True)" % (n, s["req2"])


def t_core_held_row_returned_with_held():
    s = held_scenario()
    r = _row(s["rows2"], "acct-claude-1")
    if r is None:
        return False, "no row for the held claude account"
    held = r.get("held")
    ok = isinstance(held, (int, float)) and not isinstance(held, bool) \
        and 500 <= held <= 600
    return ok, "held=%r (want ~600)" % (held,)


def t_core_held_row_is_last_reading():
    s = held_scenario()
    r = _row(s["rows2"], "acct-claude-1")
    if r is None:
        return False, "no row for the held claude account"
    ok = carries_number(r.get("usage"), 42.0) and r.get("status") != "error"
    return ok, "status=%r usage carries the pre-hold 42%%: %s" % (
        r.get("status"), carries_number(r.get("usage"), 42.0))


def t_core_codex_row_fresh_and_not_held():
    s = held_scenario()
    r = _row(s["rows2"], "acct-codex-1")
    if r is None:
        return False, "no row for codex"
    ok = r.get("status") == "ok" and not r.get("held")
    return ok, "codex status=%r held=%r" % (r.get("status"), r.get("held"))


def t_core_manual_does_not_bypass_hold():
    s = held_scenario()
    n = s["third"]["claude"]
    r = _row(s["rows3"], "acct-claude-1")
    held = (r or {}).get("held")
    return n == 0 and bool(held), \
        "manual tick: claude requests %d, held=%r" % (n, held)


def t_core_all_held_requests_nothing():
    def run():
        reset_transport()
        _clear_last_rows()
        fresh()
        for a in ACCTS:
            aipolicy.record_blocked(aipolicy.key(a["provider"], a["id"]),
                                    a["provider"])
        route(CLAUDE_URL_PART, 200, CLAUDE_OK)
        route(CODEX_URL_PART, 200, CODEX_OK)
        rows, req = _split(core.ai_poll_accounts_due(manual=True))
        helds = [(_row(rows, a["id"]) or {}).get("held") for a in ACCTS]
        ok = (len(FETCHES) == 0 and req is False
              and all(isinstance(h, (int, float)) and h > 3000 for h in helds))
        return ok, "requests %d, requested=%r, held=%r" % (len(FETCHES), req,
                                                           helds)
    return _with_vault(run)


def t_core_429_records_server_hold():
    def run():
        reset_transport()
        _clear_last_rows()
        fresh()
        aipolicy.record_blocked(aipolicy.key("claude", "acct-claude-1"),
                                "claude")
        route(CODEX_URL_PART, 429, {}, headers={"Retry-After": "700"})
        core.ai_poll_accounts_due(manual=False)
        k = aipolicy.key("codex", "acct-codex-1")
        left = aipolicy.hold_left(k)
        reset_transport()
        route(CODEX_URL_PART, 200, CODEX_OK)
        core.ai_poll_accounts_due(manual=True)
        again = FETCHES.count(CODEX_URL_PART)
        ok = 650 <= left <= 700 and again == 0
        return ok, "codex hold after 429 RA=700: %.0f s; re-requested %d" % (
            left, again)
    return _with_vault(run)


def t_core_403_html_records_block_hold():
    def run():
        reset_transport()
        _clear_last_rows()
        fresh()
        aipolicy.record_blocked(aipolicy.key("claude", "acct-claude-1"),
                                "claude")
        route(CODEX_URL_PART, 403, CF_HTML,
              headers={"Content-Type": "text/html"})
        core.ai_poll_accounts_due(manual=False)
        left = aipolicy.hold_left(aipolicy.key("codex", "acct-codex-1"))
        return 3500 <= left <= 3600, "codex hold after 403 HTML: %.0f s" % left
    return _with_vault(run)


def t_core_success_schedules_next_poll():
    def run():
        reset_transport()
        _clear_last_rows()
        fresh()
        aipolicy.record_blocked(aipolicy.key("claude", "acct-claude-1"),
                                "claude")
        route(CODEX_URL_PART, 200, CODEX_OK)
        core.ai_poll_accounts_due(manual=False)
        core.ai_poll_accounts_due(manual=False)
        n = FETCHES.count(CODEX_URL_PART)
        return n == 1, "two back-to-back scheduled ticks -> %d codex requests" % n
    return _with_vault(run)


def t_core_legacy_signature_kept():
    need(core, "ai_poll_accounts")
    import inspect
    try:
        params = list(inspect.signature(core.ai_poll_accounts).parameters)
    except Exception as e:
        return False, "signature unreadable: %s" % e
    ok = params[:2] == ["lead", "spacing"]
    return ok, "ai_poll_accounts%r" % (tuple(params),)


# ---------------------------------------------------------------------------
# wave-4 fixes (review findings) -- helpers
# ---------------------------------------------------------------------------
# These tests are written against the wave-4 fix contract: the names below
# (aipolicy.claim, provider_hold_left, record_offline, record_login_dead,
# clear, forget_except, next_wake(keys=), seed_hold, manual_floor;
# core.geo_invalidate, core.ai_active_keys) are REQUIRED by it, so a missing
# name is a FAIL, not a SKIP.
class Missing(Exception):
    """A name the fix contract requires is not there."""


def require(mod, *attrs):
    if mod is None:
        raise Missing("module not importable")
    missing = [a for a in attrs if not hasattr(mod, a)]
    if missing:
        raise Missing("contract name(s) missing: %s" % ", ".join(missing))


_ABSENT = object()


class patched(object):
    """Set attributes on one object for a block, then put them back."""

    def __init__(self, obj, **attrs):
        self.obj = obj
        self.attrs = attrs
        self.saved = {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.saved[k] = getattr(self.obj, k, _ABSENT)
            setattr(self.obj, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is _ABSENT:
                try:
                    delattr(self.obj, k)
                except AttributeError:
                    pass
            else:
                setattr(self.obj, k, v)
        return False


class env_var(object):
    def __init__(self, **kv):
        self.kv = kv
        self.saved = {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _clear_core_memory():
    for name in ("_AI_LAST_ROWS", "_AI_LAST_GOOD"):
        d = getattr(core, name, None)
        if isinstance(d, dict):
            d.clear()


def _iran_ip():
    for net in getattr(core, "IRAN_NETS", []) or []:
        for host in net.hosts():
            ip = str(host)
            if core.is_iran(ip):
                return ip
            break
    raise Missing("core.IRAN_NETS has no usable Iranian address")


_SIDECAR = {}


def get_sidecar():
    """Import sidecar once (it only defines things at import; no threads)."""
    if "mod" not in _SIDECAR:
        try:
            import sidecar as _sc  # noqa: E402
            _SIDECAR["mod"] = _sc
        except Exception as e:
            _SIDECAR["mod"] = None
            _SIDECAR["err"] = "%s: %s" % (type(e).__name__, e)
    if _SIDECAR["mod"] is None:
        raise Missing("sidecar not importable -- %s" % _SIDECAR.get("err"))
    return _SIDECAR["mod"]


class _RaisingVault(_FakeVault):
    def list_accounts(self):
        raise OSError("vault locked (stub)")


def _vault_get_account(self, aid):
    for a in self.list_accounts():
        if a.get("id") == aid:
            return a
    return None


_FakeVault.get_account = _vault_get_account

_CACHE_SEQ = [0]


class Rig(object):
    """sidecar + core with every outbound path replaced by a counting stub.

    vault: "none" (legacy path), "ok" (the two test accounts) or "raise"
    (a vault that exists but cannot be read). Every request a stub answers is
    appended to self.requests as an aipolicy-style key.
    """

    def __init__(self, vault="none", real_geo=False, nosleep=True):
        self.vault = vault
        self.real_geo = real_geo
        self.nosleep = nosleep
        self.ev = []
        self.requests = []
        self.legacy = {"claude": [], "codex": []}
        self.vault_rows = {}
        self.poll_delay = 0.0
        self._patches = []

    def _p(self, obj, **kw):
        p = patched(obj, **kw)
        p.__enter__()
        self._patches.append(p)

    def _legacy(self, pid):
        def fetch():
            self.requests.append(pid + ":legacy")
            seq = self.legacy[pid]
            if seq:
                return dict(seq.pop(0))
            if pid == "claude":
                return {"session_pct": 42.0, "week_pct": 11.0, "err": None}
            return {"sess_pct": 17.0, "week_pct": 3.0, "err": None}
        return fetch

    def _acct_poll(self, a, lead=None, **k):
        self.requests.append("%s:%s" % (a.get("provider"), a.get("id")))
        self.polling_seen_at_request = sum(
            1 for kind, f in self.ev if kind == "ai" and f.get("status") == "polling")
        if self.poll_delay:
            threading.Event().wait(self.poll_delay)
        row = self.vault_rows.get(a.get("id"))
        if row:
            return dict(row)
        return {"id": a.get("id"), "provider": a.get("provider"),
                "label": a.get("label"), "status": "ok",
                "usage": {"session_pct": 42.0}, "error": None,
                "needs_login": False}

    def __enter__(self):
        sc = get_sidecar()
        self.sc = sc
        if getattr(core, "_prov", None) is None:
            raise Missing("core could not import aiproviders")
        _CACHE_SEQ[0] += 1
        cache = os.path.join(TMPDIR, "ai_cache_%03d.json" % _CACHE_SEQ[0])

        def emit(kind, **f):
            self.ev.append((kind, dict(f)))
        self._p(sc, emit=emit, AI_CACHE=cache)
        self.cache = cache
        if not self.real_geo:
            self._p(core, geo_verdict=lambda *a, **k: (True, "DE", "ok"))
        vault = {"none": None, "ok": _FakeVault(_accounts()),
                 "raise": _RaisingVault(_accounts())}[self.vault]
        self._p(core, fetch_claude_usage=self._legacy("claude"),
                fetch_gpt_usage=self._legacy("codex"),
                ai_account_poll=self._acct_poll, _vault=vault)
        if self.nosleep:
            self._p(time, sleep=lambda s: None)
        self.latch = dict(core.AI_LATCH)
        core.AI_LATCH.update(blocked=False, reason="")
        self.state = dict(sc._state)
        sc._state.update(ip="203.0.113.5", ip2="203.0.113.5", code="DE")
        _clear_core_memory()
        return self

    def __exit__(self, *a):
        for p in reversed(self._patches):
            p.__exit__()
        core.AI_LATCH.clear()
        core.AI_LATCH.update(self.latch)
        self.sc._state.clear()
        self.sc._state.update(self.state)
        _clear_core_memory()
        return False

    def ai_events(self):
        return [f for kind, f in self.ev if kind == "ai"]

    def statuses(self):
        return [f.get("status") for f in self.ai_events() if f.get("status")]


class _OneShotStop(object):
    """A _STOP stand-in that lets ai_loop run exactly n iterations."""

    def __init__(self, n=1):
        self.n = n

    def is_set(self):
        self.n -= 1
        return self.n < 0

    def wait(self, *a):
        return False

    def set(self):
        self.n = -1


class _NoWaitEvent(object):
    def __init__(self):
        self.waits = []

    def wait(self, secs=None):
        self.waits.append(secs)
        return False

    def clear(self):
        pass

    def set(self):
        pass


# ---------------------------------------------------------------------------
# wave-4: aipolicy
# ---------------------------------------------------------------------------
def w_save_failure_reported_once():
    require(aipolicy, "record_error", "reset_for_tests")
    import contextlib
    bad = os.path.join(TMPDIR, "no_such_dir", "deeper", "state.json")
    aipolicy.reset_for_tests(bad)
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        for i in range(3):
            aipolicy.record_error("codex:sf%d" % i, "codex", now=T0 + i)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    leaked = FAKE_ACCESS in buf.getvalue() or FAKE_REFRESH in buf.getvalue()
    ok = len(lines) == 1 and not leaked
    return ok, "3 failed saves -> %d stderr line(s) (want exactly 1), secrets %s" % (
        len(lines), "LEAKED" if leaked else "absent")


def w_manual_floor_per_provider():
    require(aipolicy, "manual_floor")
    got = {p: aipolicy.manual_floor(p) for p in ("codex", "claude", "cursor", "chatgpt")}
    want = {"codex": 60, "claude": 120, "cursor": 120, "chatgpt": 60}
    bad = {p: got[p] for p in want if not near(got[p], want[p], 0.001)}
    return not bad, "manual_floor %r (want %r)" % (got, want)


def w_manual_claude_31s_refused():
    require(aipolicy, "record_success", "due")
    fresh()
    k = "claude:mf2"
    aipolicy.record_success(k, "claude", now=T0)
    a = aipolicy.due(k, "claude", now=T0 + 31, manual=True)
    b = aipolicy.due(k, "claude", now=T0 + 119, manual=True)
    c = aipolicy.due(k, "claude", now=T0 + 120.5, manual=True)
    ok = a[0] is False and b[0] is False and c[0] is True
    return ok, "claude manual +31 %r, +119 %r, +120.5 %r (want F, F, T)" % (a, b, c)


def w_jitter_never_below_floor():
    require(aipolicy, "record_success", "due")
    bad = []
    with env_set(AI_POLL_SECONDS_CLAUDE="120"):
        fresh()
        for i in range(150):
            for prov, floor in (("codex", 60.0), ("claude", 120.0)):
                k = "%s:jf%03d" % (prov, i)
                aipolicy.record_success(k, prov, now=T0)
                d = aipolicy.due(k, prov, now=T0 + floor - 0.1)
                if d[0] is not False:
                    bad.append(k)
    return not bad, ("300 schedules: none due before the provider floor"
                     if not bad else "%d due before the floor, e.g. %r" % (
                         len(bad), bad[:3]))


def w_claim_second_refused():
    require(aipolicy, "claim")
    fresh()
    k = "codex:cl"
    a = aipolicy.claim(k, "codex", now=T0, manual=True)
    b = aipolicy.claim(k, "codex", now=T0 + 1, manual=True)
    c = aipolicy.claim(k, "codex", now=T0 + 2)
    ok = (isinstance(a, tuple) and a[0] is True and b[0] is False
          and c[0] is False)
    return ok, "claim, manual claim +1 s, scheduled claim +2 s: %r %r %r" % (a, b, c)


def w_claim_threads_one_winner():
    require(aipolicy, "claim")
    fresh()
    k = "claude:race"
    go = threading.Event()
    wins = []

    def worker():
        go.wait(2)
        ok, _ = aipolicy.claim(k, "claude", manual=True)
        if ok:
            wins.append(1)
    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    go.set()
    for t in ts:
        t.join(5)
    return len(wins) == 1, "8 simultaneous manual claims -> %d granted (want 1)" % len(wins)


def w_claim_inflight_expires_120():
    require(aipolicy, "claim")
    fresh()
    k = "codex:exp"
    aipolicy.claim(k, "codex", now=T0)
    a = aipolicy.claim(k, "codex", now=T0 + 119)
    b = aipolicy.claim(k, "codex", now=T0 + 121)
    return a[0] is False and b[0] is True, \
        "unrecorded claim: +119 %r, +121 %r (want F, T)" % (a, b)


def w_due_stays_read_only():
    require(aipolicy, "claim", "due")
    fresh()
    k = "codex:ro"
    a = aipolicy.due(k, "codex", now=T0)
    b = aipolicy.due(k, "codex", now=T0)
    c = aipolicy.claim(k, "codex", now=T0)
    ok = a[0] is True and b[0] is True and c[0] is True
    return ok, "due, due, claim: %r %r %r (all True)" % (a, b, c)


def w_provider_hold_spans_keys():
    require(aipolicy, "provider_hold_left", "claim", "record_rate_limited", "due")
    fresh()
    aipolicy.record_rate_limited("claude:legacy", "claude", retry_after=600, now=T0)
    left = aipolicy.provider_hold_left("claude", now=T0 + 10)
    c = aipolicy.claim("claude:acct-x", "claude", now=T0 + 10, manual=True)
    d = aipolicy.due("claude:acct-x", "claude", now=T0 + 10)
    other = aipolicy.claim("codex:acct-y", "codex", now=T0 + 10)
    ok = (near(left, 590, 1.0) and c[0] is False and d[0] is False
          and other[0] is True)
    return ok, ("legacy 429 -> provider_hold_left %r; vault claude claim %r due %r;"
                " codex claim %r" % (left, c, d, other))


def w_provider_hold_vault_to_legacy():
    require(aipolicy, "provider_hold_left", "claim", "record_rate_limited")
    fresh()
    aipolicy.record_rate_limited("codex:acct-a", "codex", retry_after=500, now=T0)
    c = aipolicy.claim("codex:legacy", "codex", now=T0 + 5)
    c2 = aipolicy.claim("codex:acct-b", "codex", now=T0 + 5, manual=True)
    return c[0] is False and c2[0] is False, \
        "vault codex 429 -> legacy claim %r, second account claim %r" % (c, c2)


def w_offline_no_escalation():
    require(aipolicy, "record_offline", "hold_left", "due", "record_rate_limited")
    fresh()
    k = "codex:off"
    now = T0
    holds, nexts = [], []
    for _ in range(5):
        aipolicy.record_offline(k, "codex", now=now)
        holds.append(aipolicy.hold_left(k, now=now))
        early = aipolicy.due(k, "codex", now=now + 53)[0]
        late = aipolicy.due(k, "codex", now=now + 66.5)[0]
        nexts.append((early, late))
        now += 67
    rl = aipolicy.record_rate_limited(k, "codex", now=now)
    ok = (all(h == 0 for h in holds) and all(e is False and l is True for e, l in nexts)
          and near(rl, 120, 0.5))
    return ok, "5x offline: holds %r, (due@53, due@66.5) %r, first 429 then %r (want 0s, F/T, 120)" % (
        holds, nexts, rl)


def w_login_dead_hour_then_clear():
    require(aipolicy, "record_login_dead", "clear", "due", "hold_left")
    fresh()
    k = "claude:dead"
    h = aipolicy.record_login_dead(k, "claude", now=T0)
    a = aipolicy.due(k, "claude", now=T0 + 3599)
    m = aipolicy.due(k, "claude", now=T0 + 3599, manual=True)
    b = aipolicy.due(k, "claude", now=T0 + 3601)
    k2 = "claude:dead-cleared"
    aipolicy.record_login_dead(k2, "claude", now=T0)
    aipolicy.clear(k2)
    c = aipolicy.due(k2, "claude", now=T0 + 200)     # past interval + floor
    ok = (near(h, 3600, 0.5) and a[0] is False and m[0] is False
          and b[0] is True and c[0] is True)
    return ok, "hold %r; +3599 %r manual %r; +3601 %r; cleared, +200 s %r" % (
        h, a, m, b, c)


def w_forget_except():
    require(aipolicy, "forget_except", "record_success", "snapshot")
    fresh()
    for k in ("claude:legacy", "codex:legacy", "claude:a1", "codex:x1", "devin:p1"):
        aipolicy.record_success(k, k.split(":")[0], now=T0)
    aipolicy.forget_except({"claude:a1", "codex:x1"})
    left = sorted((aipolicy.snapshot().get("keys") or {}).keys())
    return left == ["claude:a1", "codex:x1"], "keys after forget_except: %r" % (left,)


def w_next_wake_keys():
    require(aipolicy, "next_wake", "record_success")
    fresh()
    aipolicy.record_success("claude:legacy", "claude", now=T0 - 1000)   # stale, overdue
    aipolicy.record_success("codex:x1", "codex", now=T0)
    try:
        only = aipolicy.next_wake(now=T0, keys=["codex:x1"])
        over = aipolicy.next_wake(now=T0, keys=["claude:legacy"])
    except TypeError:
        return False, "next_wake() does not accept keys="
    ok = 54 <= float(only) <= 66.5 and near(over, 5, 0.001)
    return ok, "keys=[active] -> %r (want ~60, not 5); keys=[overdue] -> %r (floor 5)" % (
        only, over)


def w_seed_hold():
    require(aipolicy, "seed_hold", "provider_hold_left", "claim")
    fresh()
    aipolicy.seed_hold("claude", T0 + 3000, now=T0)
    aipolicy.seed_hold("claude", T0 + 100, now=T0 + 1)     # must not shorten
    left = aipolicy.provider_hold_left("claude", now=T0 + 10)
    c = aipolicy.claim("claude:any", "claude", now=T0 + 10, manual=True)
    cx = aipolicy.claim("codex:any", "codex", now=T0 + 10)
    ok = near(left, 2990, 1.0) and c[0] is False and cx[0] is True
    return ok, "seeded 3000 s then 100 s: hold %r, claude claim %r, codex claim %r" % (
        left, c, cx)


def w_parse_retry_after_clamped():
    require(aipolicy, "parse_retry_after")
    now = 1_800_000_000.0
    far = "Fri, 31 Dec 9999 23:59:59 GMT"
    got = [aipolicy.parse_retry_after(v, now=now) for v in ("1e308", 10 ** 9, far)]
    ok = all(near(g, 21600, 0.001) for g in got)
    return ok, "'1e308', 1e9, year-9999 date -> %r (want 21600 each)" % (got,)


def w_rl_file_expanduser_and_vars():
    require(aipolicy, "reset_for_tests", "record_blocked")
    sub = os.path.join(TMPDIR, "rlvars")
    os.makedirs(sub, exist_ok=True)
    cwd = os.getcwd()
    os.chdir(TMPDIR)                       # a literal '~' would land here
    try:
        with env_var(AI_RL_FILE="~/tilde_rl.json"):
            aipolicy.reset_for_tests(None)
            aipolicy.record_blocked("claude:t", "claude", now=time.time())
        tilde_ok = os.path.isfile(os.path.join(FAKE_HOME, "tilde_rl.json"))
        literal = os.path.exists(os.path.join(TMPDIR, "~"))
        with env_var(NW_RL_TEST_DIR=sub, AI_RL_FILE="$NW_RL_TEST_DIR/var_rl.json"):
            aipolicy.reset_for_tests(None)
            aipolicy.record_blocked("claude:v", "claude", now=time.time())
        var_ok = os.path.isfile(os.path.join(sub, "var_rl.json"))
    finally:
        os.chdir(cwd)
        aipolicy.reset_for_tests(fresh_rl_path())
    return tilde_ok and var_ok and not literal, \
        "~ expanded into home: %s; $VAR expanded: %s; literal '~' dir created: %s" % (
            tilde_ok, var_ok, literal)


def w_rl_file_refuses_foreign_file():
    require(aipolicy, "reset_for_tests", "record_blocked", "hold_left")
    victims = {
        "cred.json": json.dumps({"claudeAiOauth": {"accessToken": FAKE_ACCESS,
                                                   "refreshToken": FAKE_REFRESH}}),
        "notes.txt": "not json at all\n",
        "list.json": "[1, 2, 3]",
    }
    bad = []
    for name, body in victims.items():
        p = os.path.join(TMPDIR, "victim_" + name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
        with env_var(AI_RL_FILE=p):
            aipolicy.reset_for_tests(None)
            aipolicy.record_blocked("claude:vic", "claude", now=time.time())
            held = aipolicy.hold_left("claude:vic")
        with open(p, "r", encoding="utf-8") as fh:
            after = fh.read()
        if after != body:
            bad.append("%s OVERWRITTEN" % name)
        if not held > 3000:
            bad.append("%s: hold not kept in memory (%r)" % (name, held))
    aipolicy.reset_for_tests(fresh_rl_path())
    return not bad, ("3 foreign files at AI_RL_FILE left byte-identical"
                     if not bad else "; ".join(bad))


def w_rl_file_accepts_own_file():
    require(aipolicy, "reset_for_tests", "record_blocked")
    p = os.path.join(TMPDIR, "own_rl.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"_v": 1, "version": 1, "keys": {}}, fh)
    with env_var(AI_RL_FILE=p):
        aipolicy.reset_for_tests(None)
        aipolicy.record_blocked("claude:own", "claude", now=time.time())
    aipolicy.reset_for_tests(fresh_rl_path())
    with open(p, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    ok = "claude:own" in (doc.get("keys") or {})
    return ok, "a file carrying our _v marker is used: %s" % ok


def w_saved_state_has_marker():
    require(aipolicy, "record_success")
    p = fresh()
    aipolicy.record_success("codex:mk", "codex", now=T0)
    with open(p, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc.get("_v") == 1, "saved _v = %r (want 1)" % (doc.get("_v"),)


def w_corrupt_file_no_burst():
    require(aipolicy, "reset_for_tests", "due", "claim")
    p = fresh_rl_path()
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("{definitely not json")
    aipolicy.reset_for_tests(p)
    t = time.time()
    a = aipolicy.due("claude:cx", "claude", now=t + 1)
    c = aipolicy.claim("codex:cx", "codex", now=t + 1)
    later = aipolicy.due("claude:cx", "claude", now=t + 200)
    folder, base = os.path.split(p)
    copies = [n for n in os.listdir(folder) if n.startswith(base) and "corrupt" in n]
    ok = a[0] is False and c[0] is False and later[0] is True and bool(copies)
    return ok, ("after a corrupt load: claude due +1 s %r, codex claim +1 s %r,"
                " claude due +200 s %r, .corrupt copy %r" % (a, c, later, copies))


# ---------------------------------------------------------------------------
# wave-4: core
# ---------------------------------------------------------------------------
class _geo_rig(object):
    """Real core.geo_verdict, with the Cloudflare trace stubbed to 'DE'."""

    def __enter__(self):
        need(core, "geo_verdict")
        self.calls = []
        self.saved = dict(core._geo)

        def trace(url, timeout=6):
            self.calls.append(url)
            return "DE"
        self.p = patched(core, _trace_cc=trace)
        self.p.__enter__()
        with core._geo_lock:
            core._geo["ts"] = 0.0
        if hasattr(core, "geo_invalidate"):
            core.geo_invalidate()
        return self

    def __exit__(self, *a):
        self.p.__exit__()
        core._geo.clear()
        core._geo.update(self.saved)
        return False


def w_geo_cached_ok_then_iranian_ip():
    with _geo_rig() as g:
        ip_ir = _iran_ip()
        first = core.geo_verdict("203.0.113.5", "DE")
        second = core.geo_verdict(ip_ir, "DE")
        third = core.geo_verdict("203.0.113.5", "IR")
    ok = first[0] is True and second[0] is False and third[0] is False
    return ok, "cached ok %r; then Iranian ip %r; then cc IR %r (want T, F, F)" % (
        first[:2], second[:2], third[:2])


def w_geo_cache_keyed_to_ip():
    require(core, "geo_invalidate")
    with _geo_rig() as g:
        core.geo_verdict("203.0.113.5", "DE")
        n1 = len(g.calls)
        core.geo_verdict("198.51.100.7", "DE")      # a different, non-Iranian ip
        n2 = len(g.calls)
        core.geo_invalidate()
        core.geo_verdict("198.51.100.7", "DE")
        n3 = len(g.calls)
    ok = n1 >= 1 and n2 > n1 and n3 > n2
    return ok, "trace calls: first %d, after ip change %d, after geo_invalidate %d" % (
        n1, n2, n3)


def w_record_outcome_non_str_err():
    need(core, "_ai_record_outcome")
    require(aipolicy, "due")
    bad = []
    for i, res in enumerate(({"error": {"message": "nested"}}, {"error": 503},
                             {"err": ["a", "b"]}, {"error": b"bytes"})):
        fresh()
        k = "codex:m3_%d" % i
        try:
            core._ai_record_outcome(k, "codex", res)
        except Exception as e:
            bad.append("%r raised %s" % (res, type(e).__name__))
            continue
        if aipolicy.due(k, "codex")[0] is not False:
            bad.append("%r left the key immediately due" % (res,))
    return not bad, ("4 non-str errors recorded, none raise" if not bad
                     else "; ".join(bad))


def w_record_outcome_offline_no_hold():
    need(core, "_ai_record_outcome")
    fresh()
    k = "claude:off"
    for _ in range(3):
        core._ai_record_outcome(k, "claude", {"err": "http 0", "offline": True})
    held = aipolicy.hold_left(k)
    d = aipolicy.due(k, "claude")
    kind = ((aipolicy.snapshot().get("keys") or {}).get(k) or {}).get("kind")
    ok = held == 0 and d[0] is False and kind != "rate_limited"
    return ok, "3 offline outcomes -> hold %r, due %r, kind %r (want 0, not due, not rate_limited)" % (
        held, d, kind)


def w_record_outcome_login_dead():
    need(core, "_ai_record_outcome")
    require(aipolicy, "clear")
    fresh()
    k = "codex:dead"
    core._ai_record_outcome(k, "codex", {"error": "LOGIN_EXPIRED:codex",
                                         "needs_login": True})
    h1 = aipolicy.hold_left(k)
    fresh()
    k2 = "claude:dead2"
    core._ai_record_outcome(k2, "claude", {"err": "LOGIN_EXPIRED:claude"})
    h2 = aipolicy.hold_left(k2)
    core._ai_record_outcome(k2, "claude", {"error": None, "status": "ok"})
    h3 = aipolicy.hold_left(k2)
    ok = 3500 <= h1 <= 3600 and 3500 <= h2 <= 3600 and h3 == 0
    return ok, "needs_login hold %.0f, LOGIN_EXPIRED hold %.0f, after success %r (want ~3600, ~3600, 0)" % (
        h1, h2, h3)


def w_core_legacy_hold_blocks_vault():
    def run():
        reset_transport()
        _clear_core_memory()
        fresh()
        aipolicy.record_rate_limited("claude:legacy", "claude", retry_after=600)
        route(CLAUDE_URL_PART, 200, CLAUDE_OK)
        route(CODEX_URL_PART, 200, CODEX_OK)
        core.ai_poll_accounts_due(manual=False, spacing=0)
        cl = FETCHES.count(CLAUDE_URL_PART)
        cx = FETCHES.count(CODEX_URL_PART)
        return cl == 0 and cx == 1, \
            "claude:legacy held -> vault claude requests %d, codex %d (want 0, 1)" % (cl, cx)
    return _with_vault(run)


def w_core_vault_hold_blocks_legacy():
    need(core, "ai_poll_legacy")
    fresh()
    _clear_core_memory()
    aipolicy.record_rate_limited("claude:acct-claude-1", "claude", retry_after=600)
    calls = []

    def cl():
        calls.append("claude")
        return {"session_pct": 1.0, "err": None}

    def cx():
        calls.append("codex")
        return {"sess_pct": 1.0, "err": None}
    with patched(core, fetch_claude_usage=cl, fetch_gpt_usage=cx):
        core.ai_poll_legacy(manual=True, spacing=0)
    return calls == ["codex"], "vault claude held -> legacy requests %r (want ['codex'])" % (calls,)


def w_core_held_card_last_good_vault():
    def run():
        reset_transport()
        _clear_core_memory()
        fresh()
        route(CLAUDE_URL_PART, 200, CLAUDE_OK)
        route(CODEX_URL_PART, 200, CODEX_OK)
        core.ai_poll_accounts_due(manual=False, spacing=0)
        fresh()                                   # make both due again
        reset_transport()
        route(CLAUDE_URL_PART, 429, {"error": {"type": "rate_limit_error"}})
        route(CODEX_URL_PART, 200, CODEX_OK)
        rows, _ = _split(core.ai_poll_accounts_due(manual=False, spacing=0))
        r = _row(rows, "acct-claude-1") or {}
        ok = (carries_number(r.get("usage"), 42.0) and r.get("stale") is True
              and bool(r.get("held")))
        return ok, "429'd claude row: usage keeps 42%%: %s, stale=%r, held=%r" % (
            carries_number(r.get("usage"), 42.0), r.get("stale"), r.get("held"))
    return _with_vault(run)


def w_core_held_card_last_good_legacy():
    need(core, "ai_poll_legacy")
    fresh()
    _clear_core_memory()
    seq = [{"session_pct": 42.0, "week_pct": 11.0, "err": None},
           {"err": "rate limited", "rate_limited": True, "retry_after": None}]

    def cl():
        return dict(seq.pop(0)) if seq else {"err": "unexpected"}

    def cx():
        return {"sess_pct": 17.0, "week_pct": 3.0, "err": None}
    with patched(core, fetch_claude_usage=cl, fetch_gpt_usage=cx):
        core.ai_poll_legacy(manual=False, spacing=0)
        fresh()
        claude, _, _ = core.ai_poll_legacy(manual=False, spacing=0)
    ok = (near(claude.get("session_pct"), 42.0, 0.01) and claude.get("stale") is True
          and bool(claude.get("held")))
    return ok, "legacy claude after 429: session_pct=%r stale=%r held=%r (want 42, True, >0)" % (
        claude.get("session_pct"), claude.get("stale"), claude.get("held"))


def w_core_prunes_stale_keys():
    require(core, "ai_active_keys")
    require(aipolicy, "next_wake")

    def run():
        reset_transport()
        _clear_core_memory()
        fresh()
        now = time.time()
        aipolicy.record_success("claude:legacy", "claude", now=now - 5000)
        aipolicy.record_success("codex:legacy", "codex", now=now - 5000)
        aipolicy.record_success("claude:removed-acct", "claude", now=now - 5000)
        route(CLAUDE_URL_PART, 200, CLAUDE_OK)
        route(CODEX_URL_PART, 200, CODEX_OK)
        core.ai_poll_accounts_due(manual=False, spacing=0)
        active = set(core.ai_active_keys() or ())
        left = set((aipolicy.snapshot().get("keys") or {}).keys())
        wake = aipolicy.next_wake(keys=sorted(active))
        want = {"claude:acct-claude-1", "codex:acct-codex-1"}
        ok = active == want and left == want and float(wake) > 30
        return ok, "active %r; stored keys %r; next_wake(active) %.1f s (want > 30, not 5)" % (
            sorted(active), sorted(left), float(wake))
    return _with_vault(run)


def w_core_token_429_is_rate_limited():
    def run():
        reset_transport()
        _clear_core_memory()
        fresh()
        aipolicy.record_blocked(aipolicy.key("codex", "acct-codex-1"), "codex")
        expired = claude_cred()
        expired["claudeAiOauth"]["expiresAt"] = int((time.time() - 3600) * 1000)
        accts = [dict(ACCTS[0], cred=expired), dict(ACCTS[1], cred=codex_cred())]
        route("platform.claude.com/v1/oauth/token", 429,
              {"error": "rate_limited"}, headers={"Retry-After": "900"})
        route(CLAUDE_URL_PART, 500, {"error": "must not be requested"})
        with patched(core, _vault=_FakeVault(accts)):
            core.ai_poll_accounts_due(manual=False, spacing=0)
        k = aipolicy.key("claude", "acct-claude-1")
        ent = (aipolicy.snapshot().get("keys") or {}).get(k) or {}
        left = aipolicy.hold_left(k)
        tok = FETCHES.count("platform.claude.com/v1/oauth/token")
        ok = tok == 1 and ent.get("kind") == "rate_limited" and left >= 299
        return ok, "token endpoint 429: token requests %d, kind %r, hold %.0f s (want 1, rate_limited, >=300)" % (
            tok, ent.get("kind"), left)
    return _with_vault(run)


# ---------------------------------------------------------------------------
# wave-4: sidecar
# ---------------------------------------------------------------------------
def w_sc_geo_cached_then_iran_zero_requests():
    with _geo_rig() as g, Rig(vault="none", real_geo=True) as r:
        ip_ir = _iran_ip()
        fresh()
        primed = core.geo_verdict("203.0.113.5", "DE")
        r.sc._state.update(ip=ip_ir, code="DE")
        r.sc.ai_tick(0.0, False)
        n = len(r.requests)
        off = any(f.get("off") for f in r.ai_events())
    ok = primed[0] is True and n == 0 and off
    return ok, "cached ok, then ip %s: requests %d, ai off emitted %s (want 0, True)" % (
        "<IR range>", n, off)


def w_sc_ip_change_invalidates_geo():
    sc = get_sidecar()
    require(core, "geo_invalidate")
    hits = []
    with Rig(vault="none") as r:
        with patched(core, geo_invalidate=lambda *a, **k: hits.append(1),
                     fetch_ip=lambda *a, **k: {"ip": "198.51.100.7", "cc": "DE", "isp": ""},
                     fetch_rf=lambda *a, **k: "198.51.100.7",
                     ping=lambda *a, **k: ("10ms", 0),
                     local_ip=lambda *a, **k: "10.0.0.2",
                     gateway=lambda *a, **k: "10.0.0.1",
                     log=lambda *a, **k: None, toast=lambda *a, **k: None), \
                patched(sc, checks_once=lambda *a, **k: None):
            sc._state.update(ip="203.0.113.5", checks_ip="203.0.113.5")
            sc.refresh_once()
    return len(hits) >= 1, "public ip changed -> core.geo_invalidate() called %d time(s)" % len(hits)


def w_sc_vault_error_no_legacy_fallback():
    with Rig(vault="raise") as r:
        fresh()
        r.sc.ai_tick(0.0, False)
        reqs = list(r.requests)
        errs = [f for k, f in r.ev if k == "error"] + \
               [f for f in r.ai_events() if f.get("err") or f.get("status") == "wait"]
        spinning = r.statuses()[-1:] == ["polling"]
    ok = not reqs and bool(errs) and not spinning
    return ok, "vault unreadable: requests %r, error/wait emitted %s, left spinning %s" % (
        reqs, bool(errs), spinning)


def w_sc_no_vault_uses_legacy():
    with Rig(vault="none") as r:
        fresh()
        r.sc.ai_tick(0.0, False)
        reqs = sorted(r.requests)
    return reqs == ["claude:legacy", "codex:legacy"], \
        "no vault -> legacy requests %r" % (reqs,)


def w_sc_retry_until_migrated():
    require(aipolicy, "provider_hold_left")
    with Rig(vault="none") as r:
        fresh()
        now = time.time()
        with open(r.cache, "w", encoding="utf-8") as fh:
            json.dump({"tried": now - 120, "retry_until": now + 3000}, fh)
        with patched(r.sc, _STOP=_OneShotStop(2), _ai_now=_NoWaitEvent()):
            r.sc.ai_loop()
        reqs = list(r.requests)
        hc = aipolicy.provider_hold_left("claude")
        hx = aipolicy.provider_hold_left("codex")
    ok = not reqs and 2900 <= hc <= 3000 and 2900 <= hx <= 3000
    return ok, "old retry_until +3000 s: requests %r, claude hold %.0f, codex hold %.0f" % (
        reqs, hc, hx)


def w_sc_no_polling_for_stale_keys():
    with Rig(vault="ok") as r:
        fresh()
        now = time.time()
        aipolicy.record_success("claude:acct-claude-1", "claude", now=now)
        aipolicy.record_success("codex:acct-codex-1", "codex", now=now)
        aipolicy.record_success("claude:legacy", "claude", now=now - 5000)  # stale
        r.sc.ai_tick(0.0, False)
        st = r.statuses()
        reqs = list(r.requests)
    return "polling" not in st and not reqs, \
        "nothing due except a stale key: statuses %r, requests %r" % (st, reqs)


def w_sc_no_polling_when_all_held():
    with Rig(vault="ok") as r:
        fresh()
        for a in ACCTS:
            aipolicy.record_rate_limited(aipolicy.key(a["provider"], a["id"]),
                                         a["provider"], retry_after=900)
        r.sc.ai_tick(0.0, True)
        st = r.statuses()
        reqs = list(r.requests)
    ok = "polling" not in st and not reqs and "wait" in st
    return ok, "manual press, all held: statuses %r, requests %r (want wait only)" % (st, reqs)


def w_sc_polling_precedes_request():
    with Rig(vault="ok") as r:
        fresh()
        r.polling_seen_at_request = None
        r.sc.ai_tick(0.0, False)
        seen = r.polling_seen_at_request
        n = len(r.requests)
    return n >= 1 and bool(seen), \
        "requests %d; 'polling' emitted before the first request: %s" % (n, bool(seen))


def w_sc_crash_emits_wait():
    sc = get_sidecar()

    def boom(*a, **k):
        raise RuntimeError("stub tick failure")
    with Rig(vault="none") as r:
        fresh()
        with patched(sc, ai_tick=boom, _STOP=_OneShotStop(1), _ai_now=_NoWaitEvent()):
            sc.ai_loop()
        kinds = [k for k, _ in r.ev]
        waits = [f for f in r.ai_events() if f.get("status") == "wait"]
    crash = getattr(sc, "AI_CRASH_WAIT", None)
    ok = "error" in kinds and bool(waits) and near(waits[-1].get("seconds"), crash, 1)
    return ok, "after a tick crash: events %r, wait seconds %r (want %r)" % (
        kinds, [w.get("seconds") for w in waits], crash)


def w_sc_double_refresh_one_request():
    with Rig(vault="ok", nosleep=False) as r:
        fresh()
        r.poll_delay = 0.3
        go = threading.Event()

        def press():
            go.wait(2)
            r.sc.ai_account_refresh("acct-claude-1")
        ts = [threading.Thread(target=press) for _ in range(2)]
        for t in ts:
            t.start()
        go.set()
        for t in ts:
            t.join(10)
        n = r.requests.count("claude:acct-claude-1")
    return n == 1, "two simultaneous per-account refreshes -> %d request(s) (want 1)" % n


def w_sc_refresh_respects_provider_hold():
    with Rig(vault="ok") as r:
        fresh()
        aipolicy.record_rate_limited("claude:legacy", "claude", retry_after=600)
        r.sc.ai_account_refresh("acct-claude-1")
        n = len(r.requests)
    return n == 0, "claude:legacy held -> per-account claude refresh requests %d (want 0)" % n


def w_login_dead_hold_offers_sign_in():
    """A dead-login hold must show the sign-in fault, never "rate limited"."""
    require(aipolicy, "record_login_dead")
    with Rig(vault="ok") as r:
        fresh()
        aipolicy.record_login_dead("claude:acct-claude-1", "claude")
        rows, requested = _with_expiry(
            -60, lambda: core.ai_poll_accounts_due(manual=True))
        row = next((x for x in rows if x.get("id") == "acct-claude-1"), {})
        _with_expiry(-60, lambda: r.sc.ai_account_refresh("acct-claude-1"))
        errs = [f.get("err") for k, f in r.ev if k == "ai_accounts"]
        n = sum(1 for q in r.requests if q.startswith("claude:"))
    err = str(row.get("error") or "")
    ok = (row.get("needs_login") is True and err.startswith("LOGIN_EXPIRED")
          and any(str(e or "").startswith("LOGIN_EXPIRED") for e in errs)
          and n == 0)
    return ok, "row needs_login=%r error=%r; press emits %r; claude requests %d (want True, LOGIN_EXPIRED, 0)" % (
        row.get("needs_login"), err, errs, n)


def _with_expiry(offset, fn):
    import time as _t
    orig = core._prov.cred_expiry
    core._prov.cred_expiry = lambda pid, cred: _t.time() + offset
    try:
        return fn()
    finally:
        core._prov.cred_expiry = orig


def w_login_dead_lifted_by_valid_cred():
    """A valid credential lifts a dead-login hold on the next press, even
    with no fingerprint baseline (fresh process)."""
    require(aipolicy, "record_login_dead", "hold_left")
    with Rig(vault="ok") as r:
        fresh()
        k = "claude:acct-claude-1"
        import time as _t
        aipolicy.record_login_dead(k, "claude", now=_t.time() - 600)
        core._AI_DEAD_FP.clear()
        _with_expiry(3600, lambda: r.sc.ai_account_refresh("acct-claude-1"))
        n = sum(1 for q in r.requests if q == k)
    return n == 1, "after re-login: claude requests %d (want 1)" % n


def w_login_dead_lifted_by_loop():
    """The loop lifts it too, without a press."""
    require(aipolicy, "record_login_dead", "hold_left")
    with Rig(vault="ok") as r:
        fresh()
        k = "claude:acct-claude-1"
        aipolicy.record_login_dead(k, "claude")
        core._AI_DEAD_FP.clear()
        _with_expiry(3600, core.ai_lift_relogged)
        left = aipolicy.hold_left(k)
    return left == 0, "hold left after loop lift %r (want 0)" % left


def w_login_dead_kept_for_expired_cred():
    require(aipolicy, "record_login_dead", "hold_left")
    with Rig(vault="ok") as r:
        fresh()
        k = "claude:acct-claude-1"
        aipolicy.record_login_dead(k, "claude")
        _with_expiry(-60, lambda: r.sc.ai_account_refresh("acct-claude-1"))
        _with_expiry(-60, core.ai_lift_relogged)
        n = sum(1 for q in r.requests if q == k)
        left = aipolicy.hold_left(k)
    return n == 0 and left > 3000, "still expired: requests %d, hold left %r (want 0, ~3600)" % (n, left)


# ---------------------------------------------------------------------------
# final safety
# ---------------------------------------------------------------------------
def s98_real_state_file_untouched():
    after = _stat_sig(REAL_RL_FILE)
    return after == REAL_RL_BEFORE, (
        "per-user state file unchanged (exists=%s)" % after[0]
        if after == REAL_RL_BEFORE else "per-user state file CHANGED")


def s99_no_network_attempted():
    return not NETWORK_ATTEMPTS, (
        "0 real outbound attempts" if not NETWORK_ATTEMPTS
        else "%d attempt(s): %s" % (len(NETWORK_ATTEMPTS),
                                    ", ".join(NETWORK_ATTEMPTS[:5])))


CL, CX = CLAUDE_URL_PART, CODEX_URL_PART

TESTS = [
    ("01 interval defaults: codex 60, claude 180, others 300",
     t_default_intervals),
    ("02 interval: chatgpt/openai alias to codex", t_alias_to_codex),
    ("03 interval: per-provider env overrides", t_env_overrides),
    ("04 interval: AI_POLL_SECONDS does not leak to claude/others",
     t_env_no_crosstalk),
    ("05 interval: env read at call time", t_env_read_at_call_time),
    ("06 interval: floor (claude 120, codex 60, other 120) and 3600 ceiling",
     t_env_floor_and_ceiling_clamp),
    ("07 interval: NaN/inf/garbage/empty env -> default",
     t_env_garbage_falls_back),
    ("08 interval: optional env= mapping is honoured", t_env_mapping_argument),
    ("09 interval: junk provider ids never raise",
     t_interval_junk_provider_never_raises),

    ("10 retry-after: seconds as int/float/str/bytes", t_retry_after_seconds),
    ("11 retry-after: HTTP-date relative to now", t_retry_after_http_date),
    ("12 retry-after: 0, negative and past dates -> None",
     t_retry_after_zero_negative_past),
    ("13 retry-after: garbage -> None, never raises", t_retry_after_garbage),
    ("14 key: provider:id, missing id -> legacy", t_key_shape),

    ("15 due: unknown key is due", t_unknown_key_due),
    ("16 429 backoff codex 120/240/480/900 cap", t_rl_backoff_codex),
    ("17 429 backoff claude 300/600/1200/1800 cap", t_rl_backoff_claude),
    ("18 429 backoff other providers 300/600/900 cap", t_rl_backoff_other),
    ("19 429 backoff: 80 strikes stay finite and capped",
     t_rl_many_strikes_no_overflow),
    ("20 429: a longer server Retry-After is honoured above the cap",
     t_rl_retry_after_longer_is_honoured),
    ("21 429: server Retry-After clamped to 6 h", t_rl_retry_after_clamped_6h),
    ("22 429: short/0/missing Retry-After never shortens our backoff",
     t_rl_retry_after_short_or_zero_does_not_shorten),
    ("23 never-shorten: 1 h block survives a later 429 and error",
     t_never_shorten_blocked_then_429),
    ("24 never-shorten: long hold survives a later short 429",
     t_never_shorten_long_429_then_short_429),
    ("25 held key is not due until the hold ends", t_held_key_not_due_scheduled),
    ("26 manual refresh cannot bypass a 429 hold", t_manual_cannot_bypass_hold),
    ("27 manual refresh cannot bypass a 403-challenge hold",
     t_manual_cannot_bypass_block),
    ("28 manual floor: codex max(30 s, floor 60 s) = 60 s", t_manual_floor_30s),
    ("29 manual floor: claude max(30 s, floor 120 s) = 120 s",
     t_manual_floor_applies_to_claude_too),
    ("30 scheduled codex poll lands inside 60 s +-10%",
     t_scheduled_due_inside_jitter_band),
    ("31 scheduled claude poll lands inside 180 s +-10%",
     t_scheduled_claude_respects_180),
    ("32 success clears the hold and resets strikes",
     t_success_clears_hold_and_strikes),
    ("33 403 challenge holds 3600 s and counts a strike",
     t_blocked_hold_and_strike),
    ("34 error backoff interval*2^n capped at 900", t_error_backoff),
    ("35 next_wake: 0 with no keys, hold-aware, floor 5", t_next_wake),

    ("36 persistence: a hold survives a restart", t_hold_survives_restart),
    ("37 persistence: strikes survive a restart", t_strikes_survive_restart),
    ("38 persistence: the schedule survives a restart",
     t_schedule_survives_restart),
    ("39 persistence: AI_RL_FILE is the store when no override",
     t_env_path_used_when_no_override),
    ("40 persistence: corrupt files load with no hold, never raise",
     t_corrupt_file_is_empty_state),
    ("41 persistence: a corrupt file is replaced by valid JSON on write",
     t_corrupt_file_is_repaired_on_write),
    ("42 persistence: atomic writes leave no temp files",
     t_atomic_write_leaves_no_temp_files),
    ("43 snapshot is JSON-safe", t_snapshot_json_safe),

    ("44 claude adapter: 429 + Retry-After 120 -> rate_limited, 120",
     _flag_429("claude", CL, claude_cred)),
    ("45 codex adapter: 429 + Retry-After 120 -> rate_limited, 120",
     _flag_429("codex", CX, codex_cred)),
    ("46 claude adapter: 429 without header -> retry_after None",
     _flag_429_no_header("claude", CL, claude_cred)),
    ("47 codex adapter: 429 without header -> retry_after None",
     _flag_429_no_header("codex", CX, codex_cred)),
    ("48 claude adapter: 429 error string unchanged",
     _flag_429_keeps_error_string("claude", CL, claude_cred)),
    ("49 codex adapter: 429 error string unchanged",
     _flag_429_keeps_error_string("codex", CX, codex_cred)),
    ("50 claude adapter: 403 HTML -> blocked_html",
     _flag_403_html("claude", CL, claude_cred)),
    ("51 codex adapter: 403 Cloudflare HTML -> blocked_html",
     _flag_403_html("codex", CX, codex_cred)),
    ("52 codex adapter: 403 JSON is not blocked_html",
     _flag_403_json_not_html("codex", CX, codex_cred)),
    ("53 claude adapter: status 0 -> offline",
     _flag_offline("claude", CL, claude_cred)),
    ("54 codex adapter: status 0 -> offline",
     _flag_offline("codex", CX, codex_cred)),
    ("55 claude adapter: 200 carries no scheduler flags",
     _flag_ok_carries_no_flags("claude", CL, claude_cred, CLAUDE_OK)),
    ("56 codex adapter: 200 carries no scheduler flags",
     _flag_ok_carries_no_flags("codex", CX, codex_cred, CODEX_OK)),

    ("57 core due-poll: first tick requests both accounts",
     t_core_first_poll_requests_both),
    ("58 core due-poll: a held account is NOT requested",
     t_core_held_account_not_requested),
    ("59 core due-poll: another provider is still requested",
     t_core_other_provider_still_requested),
    ("60 core due-poll: held account's row carries held>0",
     t_core_held_row_returned_with_held),
    ("61 core due-poll: held account's row is its last reading",
     t_core_held_row_is_last_reading),
    ("62 core due-poll: the polled account's row is fresh, not held",
     t_core_codex_row_fresh_and_not_held),
    ("63 core due-poll: manual does not bypass the hold",
     t_core_manual_does_not_bypass_hold),
    ("64 core due-poll: everything held -> zero requests, requested False",
     t_core_all_held_requests_nothing),
    ("65 core due-poll: a 429 Retry-After becomes a persisted hold",
     t_core_429_records_server_hold),
    ("66 core due-poll: a 403 HTML becomes a 1 h hold",
     t_core_403_html_records_block_hold),
    ("67 core due-poll: success schedules the next poll (no re-request)",
     t_core_success_schedules_next_poll),
    ("68 core: ai_poll_accounts signature kept for back-compat",
     t_core_legacy_signature_kept),

    ("71 aipolicy: a failed save is reported once on stderr, no secrets",
     w_save_failure_reported_once),
    ("72 aipolicy: manual_floor = max(30, provider floor)", w_manual_floor_per_provider),
    ("73 aipolicy: claude manual refresh at +31 s is refused", w_manual_claude_31s_refused),
    ("74 aipolicy: jitter never schedules below the provider floor",
     w_jitter_never_below_floor),
    ("75 aipolicy: claim -- a second claim before record_* is refused",
     w_claim_second_refused),
    ("76 aipolicy: claim -- 8 racing threads, exactly one wins", w_claim_threads_one_winner),
    ("77 aipolicy: claim -- an unrecorded claim expires after 120 s",
     w_claim_inflight_expires_120),
    ("78 aipolicy: due() stays read-only", w_due_stays_read_only),
    ("79 aipolicy: a legacy 429 holds every key of that provider",
     w_provider_hold_spans_keys),
    ("80 aipolicy: a vault 429 holds legacy and other accounts",
     w_provider_hold_vault_to_legacy),
    ("81 aipolicy: offline -> no hold, no strikes, next at interval",
     w_offline_no_escalation),
    ("82 aipolicy: dead login holds 1 h (manual too), clear() lifts it",
     w_login_dead_hour_then_clear),
    ("83 aipolicy: forget_except drops every other key", w_forget_except),
    ("84 aipolicy: next_wake(keys=) ignores stale overdue keys", w_next_wake_keys),
    ("85 aipolicy: seed_hold imports a provider hold, never shortens", w_seed_hold),
    ("86 aipolicy: parse_retry_after clamped to 6 h", w_parse_retry_after_clamped),
    ("87 aipolicy: AI_RL_FILE expands ~ and $VARS", w_rl_file_expanduser_and_vars),
    ("88 aipolicy: AI_RL_FILE never overwrites a foreign file",
     w_rl_file_refuses_foreign_file),
    ("89 aipolicy: AI_RL_FILE with our _v marker is used", w_rl_file_accepts_own_file),
    ("90 aipolicy: saved state carries _v: 1", w_saved_state_has_marker),
    ("91 aipolicy: corrupt file -> .corrupt copy and no burst", w_corrupt_file_no_burst),

    ("92 core geo: cached ok, then Iranian ip/cc -> fail closed",
     w_geo_cached_ok_then_iranian_ip),
    ("93 core geo: cache keyed to ip; geo_invalidate() forces a check",
     w_geo_cache_keyed_to_ip),
    ("94 core: _ai_record_outcome with a non-str error never raises",
     w_record_outcome_non_str_err),
    ("95 core: offline outcome creates no hold", w_record_outcome_offline_no_hold),
    ("96 core: dead login -> 1 h hold, success clears it", w_record_outcome_login_dead),
    ("97 core: claude:legacy hold stops the vault claude request",
     w_core_legacy_hold_blocks_vault),
    ("98 core: vault claude hold stops the legacy claude request",
     w_core_vault_hold_blocks_legacy),
    ("99 core: a 429'd vault card keeps its last good reading (stale)",
     w_core_held_card_last_good_vault),
    ("100 core: a 429'd legacy card keeps its last good reading (stale)",
     w_core_held_card_last_good_legacy),
    ("101 core: stale keys pruned; ai_active_keys; next_wake not stuck at 5",
     w_core_prunes_stale_keys),
    ("102 core: token-endpoint 429 is a rate limit", w_core_token_429_is_rate_limited),

    ("103 sidecar: cached geo ok, ip turns Iranian -> zero requests",
     w_sc_geo_cached_then_iran_zero_requests),
    ("104 sidecar: a public ip change calls core.geo_invalidate()",
     w_sc_ip_change_invalidates_geo),
    ("105 sidecar: unreadable vault -> no legacy fallback, error + wait",
     w_sc_vault_error_no_legacy_fallback),
    ("106 sidecar: no vault -> the legacy path still polls", w_sc_no_vault_uses_legacy),
    ("107 sidecar: old retry_until is seeded as a hold before the first tick",
     w_sc_retry_until_migrated),
    ("108 sidecar: no 'polling' emit when only a stale key is due",
     w_sc_no_polling_for_stale_keys),
    ("109 sidecar: no 'polling' emit on a manual press while all held",
     w_sc_no_polling_when_all_held),
    ("110 sidecar: 'polling' is emitted before a real request",
     w_sc_polling_precedes_request),
    ("111 sidecar: a crashed tick emits wait (spinner stops)", w_sc_crash_emits_wait),
    ("112 sidecar: double per-account refresh -> one request",
     w_sc_double_refresh_one_request),
    ("113 sidecar: per-account refresh respects the provider hold",
     w_sc_refresh_respects_provider_hold),
    ("114 dead-login hold offers sign-in, not 'rate limited'",
     w_login_dead_hold_offers_sign_in),
    ("115 re-login (valid cred) lifts the dead-login hold on press",
     w_login_dead_lifted_by_valid_cred),
    ("116 re-login lifts the dead-login hold from the loop",
     w_login_dead_lifted_by_loop),
    ("117 expired cred keeps the dead-login hold, zero requests",
     w_login_dead_kept_for_expired_cred),

    ("69 safety: real per-user state file untouched",
     s98_real_state_file_untouched),
    ("70 safety: zero real outbound attempts for the whole run",
     s99_no_network_attempted),
]


def main():
    print("net-watch-widget :: AI poll policy regression harness")
    print("python %s" % sys.version.split()[0])
    print("sandbox: <temp>/%s" % os.path.basename(TMPDIR))
    print("")

    missing = sorted(REQUIRED_LAYERS - set(TRIPWIRE_ARMED))
    if missing:
        print("ABORT: the network kill-switch is NOT armed at %r." % missing)
        print("       Refusing to run: a real request would reach a vendor "
              "with the user's own account.")
        cleanup()
        return 2
    print("safety: kill-switch armed and PROBED -- %s all refuse"
          % ", ".join(TRIPWIRE_ARMED))
    ok, detail = s02_paths_sandboxed()
    if not ok:
        print("ABORT: %s" % detail)
        cleanup()
        return 2
    print("safety: %s" % detail)
    if CORE_STUBS:
        print("note: off Windows, inert stand-ins for %s let core import"
              % ", ".join(CORE_STUBS))
    for name, err in sorted(IMPORT_ERRORS.items()):
        print("import: %s failed -- %s" % (name, err))
    print("")

    for name, fn in TESTS:
        check(name, fn)

    npass = sum(1 for s, _, _ in RESULTS if s == "PASS")
    nfail = sum(1 for s, _, _ in RESULTS if s == "FAIL")
    nskip = sum(1 for s, _, _ in RESULTS if s == "SKIP")
    print("")
    print("-" * 66)
    print("%d passed, %d failed, %d skipped, %d total"
          % (npass, nfail, nskip, len(RESULTS)))
    if nfail:
        print("")
        print("failures:")
        for s, n, d in RESULTS:
            if s == "FAIL":
                print("  - %s :: %s" % (n, d))
    if nskip:
        print("")
        print("skipped:")
        for s, n, d in RESULTS:
            if s == "SKIP":
                print("  - %s :: %s" % (n, d))
    print("-" * 66)
    return 1 if nfail else 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:
        traceback.print_exc()
        rc = 3
    finally:
        cleanup()
    sys.exit(rc)
