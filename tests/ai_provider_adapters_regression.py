#!/usr/bin/env python3
"""Standalone regression harness for the per-vendor AI usage adapters.

Runs with a bare interpreter: standard library only, no pytest.

    python tests/ai_provider_adapters_regression.py

Scope
-----
aiproviders.py only, and specifically the six adapters promoted from
`planned` to `live` after claude/codex: cursor, copilot, windsurf, devin,
kimi, cline. The vault (aiaccounts.py) is out of scope -- it is covered by
tests/ai_accounts_regression.py -- and nothing here writes to it.

Why the harness is paranoid
---------------------------
  * Every vendor here geo-blocks the user's country. A real outbound request
    from a test run does not merely error, it puts the user's actual account
    in front of a WAF. The network is therefore killed at four layers
    (urlopen, OpenerDirector.open, socket.create_connection,
    socket.socket.connect), the kill-switch is PROBED at startup to prove it
    fires, and the run asserts at the end that zero outbound attempts were
    made. Both facts are reported as ordinary test lines, so a reader can see
    the safety held rather than trust a comment.
  * No token value, fake or otherwise, is ever printed. Fake secrets are
    obvious placeholders (FAKE-...-DO-NOT-USE) that cannot be mistaken for
    real credentials.
  * Every environment variable that could resolve to a real credential path
    is redirected into a throwaway temp directory BEFORE the module under
    test is imported, and the redirection is asserted. HOME/USERPROFILE go
    too, not just the feature's own variable: a helper in this codebase
    APPENDS the per-user default path to whatever its variable specifies, so
    redirecting one variable is not sufficient.

Any of those three failing aborts the run before a single adapter is called.

Test-design rules
-----------------
Tests are written against the CONTRACT, not against whatever the
implementation happens to do. A disagreement is a finding, not a reason to
soften the test. Three traps are worth naming, because a wrong implementation
still produces a plausible number and only a contract-driven test catches it:

  * Windsurf/Devin speak proto3 JSON, which OMITS zero values. A response
    carrying a reset timestamp but NO percentage field means one hundred
    percent consumed, not zero.
  * Those same percentages are REMAINING, not used. "20" means 80% used.
  * Copilot free-tier accounts use a completely different body. A paid-shape
    reader finds nothing and renders zeros, which looks like real data and is
    worse than an error.

Response fixtures use the vendors' own field spellings, because the wire
format is a fact about the vendor rather than a choice of this codebase. The
VALUE assertions are the contractual part and are independent of spelling.

Set NW_AIPROVIDERS_DIR to import aiproviders.py from another directory. That
exists so the suite can be proven capable of failing by running it against a
deliberately mutated COPY, without ever touching the real file.
"""

import base64
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import traceback
import urllib.request

REPO_ROOT = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))

# Obvious placeholders. Nothing here is or resembles a real secret.
FAKE_ACCESS = "FAKE-ACCESS-TOKEN-DO-NOT-USE"
FAKE_REFRESH = "FAKE-REFRESH-TOKEN-DO-NOT-USE"

NEW_LIVE = ["cursor", "copilot", "windsurf", "devin", "kimi", "cline"]
EXPECTED_LIVE = set(NEW_LIVE) | {"claude", "codex"}
EXPECTED_PLANNED = {"glm", "replit", "antigravity", "railway"}
EXPECTED_IDS = sorted(EXPECTED_LIVE | EXPECTED_PLANNED)


# ---------------------------------------------------------------------------
# result bookkeeping (same conventions as tests/ai_accounts_regression.py)
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


# ---------------------------------------------------------------------------
# sandbox, installed before anything under test is imported
# ---------------------------------------------------------------------------
TMPDIR = os.path.realpath(tempfile.mkdtemp(prefix="nw_ai_adapters_"))
FAKE_HOME = os.path.join(TMPDIR, "home")
os.makedirs(FAKE_HOME, exist_ok=True)
os.makedirs(os.path.join(TMPDIR, "appdata"), exist_ok=True)
os.makedirs(os.path.join(TMPDIR, "vault"), exist_ok=True)

os.environ["AI_ACCOUNTS_FILE"] = os.path.join(TMPDIR, "vault", "accounts.json")
# Every notion of "home" is redirected, not just the feature's own variable:
# a helper in this codebase appends the per-user default path to whatever its
# variable says, so pinning one variable still leaves a walk into the real
# credential stores.
for _v in ("HOME", "USERPROFILE", "HOMEPATH"):
    os.environ[_v] = FAKE_HOME
os.environ["HOMEDRIVE"] = ""
os.environ["APPDATA"] = os.path.join(TMPDIR, "appdata")
os.environ["LOCALAPPDATA"] = os.path.join(TMPDIR, "appdata")
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMPDIR, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(TMPDIR, "data")


def inside_tmp(path):
    try:
        rp = os.path.realpath(path)
    except Exception:
        return False
    return os.path.normcase(rp).startswith(os.path.normcase(TMPDIR) + os.sep)


def cleanup():
    shutil.rmtree(TMPDIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# network kill-switch
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


def _blocked_urlopen(*a, **k):
    _trip("urlopen")


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


urllib.request.urlopen = _blocked_urlopen
urllib.request.OpenerDirector.open = _blocked_opener_open
socket.create_connection = _blocked_create_connection
socket.socket.connect = _blocked_connect


def probe_tripwire():
    """Prove the kill-switch fires. Returns the list of layers that refused.

    TEST-NET-1 (192.0.2.0/24) is used as the target: even if a layer somehow
    were not patched, that block is reserved for documentation and routes
    nowhere.
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
# import the module under test
# ---------------------------------------------------------------------------
_ALT = os.environ.get("NW_AIPROVIDERS_DIR")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if _ALT:
    sys.path.insert(0, _ALT)

aiproviders = None
IMPORT_ERROR = None
try:
    import aiproviders  # noqa: F811
except Exception as e:
    IMPORT_ERROR = "%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------------------
# transport stub over the module's documented seam
# ---------------------------------------------------------------------------
HTTP = {"resp": (0, None, "StubbedTransport"), "calls": []}


def _stub_http_json(url, headers, data=None, timeout=None):
    HTTP["calls"].append(url)
    r = HTTP["resp"]
    return r(url) if callable(r) else r


HTTP_STUB_INSTALLED = False
if aiproviders is not None and hasattr(aiproviders, "_http_json"):
    aiproviders._http_json = _stub_http_json
    HTTP_STUB_INSTALLED = True


def set_http(status, parsed, raw=None):
    if raw is None:
        raw = json.dumps(parsed) if parsed is not None else ""
    HTTP["resp"] = (status, parsed, raw)
    HTTP["calls"] = []


def serve(parsed):
    """200 with this JSON body; clears the call log."""
    set_http(200, parsed)


def serve_fn(fn):
    HTTP["resp"] = fn
    HTTP["calls"] = []


# ---------------------------------------------------------------------------
# output inspection
#
# The normalised shape is <window>_pct / <window>_used / <window>_limit /
# <window>_reset with percent CONSUMED on 0..100. Assertions match on the
# KIND of field rather than an exact key, so the suite does not break when a
# vendor gains an extra window, but a number in the wrong kind of field (used
# vs remaining) still fails.
# ---------------------------------------------------------------------------
def flatten(o, prefix=""):
    out = {}
    if isinstance(o, dict):
        for k, v in o.items():
            key = ("%s.%s" % (prefix, k)) if prefix else str(k)
            out.update(flatten(v, key))
    elif isinstance(o, (list, tuple)):
        for i, v in enumerate(o):
            out.update(flatten(v, "%s[%d]" % (prefix, i)))
    else:
        out[prefix] = o
    return out


def numeric_fields(out, include=(), exclude=()):
    got = {}
    for k, v in flatten(out).items():
        kl = k.lower()
        if exclude and any(x in kl for x in exclude):
            continue
        if include and not any(x in kl for x in include):
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        got[k] = float(v)
    return got


PCT = ("pct", "percent", "ratio")
NOT_REMAINING = ("remain", "left", "avail")
LIMIT_TOK = ("limit", "entitle", "total", "max", "quota", "cap")
USED_TOK = ("used", "usage", "count", "request", "consum")


def has_value(out, include, value, exclude=(), tol=0.51):
    return any(abs(v - value) <= tol
               for v in numeric_fields(out, include, exclude).values())


def used_pct_values(out):
    return numeric_fields(out, PCT, NOT_REMAINING)


def reset_fields(out):
    return {k: v for k, v in flatten(out).items() if "reset" in k.lower()}


def contains_str(out, needle):
    return any(isinstance(v, str) and needle in v
               for v in flatten(out).values())


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def fake_jwt(payload):
    def seg(o):
        return base64.urlsafe_b64encode(
            json.dumps(o, separators=(",", ":")).encode()).decode().rstrip("=")
    return "%s.%s.%s" % (seg({"alg": "none", "typ": "JWT"}), seg(payload),
                         "FAKE-SIGNATURE-DO-NOT-USE")


CURSOR_UID = "00000000-0000-4000-8000-00000000fake"
CURSOR_JWT = fake_jwt({"sub": "auth0|" + CURSOR_UID,
                       "exp": int(time.time()) + 3600})


def cursor_cred(tok=CURSOR_JWT):
    return {"accessToken": tok, "refreshToken": FAKE_REFRESH}


# Cursor keys the body by MODEL NAME; the flagship pool is the one with the
# real cap.
CURSOR_OK = {
    "gpt-4": {"numRequests": 120, "numRequestsTotal": 120,
              "maxRequestUsage": 500, "maxTokenUsage": None},
    "gpt-3.5-turbo": {"numRequests": 12, "numRequestsTotal": 12,
                      "maxRequestUsage": None, "maxTokenUsage": None},
    "startOfMonth": "2026-09-01T00:00:00Z",
}

COPILOT_CRED = {"oauth_token": FAKE_ACCESS}
COPILOT_PAID = {
    "quota_snapshots": {
        "premium_interactions": {"entitlement": 300, "remaining": 75.0,
                                 "percent_remaining": 25.0,
                                 "unlimited": False},
        "chat": {"entitlement": 0, "remaining": 0, "percent_remaining": 0,
                 "unlimited": True},
        "completions": {"entitlement": 0, "remaining": 0,
                        "percent_remaining": 0, "unlimited": True},
    },
    "quota_reset_date": "2026-10-01",
    "copilot_plan": "individual",
}
# Free tier: a COMPLETELY different body. limited_user_quotas is what is LEFT.
COPILOT_FREE = {
    "monthly_quotas": {"chat": 50, "completions": 2000},
    "limited_user_quotas": {"chat": 30, "completions": 1200},
    "limited_user_reset_date": "2026-10-01",
    "access_type_sku": "free_limited_copilot",
    "copilot_plan": "free",
}

WD_CRED = {"apiKey": FAKE_ACCESS}
WD_RESET = 1790000000
# proto3 JSON: percentages are REMAINING, and zero values are OMITTED.
WD_REMAINING_20 = {"planStatus": {
    "dailyQuotaRemainingPercent": 20,
    "dailyQuotaResetAtUnix": WD_RESET,
    "overageBalanceMicros": 3500000,
}}
WD_OMITTED = {"planStatus": {"dailyQuotaResetAtUnix": WD_RESET}}

KIMI_CRED = {"access_token": FAKE_ACCESS, "refresh_token": FAKE_REFRESH}
# Ratio generation. The weekly window is ABSENT, not zero.
KIMI_RATIO = {"usages": {
    "five_hour": {"usedRatio": 0.42, "resetAt": "2026-09-19T18:00:00Z"}}}
# Legacy count generation, still in circulation.
KIMI_LEGACY = {"usages": {
    "five_hour": {"used": 120, "limit": 500,
                  "reset_at": "2026-09-19T18:00:00Z"}}}

CLINE_CRED = {"token": FAKE_ACCESS}
CLINE_OK = {"success": True, "data": {"limits": [
    {"type": "five_hour", "percentUsed": 42.5,
     "resetsAt": "2026-10-01T00:00:00Z"},
    {"type": "weekly", "percentUsed": 10, "resetsAt": None},
]}}

CRED = {"cursor": cursor_cred(), "copilot": COPILOT_CRED, "windsurf": WD_CRED,
        "devin": WD_CRED, "kimi": KIMI_CRED, "cline": CLINE_CRED}
OK_BODY = {"cursor": CURSOR_OK, "copilot": COPILOT_PAID,
           "windsurf": WD_REMAINING_20, "devin": WD_REMAINING_20,
           "kimi": KIMI_RATIO, "cline": CLINE_OK}


def need_adapter(pid):
    need(aiproviders, "fetch_usage", "PROVIDERS", "provider")
    if not HTTP_STUB_INSTALLED:
        raise Skip("aiproviders._http_json is not stubbable; refusing to call "
                   "an adapter without a guaranteed-offline transport")
    p = aiproviders.provider(pid)
    if p is None:
        raise Skip("provider %r is not registered" % pid)
    if p.get("status") != "live":
        raise Skip("provider %r is still status=%r -- no adapter yet"
                   % (pid, p.get("status")))


# ===========================================================================
# safety
# ===========================================================================
def s01_tripwire_armed():
    armed = set(TRIPWIRE_ARMED)
    missing = sorted(REQUIRED_LAYERS - armed)
    return not missing, ("probed and refused: %s" % ", ".join(TRIPWIRE_ARMED)
                         if not missing else "NOT armed: %r" % missing)


def s02_paths_sandboxed():
    probes = {
        "expanduser('~')": os.path.expanduser("~"),
        "HOME": os.environ.get("HOME", ""),
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
        "APPDATA": os.environ.get("APPDATA", ""),
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME", ""),
        "AI_ACCOUNTS_FILE": os.environ.get("AI_ACCOUNTS_FILE", ""),
    }
    bad = sorted(k for k, v in probes.items() if not inside_tmp(v))
    return not bad, ("all %d resolved paths inside the sandbox" % len(probes)
                     if not bad else "OUTSIDE the sandbox: %r" % bad)


def s03_transport_stubbed():
    need(aiproviders, "_http_json")
    return (HTTP_STUB_INSTALLED
            and aiproviders._http_json is _stub_http_json), \
        "aiproviders._http_json replaced by the harness stub"


def s99_no_network_attempted():
    return not NETWORK_ATTEMPTS, (
        "zero outbound attempts across the whole run" if not NETWORK_ATTEMPTS
        else "TRIPWIRE FIRED: %r" % (NETWORK_ATTEMPTS[:5],))


# ===========================================================================
# registry
# ===========================================================================
def t01_registry_twelve_slugs():
    need(aiproviders, "PROVIDERS", "provider_ids")
    ids = list(aiproviders.provider_ids())
    ok = (len(aiproviders.PROVIDERS) == 12 and len(set(ids)) == 12
          and sorted(ids) == EXPECTED_IDS)
    return ok, ("12 providers, slugs exactly as expected" if ok else
                "n=%d missing=%r extra=%r"
                % (len(ids), sorted(set(EXPECTED_IDS) - set(ids)),
                   sorted(set(ids) - set(EXPECTED_IDS))))


def t02_six_new_adapters_are_live():
    need(aiproviders, "PROVIDERS", "is_live")
    live = {p["id"] for p in aiproviders.PROVIDERS if p.get("status") == "live"}
    planned = {p["id"] for p in aiproviders.PROVIDERS
               if p.get("status") != "live"}
    ok = (live == EXPECTED_LIVE and planned == EXPECTED_PLANNED
          and all(aiproviders.is_live(p) for p in live)
          and not any(aiproviders.is_live(p) for p in planned))
    return ok, ("live=%s planned=%s" % (sorted(live), sorted(planned)) if ok
                else "expected-live-but-planned=%r live=%s"
                     % (sorted(set(NEW_LIVE) - live), sorted(live)))


def t03_planned_not_supported():
    need(aiproviders, "fetch_usage", "PROVIDERS", "NOT_SUPPORTED")
    planned = [p["id"] for p in aiproviders.PROVIDERS
               if p.get("status") != "live"]
    bad = []
    for pid in planned:
        for cred in ({}, None, "junk", {"accessToken": FAKE_ACCESS}):
            try:
                out = aiproviders.fetch_usage(pid, cred)
            except Exception as e:
                bad.append((pid, "raised " + type(e).__name__))
                continue
            if not isinstance(out, dict):
                bad.append((pid, "non-dict"))
            elif out.get("error") != "not supported yet":
                bad.append((pid, out.get("error")))
    return not bad, "%d planned providers x4 creds -> 'not supported yet'%s" % (
        len(planned), "" if not bad else "; MISMATCH %r" % (bad[:4],))


def t04_live_adapters_respond():
    need(aiproviders, "fetch_usage", "PROVIDERS")
    if not HTTP_STUB_INSTALLED:
        raise Skip("transport not stubbed")
    live = [p["id"] for p in aiproviders.PROVIDERS if p.get("status") == "live"]
    bad = []
    for pid in live:
        for body in (OK_BODY.get(pid, {}), {}, None):
            set_http(200, body, json.dumps(body) if body is not None else "")
            try:
                out = aiproviders.fetch_usage(pid, CRED.get(pid, {}))
            except Exception as e:
                bad.append((pid, "raised " + type(e).__name__))
                continue
            if not isinstance(out, dict):
                bad.append((pid, "non-dict result"))
            elif out.get("error") == "not supported yet":
                bad.append((pid, "live but answers 'not supported yet'"))
    return not bad, "%d live adapters answered a dict on 3 bodies%s" % (
        len(live), "" if not bad else "; problems=%r" % (bad,))


# ===========================================================================
# success parsing
# ===========================================================================
def _cursor_success():
    need_adapter("cursor")
    serve(CURSOR_OK)
    out = aiproviders.fetch_usage("cursor", cursor_cred())
    if out.get("error"):
        return False, "error=%r" % out["error"]
    bad = []
    if not has_value(out, USED_TOK, 120):
        bad.append("used=120 missing (flagship pool, not the 12-request one)")
    if not has_value(out, LIMIT_TOK, 500):
        bad.append("limit=500 missing")
    if not has_value(out, PCT, 24.0, NOT_REMAINING):
        bad.append("pct != 24 (values=%r)" % sorted(used_pct_values(out)
                                                    .values()))
    if not contains_str(out, "2026-09-01"):
        bad.append("period anchor startOfMonth dropped entirely")
    return not bad, ("120/500 = 24%, month anchor kept" if not bad
                     else "; ".join(bad))


def _copilot_success():
    need_adapter("copilot")
    serve(COPILOT_PAID)
    out = aiproviders.fetch_usage("copilot", COPILOT_CRED)
    if out.get("error"):
        return False, "error=%r" % out["error"]
    bad = []
    if not has_value(out, LIMIT_TOK, 300):
        bad.append("entitlement=300 missing")
    if not has_value(out, USED_TOK, 225):
        bad.append("used=225 (300-75) missing")
    if not has_value(out, PCT, 75.0, NOT_REMAINING):
        bad.append("used pct != 75 (25 remaining) -- values=%r"
                   % sorted(used_pct_values(out).values()))
    if has_value(out, PCT, 25.0, NOT_REMAINING):
        bad.append("25 (REMAINING) surfaced as a used percentage")
    if not contains_str(out, "2026-10-01"):
        bad.append("quota_reset_date dropped")
    return not bad, ("premium 225/300 = 75% used, reset kept" if not bad
                     else "; ".join(bad))


def _wd_success(pid):
    def body():
        need_adapter(pid)
        serve(WD_REMAINING_20)
        out = aiproviders.fetch_usage(pid, WD_CRED)
        if out.get("error"):
            return False, "error=%r" % out["error"]
        bad = []
        if not has_value(out, ("reset",), float(WD_RESET), tol=1.0):
            bad.append("reset timestamp dropped (fields=%r)"
                       % sorted(reset_fields(out)))
        vals = used_pct_values(out)
        if not vals:
            bad.append("no used-percentage field at all")
        elif not has_value(out, PCT, 80.0, NOT_REMAINING):
            bad.append("used pct != 80 (values=%r)" % sorted(vals.values()))
        return not bad, ("20% remaining -> 80% used, reset kept"
                         if not bad else "; ".join(bad))
    return body


def _kimi_success():
    need_adapter("kimi")
    serve(KIMI_RATIO)
    out = aiproviders.fetch_usage("kimi", KIMI_CRED)
    if out.get("error"):
        return False, "error=%r" % out["error"]
    bad = []
    if not (has_value(out, PCT, 42.0, NOT_REMAINING)
            or has_value(out, PCT, 0.42, NOT_REMAINING, tol=0.005)):
        bad.append("usedRatio 0.42 not surfaced as 42%% or 0.42 (values=%r)"
                   % sorted(used_pct_values(out).values()))
    if not contains_str(out, "2026-09-19"):
        bad.append("resetAt dropped")
    return not bad, ("five-hour pool 42% used, reset kept" if not bad
                     else "; ".join(bad))


def _cline_success():
    need_adapter("cline")
    serve(CLINE_OK)
    out = aiproviders.fetch_usage("cline", CLINE_CRED)
    if out.get("error"):
        return False, "error=%r" % out["error"]
    bad = []
    if not has_value(out, PCT, 42.5, NOT_REMAINING, tol=0.05):
        bad.append("percentUsed 42.5 missing (values=%r)"
                   % sorted(used_pct_values(out).values()))
    if not has_value(out, PCT, 10.0, NOT_REMAINING, tol=0.05):
        bad.append("the second limits[] row (10%) was dropped")
    if not contains_str(out, "2026-10-01"):
        bad.append("resetsAt dropped")
    return not bad, ("limits[] parsed per window, 42.5% + 10%" if not bad
                     else "; ".join(bad))


# ===========================================================================
# failure classification
# ===========================================================================
DEAD_BODY = '{"error":"invalid_grant","error_description":"token expired"}'
BARE_400 = '{"error":"malformed_request","message":"missing parameter"}'


def _classification(pid):
    """Three failure kinds -> three different reasons, and a bare 400 is
    never a dead login.

    Vendors with a real refresh flow are judged on refresh_cred(); vendors
    documented as issuing non-expiring tokens have no refresh to exercise, so
    the same three kinds are judged on the usage path instead. Either way the
    offline case must stay distinct and no bare 400 may claim an expired
    login.
    """
    def body():
        need_adapter(pid)
        cred = CRED[pid]
        set_http(400, json.loads(DEAD_BODY), DEAD_BODY)
        _, r_dead = aiproviders.refresh_cred(pid, cred)
        did_call = bool(HTTP["calls"])

        if did_call:
            set_http(401, json.loads(DEAD_BODY), DEAD_BODY)
            _, r_401 = aiproviders.refresh_cred(pid, cred)
            set_http(0, None, "URLError")
            _, r_off = aiproviders.refresh_cred(pid, cred)
            set_http(500, None, "upstream boom")
            _, r_srv = aiproviders.refresh_cred(pid, cred)
            set_http(400, json.loads(BARE_400), BARE_400)
            _, r_bare = aiproviders.refresh_cred(pid, cred)
            bad = []
            if r_dead != "login expired":
                bad.append("400+invalid_grant -> %r" % r_dead)
            if r_401 != "login expired":
                bad.append("401+invalid_grant -> %r" % r_401)
            if r_off != "offline":
                bad.append("transport failure -> %r (want 'offline')" % r_off)
            if not (isinstance(r_srv, str) and "refresh failed" in r_srv
                    and "500" in r_srv):
                bad.append("500 -> %r (want 'refresh failed (500)')" % r_srv)
            if len({r_dead, r_off, r_srv}) != 3:
                bad.append("reasons collapsed: %r" % ((r_dead, r_off, r_srv),))
            if r_bare == "login expired":
                bad.append("BARE 400 reported as a dead login")
            return not bad, ("refresh: expired/offline/refresh-failed "
                             "distinct; bare 400 -> %r" % (r_bare,)
                             if not bad else "; ".join(bad))

        # No refresh flow for this vendor.
        set_http(0, None, "URLError")
        e_off = aiproviders.fetch_usage(pid, cred).get("error")
        set_http(401, json.loads(DEAD_BODY), DEAD_BODY)
        e_401 = aiproviders.fetch_usage(pid, cred).get("error")
        set_http(500, None, "upstream boom")
        e_srv = aiproviders.fetch_usage(pid, cred).get("error")
        set_http(400, json.loads(BARE_400), BARE_400)
        e_bare = aiproviders.fetch_usage(pid, cred).get("error")
        bad = []
        if e_off != "offline":
            bad.append("transport failure -> %r (want 'offline')" % e_off)
        if len({e_off, e_401, e_srv}) != 3:
            bad.append("usage errors collapsed: %r"
                       % ((e_off, e_401, e_srv),))
        for label, v in (("401", e_401), ("bare 400", e_bare),
                         ("500", e_srv)):
            if isinstance(v, str) and "expired" in v.lower():
                bad.append("%s reported as expired: %r" % (label, v))
        if not (isinstance(e_srv, str) and "500" in e_srv):
            bad.append("500 loses its status code: %r" % e_srv)
        return not bad, ("no refresh flow (%r); usage: offline/%r/%r distinct,"
                         " bare 400 -> %r" % (r_dead, e_401, e_srv, e_bare)
                         if not bad else "; ".join(bad))
    return body


def t_classify_primitive():
    """The shared decision, tested directly as well as through the adapters."""
    need(aiproviders, "_classify")
    cases = [((0, "URLError"), "offline"), ((0, ""), "offline"),
             ((400, DEAD_BODY), "login expired"),
             ((401, DEAD_BODY), "login expired"),
             ((401, '{"error":"revoked"}'), "login expired"),
             ((400, '{"detail":"credential REVOKED"}'), "login expired")]
    bad = []
    for (st, raw), want in cases:
        got = aiproviders._classify(st, raw)
        if got != want:
            bad.append("(%s,...) -> %r want %r" % (st, got, want))
    bare = aiproviders._classify(400, BARE_400) or ""
    if bare == "login expired":
        bad.append("bare 400 -> 'login expired'")
    if "refresh failed" not in bare or "400" not in bare:
        bad.append("bare 400 -> %r (want 'refresh failed (400)')" % bare)
    for st in (403, 404, 429, 500, 502):
        got = aiproviders._classify(st, "whatever") or ""
        if "refresh failed" not in got or str(st) not in got:
            bad.append("%s -> %r" % (st, got))
    return not bad, ("offline / login expired / refresh failed (N) distinct "
                     "across %d cases; bare 400 -> %r"
                     % (len(cases) + 6, bare) if not bad else "; ".join(bad))


def t_no_refresh_vendors_honest():
    """A vendor with no refresh flow must say so, not claim an expired login."""
    need(aiproviders, "refresh_cred", "is_live")
    bad = []
    seen = {}
    for pid in NEW_LIVE:
        if not aiproviders.is_live(pid):
            continue
        set_http(0, None, "URLError")
        new, why = aiproviders.refresh_cred(pid, CRED[pid])
        if HTTP["calls"]:
            continue  # has a real refresh flow; covered elsewhere
        seen[pid] = why
        if new is not None:
            bad.append("%s invented a credential without a request" % pid)
        if why is None:
            bad.append("%s: silent no-op refresh" % pid)
        elif why == "login expired":
            bad.append("%s: no refresh flow reported as 'login expired'" % pid)
    if not seen:
        raise Skip("no no-refresh vendors live yet")
    return not bad, ("honest reasons: %r" % seen if not bad
                     else "; ".join(bad))


# ===========================================================================
# junk input / expiry
# ===========================================================================
JUNK = [None, {}, [], "", 0, "a-string", True,
        {"accessToken": None}, {"tokens": "not-a-dict"},
        {"a": {"b": {"c": {"d": [1, {"e": None}]}}}},
        {"quota_snapshots": "string-not-object"}, {"limits": "not-a-list"},
        {"token": FAKE_ACCESS, "limits": [None, 5, {"type": None}]},
        {"token": FAKE_ACCESS, "planStatus": "nope"},
        {"token": FAKE_ACCESS, "usages": [[[]]]}]

JUNK_BODIES = [(200, {}), (200, None), (200, []), (200, "text"),
               (0, None), (500, None), (404, {"x": 1})]


def t_junk_never_raises():
    need(aiproviders, "fetch_usage", "provider_ids")
    if not HTTP_STUB_INSTALLED:
        raise Skip("transport not stubbed")
    bad = []
    n = 0
    for pid in list(aiproviders.provider_ids()) + ["no-such-provider"]:
        for st, p in JUNK_BODIES:
            set_http(st, p, "x")
            for c in JUNK:
                n += 1
                try:
                    out = aiproviders.fetch_usage(pid, c)
                except Exception as e:
                    bad.append((pid, "raised " + type(e).__name__))
                    continue
                if not isinstance(out, dict):
                    bad.append((pid, "non-dict %s" % type(out).__name__))
                elif "error" not in out:
                    bad.append((pid, "junk credential -> no error key"))
                elif "adapter error" in str(out.get("error")):
                    bad.append((pid, "swallowed exception: %r" % out["error"]))
    return not bad, "%d provider/body/credential combos, all total%s" % (
        n, "" if not bad else "; problems=%r" % (bad[:4],))


def t_cred_expiry_none_when_unknown():
    """No expiry in the shape means None, not a guess -- and a non-expiring
    token must never be called unrecoverable."""
    need(aiproviders, "cred_expiry", "is_unrecoverable", "is_live")
    bad = []
    shapes = [{}, None, "string", 7, {"accessToken": FAKE_ACCESS},
              {"apiKey": FAKE_ACCESS}, {"token": FAKE_ACCESS},
              {"oauth_token": FAKE_ACCESS}, {"access_token": FAKE_ACCESS}]
    for pid in NEW_LIVE:
        for c in shapes:
            try:
                exp = aiproviders.cred_expiry(pid, c)
            except Exception as e:
                bad.append("%s cred_expiry raised %s" % (pid,
                                                         type(e).__name__))
                continue
            if exp is not None:
                bad.append("%s cred_expiry(%s) -> %r, want None"
                           % (pid, type(c).__name__, exp))
            try:
                dead = aiproviders.is_unrecoverable(pid, c)
            except Exception as e:
                bad.append("%s is_unrecoverable raised %s"
                           % (pid, type(e).__name__))
                continue
            if dead:
                bad.append("%s: unknown/non-expiring credential called "
                           "unrecoverable" % pid)
    live = [p for p in NEW_LIVE if aiproviders.is_live(p)]
    return not bad, ("%d shapes x %d providers -> None, none unrecoverable "
                     "(%d live)" % (len(shapes), len(NEW_LIVE), len(live))
                     if not bad else "; ".join(bad[:4]))


def t_cred_max_stale_present():
    need(aiproviders, "CRED_MAX_STALE")
    v = aiproviders.CRED_MAX_STALE
    return isinstance(v, (int, float)) and v > 0, "CRED_MAX_STALE=%r s" % v


# ===========================================================================
# trap-specific tests
# ===========================================================================
def t_copilot_free_tier():
    """Free accounts use monthly_quotas / limited_user_quotas, NOT
    quota_snapshots. Zeros here look like real data and are worse than an
    error."""
    need_adapter("copilot")
    serve(COPILOT_FREE)
    out = aiproviders.fetch_usage("copilot", COPILOT_CRED)
    if out.get("error"):
        return False, "free-tier shape -> error=%r" % out["error"]
    nums = numeric_fields(out)
    bad = []
    if nums and all(abs(v) < 1e-9 for v in nums.values()):
        bad.append("every numeric field is ZERO -- paid-shape-only parse")
    if not has_value(out, LIMIT_TOK, 50):
        bad.append("chat allowance 50 missing")
    if not has_value(out, USED_TOK, 20):
        bad.append("chat used 20 (50-30) missing")
    if not has_value(out, PCT, 40.0, NOT_REMAINING):
        bad.append("chat pct != 40 (values=%r)"
                   % sorted(used_pct_values(out).values()))
    if not has_value(out, LIMIT_TOK, 2000):
        bad.append("completions allowance 2000 missing")
    if not contains_str(out, "2026-10-01"):
        bad.append("limited_user_reset_date dropped")
    return not bad, ("free tier: chat 20/50 = 40% used, real numbers"
                     if not bad else "; ".join(bad))


def _wd_omitted_is_full(pid):
    """proto3 omits zero: no percentage + a reset time == 100% consumed."""
    def body():
        need_adapter(pid)
        serve(WD_OMITTED)
        out = aiproviders.fetch_usage(pid, WD_CRED)
        if out.get("error"):
            return False, "omitted-field response -> error=%r" % out["error"]
        vals = used_pct_values(out)
        if not vals:
            return False, ("no used-percentage emitted; an omitted proto3 "
                           "zero must read as 100% consumed, not unknown")
        if any(abs(v) < 1e-9 for v in vals.values()):
            return False, ("omitted percentage read as 0%% used (values=%r) "
                           "-- proto3 omits zeros, so this account is FULLY "
                           "consumed" % sorted(vals.values()))
        if not has_value(out, PCT, 100.0, NOT_REMAINING):
            return False, "used pct != 100 (values=%r)" % sorted(vals.values())
        if not has_value(out, ("reset",), float(WD_RESET), tol=1.0):
            return False, "reset timestamp lost"
        return True, "omitted zero -> 100% consumed, reset kept"
    return body


def _wd_remaining_not_used(pid):
    """20 REMAINING must not display as 20 USED."""
    def body():
        need_adapter(pid)
        serve(WD_REMAINING_20)
        out = aiproviders.fetch_usage(pid, WD_CRED)
        if out.get("error"):
            return False, "error=%r" % out["error"]
        vals = used_pct_values(out)
        if not vals:
            return False, "no used-percentage field at all"
        if any(abs(v - 20.0) <= 0.51 for v in vals.values()):
            return False, ("20%% REMAINING displayed as 20%% USED -- inverted "
                           "(values=%r)" % sorted(vals.values()))
        if not has_value(out, PCT, 80.0, NOT_REMAINING):
            return False, "used pct != 80 (values=%r)" % sorted(vals.values())
        return True, "percentRemaining=20 -> 80% used"
    return body


def t_cline_null_reset():
    need_adapter("cline")
    serve(CLINE_OK)
    out = aiproviders.fetch_usage("cline", CLINE_CRED)
    if out.get("error"):
        return False, "error=%r" % out["error"]
    bad = []
    for k, v in reset_fields(out).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) \
                and abs(v) < 1e-9:
            bad.append("%s == 0 (epoch); want the key absent" % k)
        if v is None:
            bad.append("%s == None; want the key absent" % k)
        if isinstance(v, str) and ("1970" in v or v == "0"):
            bad.append("%s == %r (epoch); want the key absent" % (k, v))
    if contains_str(out, "1970"):
        bad.append("epoch 1970 leaked into the output")
    if not has_value(out, PCT, 10.0, NOT_REMAINING, tol=0.05):
        bad.append("the null-reset window's percentUsed=10 was dropped "
                   "along with its clock")
    return not bad, ("null resetsAt -> reset key absent, percentage kept "
                     "(reset fields=%r)" % sorted(reset_fields(out))
                     if not bad else "; ".join(bad))


def t_kimi_absent_window_stays_absent():
    need_adapter("kimi")
    serve(KIMI_RATIO)
    out = aiproviders.fetch_usage("kimi", KIMI_CRED)
    if out.get("error"):
        return False, "error=%r" % out["error"]
    week = {k: v for k, v in flatten(out).items()
            if any(t in k.lower() for t in ("week", "month", "7d"))}
    zeros = sorted(k for k, v in week.items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)
                   and abs(v) < 1e-9)
    if zeros:
        return False, "absent window rendered as zero usage: %r" % zeros
    if week:
        return False, "absent window fabricated: %r" % sorted(week)
    if not has_value(out, PCT, 42.0, NOT_REMAINING):
        return False, "the window that WAS reported got lost too"
    return True, "reported window present, unreported windows absent"


def t_kimi_legacy_count_shape():
    need_adapter("kimi")
    serve(KIMI_LEGACY)
    out = aiproviders.fetch_usage("kimi", KIMI_CRED)
    if out.get("error"):
        return False, "legacy count shape -> error=%r" % out["error"]
    bad = []
    if not (has_value(out, PCT, 24.0, NOT_REMAINING)
            or has_value(out, USED_TOK, 120)):
        bad.append("120/500 not surfaced as 24%% or as a count (values=%r)"
                   % sorted(numeric_fields(out).values()))
    if not contains_str(out, "2026-09-19"):
        bad.append("reset_at dropped")
    return not bad, ("legacy 120/500 -> 24% used" if not bad
                     else "; ".join(bad))


def t_cursor_jwt_user_id():
    """sub is '<tenant>|<user id>'; only the second field is the id.

    The REST path is the one that needs the id, so the RPC is made to answer
    unusably to force the fallback.
    """
    need_adapter("cursor")

    def route(url):
        if "user" in url.lower() and "=" in url:
            return 200, CURSOR_OK, json.dumps(CURSOR_OK)
        return 404, None, "not found"

    serve_fn(route)
    out = aiproviders.fetch_usage("cursor", cursor_cred())
    urls = list(HTTP["calls"])
    if len(urls) < 2:
        return False, "no fallback request was made (calls=%d)" % len(urls)
    rest = urls[-1]
    if CURSOR_UID not in rest:
        return False, "derived user id absent from the request"
    if "auth0|" in rest or "auth0%7C" in rest:
        return False, "sub sent unsplit; the tenant prefix leaked into the URL"
    if out.get("error"):
        return False, "fallback parsed to error=%r" % out["error"]
    if not has_value(out, USED_TOK, 120):
        return False, "fallback response not parsed (%r)" % out
    return True, "user id split out of the sub claim and used in the request"


def t_cursor_malformed_jwt():
    need_adapter("cursor")
    serve_fn(lambda url: (404, None, "not found"))
    bad_tokens = ["", "not-a-jwt", "a.b", "a.b.c",
                  "eyJhbGciOiJub25lIn0.!!!not-base64!!!.sig",
                  fake_jwt({"nosub": 1}), fake_jwt({"sub": "no-pipe-here"}),
                  fake_jwt({"sub": "auth0|"}), fake_jwt({"sub": 12345}),
                  None, 12345]
    bad = []
    for tok in bad_tokens:
        try:
            out = aiproviders.fetch_usage("cursor", cursor_cred(tok))
        except Exception as e:
            bad.append("token #%d raised %s" % (bad_tokens.index(tok),
                                                type(e).__name__))
            continue
        if not isinstance(out, dict) or "error" not in out:
            bad.append("token #%d -> no error key" % bad_tokens.index(tok))
        elif "adapter error" in str(out["error"]):
            bad.append("token #%d -> swallowed exception %r"
                       % (bad_tokens.index(tok), out["error"]))
    return not bad, ("%d malformed JWTs -> error dict, no exception"
                     % len(bad_tokens) if not bad else "; ".join(bad[:3]))


# ===========================================================================
# durable credential identity
#
# Covers the fix for a bug the user actually hit: adding a SECOND claude
# account silently reconnected the FIRST one. The claude credential file
# carries no account id, so the fingerprint was built from subscriptionType
# plus expiresAt -- and expiresAt is rewritten on every single token issue.
# The "fingerprint" was therefore a timestamp, the duplicate check never
# matched, and a second row was written for the same account.
#
# These tests are written against the CONTRACT of the identity API, using
# SYNTHETIC credential dicts and throwaway CLI homes created inside the
# harness sandbox. No real credential file is read, and no fingerprint is
# ever allowed to contain token material or an email address.
#
# A missing identity API is a FAIL here, not a SKIP: unlike the `planned`
# adapters, this capability exists and is what stops the user's bug coming
# back, so its disappearance is a regression rather than an absent feature.
# ===========================================================================
IDENTITY_API = ("cred_identity", "cred_fingerprint", "is_durable_fingerprint",
                "cred_fingerprint_candidates", "cred_fingerprint_matches")

# Obvious synthetic account identifiers. Neither is a real account.
ACCT_A = "11111111-2222-3333-4444-555555555555"
ACCT_B = "99999999-8888-7777-6666-555555555555"
MAIL_A = "Nobody.A@example.invalid"
MAIL_B = "nobody.b@example.invalid"

# Two arbitrary token expiries, far apart, so "it changed" is unmistakable.
EXP_OLD = 1800000000000
EXP_NEW = 1900000000000
# The legacy fingerprint for claude_cred() at EXP_OLD, written out as a
# literal rather than computed. Vault rows and tombstone files already on
# disk contain exactly this string; if the implementation's spelling drifts
# by one character every one of them is orphaned, which presents as the
# user's original bug (a known account seen as new).
LEGACY_FP_OLD = "claude:pro:1800000000000"


def need_identity():
    if aiproviders is None:
        raise RuntimeError("aiproviders not importable: %s" % IMPORT_ERROR)
    missing = [a for a in IDENTITY_API if not hasattr(aiproviders, a)]
    if missing:
        raise RuntimeError("durable identity API missing: %s"
                           % ", ".join(missing))


def claude_cred(tier="pro", expires_at=EXP_OLD, oauth_account=None):
    """A synthetic claude credential, shaped like .credentials.json.

    Deliberately carries NO account identifier in the default case, because
    the real file carries none either -- that absence is the whole reason
    the durable scheme has to look elsewhere.
    """
    cred = {"claudeAiOauth": {
        "accessToken": FAKE_ACCESS, "refreshToken": FAKE_REFRESH,
        "expiresAt": expires_at, "scopes": ["user:inference", "user:profile"],
        "subscriptionType": tier, "rateLimitTier": "default_claude_ai"}}
    if oauth_account is not None:
        cred["oauthAccount"] = oauth_account
    return cred


_HOME_SEQ = [0]


def claude_home(account_uuid=None, email=None, state=True, tag=""):
    """A throwaway CLI home; returns the credential path inside it.

    Layout mirrors the real one: <home>/.claude/.credentials.json beside
    <home>/.claude.json. Every home lives under the harness sandbox, so
    s02_paths_sandboxed's guarantee still covers these reads.
    """
    _HOME_SEQ[0] += 1
    home = os.path.join(TMPDIR, "homes", "h%02d%s" % (_HOME_SEQ[0], tag))
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    cpath = os.path.join(home, ".claude", ".credentials.json")
    with open(cpath, "w", encoding="utf-8") as f:
        json.dump(claude_cred(), f)
    if state:
        acct = {}
        if account_uuid:
            acct["accountUuid"] = account_uuid
        if email:
            acct["emailAddress"] = email
        doc = {"numStartups": 7, "installMethod": "synthetic"}
        if acct:
            acct["organizationUuid"] = "00000000-0000-4000-8000-0000000000ff"
            doc["oauthAccount"] = acct
        with open(os.path.join(home, ".claude.json"), "w",
                  encoding="utf-8") as f:
            json.dump(doc, f)
    return cpath


def no_state_home(tag="nostate"):
    """A home with a credential file but no CLI state file at all."""
    return claude_home(state=False, tag=tag)


def t_identity_stable_across_token_refresh():
    """THE REPORTED BUG: the fingerprint must not move when the token does.

    The drift is asserted first -- against the legacy scheme, via a home
    with no state file -- so a fingerprint that were constant for some
    unrelated reason could not pass this test silently.
    """
    need_identity()
    bare = no_state_home("drift")
    drift_old = aiproviders.cred_fingerprint("claude", claude_cred(
        expires_at=EXP_OLD), bare)
    drift_new = aiproviders.cred_fingerprint("claude", claude_cred(
        expires_at=EXP_NEW), bare)
    if EXP_OLD == EXP_NEW:
        return False, "fixture bug: the two expiries are identical"
    if drift_old == drift_new:
        return False, ("precondition failed: the legacy scheme did NOT move "
                       "when expiresAt changed, so this test proves nothing "
                       "(%r)" % drift_old)

    path = claude_home(account_uuid=ACCT_A, tag="stable")
    before = aiproviders.cred_fingerprint("claude",
                                          claude_cred(expires_at=EXP_OLD),
                                          path)
    after = aiproviders.cred_fingerprint("claude",
                                         claude_cred(expires_at=EXP_NEW),
                                         path)
    bad = []
    if before is None:
        bad.append("no fingerprint produced for a known account")
    if before != after:
        bad.append("fingerprint MOVED across a token refresh: %r -> %r"
                   % (before, after))
    if not aiproviders.is_durable_fingerprint(before):
        bad.append("fingerprint %r not reported durable" % (before,))
    ident = aiproviders.cred_identity("claude",
                                      claude_cred(expires_at=EXP_NEW), path)
    if ident.get("kind") != "account_uuid":
        bad.append("kind=%r, expected account_uuid" % ident.get("kind"))
    if ident.get("durable") is not True:
        bad.append("identity not flagged durable (%r)" % ident.get("durable"))
    if ACCT_A not in (before or ""):
        bad.append("account uuid absent from %r" % (before,))
    return not bad, ("expiresAt %s -> %s moved the legacy fp (%r -> %r) but "
                     "the durable fp held at %r"
                     % (EXP_OLD, EXP_NEW, drift_old, drift_new, before)
                     if not bad else "; ".join(bad))


def t_identity_distinct_accounts_stay_distinct():
    """The fix must not over-collapse: two real accounts stay two."""
    need_identity()
    same_exp = EXP_OLD
    a = aiproviders.cred_fingerprint("claude", claude_cred(
        expires_at=same_exp), claude_home(account_uuid=ACCT_A, tag="da"))
    b = aiproviders.cred_fingerprint("claude", claude_cred(
        expires_at=same_exp), claude_home(account_uuid=ACCT_B, tag="db"))
    ea = aiproviders.cred_fingerprint("claude", claude_cred(),
                                      claude_home(email=MAIL_A, tag="dea"))
    eb = aiproviders.cred_fingerprint("claude", claude_cred(),
                                      claude_home(email=MAIL_B, tag="deb"))
    bad = []
    if a is None or b is None:
        bad.append("a fingerprint was None (%r, %r)" % (a, b))
    elif a == b:
        bad.append("two different accountUuids COLLAPSED to %r" % (a,))
    if ea is None or eb is None:
        bad.append("an email fingerprint was None (%r, %r)" % (ea, eb))
    elif ea == eb:
        bad.append("two different emails COLLAPSED to %r" % (ea,))
    # Cross-matching must fail in both directions.
    if aiproviders.cred_fingerprint_matches(
            "claude", claude_cred(expires_at=same_exp), b,
            claude_home(account_uuid=ACCT_A, tag="dx")):
        bad.append("account A matched account B's stored fingerprint")
    return not bad, ("distinct uuids -> %r vs %r; distinct emails stay "
                     "distinct too" % (a, b) if not bad else "; ".join(bad))


def t_identity_legacy_fingerprint_back_compat():
    """An OLD stored fingerprint must still match its account.

    This is the property that stops the user's bug returning through the
    back door: the scheme changed with NO migration, so every vault row and
    tombstone written before the change is a legacy string, and a credential
    must still answer to it.
    """
    need_identity()
    cred = claude_cred(expires_at=EXP_OLD)
    bare = no_state_home("compat")

    bad = []
    # Byte-for-byte: what a pre-change build wrote for this credential.
    produced = aiproviders.cred_fingerprint("claude", cred, bare)
    if produced != LEGACY_FP_OLD:
        bad.append("legacy string changed: %r != %r"
                   % (produced, LEGACY_FP_OLD))

    known = claude_home(account_uuid=ACCT_A, tag="compat")
    if not aiproviders.cred_fingerprint_matches("claude", cred,
                                                LEGACY_FP_OLD, known):
        bad.append("an account with a durable id no longer answers to its "
                   "OLD fingerprint -- every pre-change vault row orphaned")
    # Same, for a row whose CLI state file is simply gone.
    if not aiproviders.cred_fingerprint_matches("claude", cred,
                                                LEGACY_FP_OLD, bare):
        bad.append("legacy stored fp does not match without a state file")
    cands = aiproviders.cred_fingerprint_candidates("claude", cred, known)
    if not isinstance(cands, list) or LEGACY_FP_OLD not in cands:
        bad.append("candidates %r omit the legacy form" % (cands,))
    if cands and cands[0] != "claude:acct:%s" % ACCT_A:
        bad.append("durable form is not offered first (%r)" % (cands,))
    # And it must still be a fingerprint, not a wildcard.
    if aiproviders.cred_fingerprint_matches(
            "claude", claude_cred(tier="max", expires_at=EXP_OLD),
            LEGACY_FP_OLD, no_state_home("othertier")):
        bad.append("a DIFFERENT subscription tier matched the legacy fp")
    if aiproviders.cred_fingerprint_matches("claude", cred,
                                            "claude:pro:1", bare):
        bad.append("an unrelated legacy fp matched")
    return not bad, ("legacy string reproduced byte-for-byte (%r) and still "
                     "matches; candidates=%r" % (LEGACY_FP_OLD, cands)
                     if not bad else "; ".join(bad))


def t_identity_durable_flag_discriminates():
    """is_durable_fingerprint answers from the stored string alone."""
    need_identity()
    durable = ["claude:acct:%s" % ACCT_A, "claude:email:0123456789abcdef",
               "codex:acct-synthetic-1"]
    legacy = [LEGACY_FP_OLD, "claude:max:1", "claude:?:0"]
    junk = [None, "", 0, [], {}, True, "claude:", "nonsense"]
    bad = []
    for fp in durable:
        if aiproviders.is_durable_fingerprint(fp) is not True:
            bad.append("durable %r reported non-durable" % (fp,))
    for fp in legacy:
        if aiproviders.is_durable_fingerprint(fp) is not False:
            bad.append("legacy %r reported DURABLE -- a caller would trust a "
                       "timestamp as an identity" % (fp,))
    for fp in junk:
        try:
            if aiproviders.is_durable_fingerprint(fp) is not False:
                bad.append("junk %r reported durable" % (fp,))
        except Exception as e:
            bad.append("junk %r raised %s" % (fp, type(e).__name__))
    return not bad, ("%d durable / %d legacy / %d junk classified correctly"
                     % (len(durable), len(legacy), len(junk))
                     if not bad else "; ".join(bad[:3]))


def t_identity_degrades_without_cli_state():
    """No state file and no stapled oauthAccount -> legacy, flagged honestly.

    Degrading is the contract: an unidentifiable account must be reported as
    non-durable rather than guessed at, because a caller that believes a
    fabricated id would merge two real accounts.
    """
    need_identity()
    bare = no_state_home("degrade")
    cred = claude_cred(expires_at=EXP_OLD)
    bad = []
    try:
        ident = aiproviders.cred_identity("claude", cred, bare)
    except Exception as e:
        return False, "cred_identity raised %s" % type(e).__name__
    if not isinstance(ident, dict):
        return False, "cred_identity returned %r" % (ident,)
    if ident.get("fp") != LEGACY_FP_OLD:
        bad.append("fp=%r, expected the legacy string" % ident.get("fp"))
    if ident.get("durable") is not False:
        bad.append("durable=%r; a drifting fp must not claim durability"
                   % ident.get("durable"))
    if aiproviders.is_durable_fingerprint(ident.get("fp")):
        bad.append("is_durable_fingerprint disagrees with cred_identity")
    if ident.get("source") != "legacy":
        bad.append("source=%r, expected 'legacy'" % ident.get("source"))
    # An empty oauthAccount object is the same situation, not a crash.
    empty = aiproviders.cred_identity("claude",
                                      claude_cred(oauth_account={}), bare)
    if empty.get("durable") is not False:
        bad.append("empty oauthAccount claimed durability")
    # A credential path pointing at nothing at all must behave the same.
    missing = aiproviders.cred_identity(
        "claude", cred, os.path.join(TMPDIR, "homes", "absent",
                                     ".claude", ".credentials.json"))
    if missing.get("fp") != LEGACY_FP_OLD or missing.get("durable") is not \
            False:
        bad.append("absent path -> %r" % (missing,))
    return not bad, ("no identity material -> %r, durable=False, source=%r"
                     % (ident.get("fp"), ident.get("source"))
                     if not bad else "; ".join(bad))


def t_identity_junk_never_raises():
    """Totality: every entry point survives garbage without an exception.

    These functions run inside the import/duplicate/tombstone paths, where an
    exception would abort an account sweep over a single malformed file.
    """
    need_identity()
    creds = [None, 0, "", [], True, {}, {"claudeAiOauth": []},
             {"claudeAiOauth": {}}, {"claudeAiOauth": {"expiresAt": None}},
             {"claudeAiOauth": {"subscriptionType": None, "expiresAt": {}}},
             {"oauthAccount": "not-a-dict"},
             claude_cred(oauth_account={"accountUuid": 12345}),
             claude_cred(oauth_account={"accountUuid": "   "})]
    paths = [None, "", 12345, [], claude_home(account_uuid=ACCT_A, tag="junk"),
             os.path.join(TMPDIR, "homes", "nope", "x.json")]
    providers = ["claude", "codex", "cursor", "nope", None]
    bad = []
    combos = 0
    for pid in providers:
        for c in creds:
            for p in paths:
                combos += 1
                try:
                    aiproviders.cred_identity(pid, c, p)
                    aiproviders.cred_fingerprint(pid, c, p)
                    aiproviders.cred_fingerprint_candidates(pid, c, p)
                    aiproviders.cred_fingerprint_matches(pid, c, "x", p)
                    aiproviders.cred_fingerprint_matches(pid, c, None, p)
                except Exception as e:
                    bad.append("%s/%r/%r raised %s"
                               % (pid, c, p, type(e).__name__))
    # A corrupt CLI state file is a real failure mode, not a hypothetical.
    corrupt = claude_home(account_uuid=ACCT_A, tag="corrupt")
    with open(os.path.join(os.path.dirname(os.path.dirname(corrupt)),
                           ".claude.json"), "w", encoding="utf-8") as f:
        f.write("{not json at all")
    try:
        out = aiproviders.cred_identity("claude", claude_cred(), corrupt)
        if out.get("fp") != LEGACY_FP_OLD:
            bad.append("corrupt state file -> %r, expected legacy fallback"
                       % out.get("fp"))
    except Exception as e:
        bad.append("corrupt state file raised %s" % type(e).__name__)
    return not bad, ("%d junk combinations, no exception" % combos
                     if not bad else "; ".join(bad[:3]))


def t_identity_no_secret_or_email_in_fingerprint():
    """A fingerprint is stored, logged and shown; it must leak nothing.

    Fingerprints end up in the vault file, in tombstone sidecars and in
    diagnostic listings, so an email address or any token material in one is
    a privacy leak for no gain -- only equality is ever needed.
    """
    need_identity()
    fps = []
    for path in (claude_home(account_uuid=ACCT_A, tag="p1"),
                 claude_home(email=MAIL_A, tag="p2"),
                 claude_home(account_uuid=ACCT_B, email=MAIL_B, tag="p3"),
                 no_state_home("p4")):
        fps.append(aiproviders.cred_fingerprint("claude", claude_cred(), path))
        fps.extend(aiproviders.cred_fingerprint_candidates(
            "claude", claude_cred(), path))
        ident = aiproviders.cred_identity("claude", claude_cred(), path)
        fps.extend(str(v) for v in ident.values())
    fps.append(aiproviders.cred_fingerprint(
        "claude", claude_cred(oauth_account={"emailAddress": MAIL_A}), None))
    blob = " ".join(f for f in fps if isinstance(f, str)).lower()
    bad = []
    for needle in (FAKE_ACCESS, FAKE_REFRESH, "fake-access", "fake-refresh"):
        if needle.lower() in blob:
            bad.append("token material present (%r)" % needle)
    for needle in (MAIL_A, MAIL_B, "nobody.a", "nobody.b", "example.invalid",
                   "@"):
        if needle.lower() in blob:
            bad.append("email material present (%r)" % needle)
    # The email fingerprint must still WORK -- same address, same value,
    # case-insensitively -- or the digest has been made useless.
    e1 = aiproviders.cred_fingerprint("claude", claude_cred(),
                                      claude_home(email=MAIL_A, tag="pc1"))
    e2 = aiproviders.cred_fingerprint(
        "claude", claude_cred(), claude_home(email=MAIL_A.lower(), tag="pc2"))
    if e1 is None or e1 != e2:
        bad.append("email fingerprint unstable across letter case: %r vs %r"
                   % (e1, e2))
    # ...and it must genuinely be DERIVED from the address, or "no address
    # appears in the fingerprint" would be satisfied trivially by a scheme
    # that never looked at one.
    if e1 == aiproviders.cred_fingerprint("claude", claude_cred(),
                                          no_state_home("pc3")):
        bad.append("the email was never used: the fingerprint is identical "
                   "to the one produced with no account material at all "
                   "(%r), so the privacy claim here is vacuous" % (e1,))
    if not aiproviders.is_durable_fingerprint(e1):
        bad.append("email fingerprint %r is not durable" % (e1,))
    return not bad, ("%d fingerprint strings carry no address and no token "
                     "material; email digest is stable (%r)" % (len(fps), e1)
                     if not bad else "; ".join(bad[:3]))


def t_identity_same_account_two_homes_collapses():
    """The Windows copy and the WSL copy of one account are ONE account.

    The same person signed in on both sides of WSL has two credential files
    with independently-refreshed tokens. Under the legacy scheme they
    collapsed only when both files happened to hold the same expiry -- i.e.
    by luck. Identity must collapse them on purpose.
    """
    need_identity()
    win = claude_home(account_uuid=ACCT_A, email=MAIL_A, tag="win")
    wsl = claude_home(account_uuid=ACCT_A, email=MAIL_A, tag="wsl")
    # Independently refreshed: the two files disagree about expiresAt.
    cred_win = claude_cred(expires_at=EXP_OLD)
    cred_wsl = claude_cred(expires_at=EXP_NEW)
    fw = aiproviders.cred_fingerprint("claude", cred_win, win)
    fl = aiproviders.cred_fingerprint("claude", cred_wsl, wsl)
    bad = []
    if fw is None:
        bad.append("no fingerprint for the windows copy")
    if fw != fl:
        bad.append("same account seen as two: %r vs %r -- this IS the "
                   "reported bug" % (fw, fl))
    legacy_w = aiproviders.cred_fingerprint("claude", cred_win,
                                            no_state_home("lw"))
    legacy_l = aiproviders.cred_fingerprint("claude", cred_wsl,
                                            no_state_home("ll"))
    if legacy_w == legacy_l:
        bad.append("precondition failed: the two copies were not actually "
                   "distinguishable under the legacy scheme, so the collapse "
                   "proves nothing")
    # The vault's own question: is this credential already known?
    if not aiproviders.cred_fingerprint_matches("claude", cred_wsl, fw, wsl):
        bad.append("the stored windows fingerprint does not recognise the "
                   "wsl credential")
    # A stapled oauthAccount (the future import path) must agree, with no
    # file access at all.
    inline = aiproviders.cred_fingerprint(
        "claude", claude_cred(oauth_account={"accountUuid": ACCT_A}), None)
    if inline != fw:
        bad.append("stapled oauthAccount gave %r, file gave %r"
                   % (inline, fw))
    return not bad, ("two homes, two different expiries (%r vs %r) -> one "
                     "identity %r" % (legacy_w, legacy_l, fw)
                     if not bad else "; ".join(bad))


# ===========================================================================
TESTS = [
    ("00 safety: network tripwire armed at every layer", s01_tripwire_armed),
    ("00b safety: every resolved path inside the sandbox", s02_paths_sandboxed),
    ("00c safety: provider transport stubbed", s03_transport_stubbed),

    ("01 registry: twelve providers, expected slugs", t01_registry_twelve_slugs),
    ("02 registry: six new adapters promoted to live",
     t02_six_new_adapters_are_live),
    ("03 planned providers -> 'not supported yet', never raise",
     t03_planned_not_supported),
    ("04 every live provider answers fetch_usage", t04_live_adapters_respond),

    ("05 cursor: success response parsed", _cursor_success),
    ("06 copilot: paid success response parsed", _copilot_success),
    ("07 windsurf: success response parsed", _wd_success("windsurf")),
    ("08 devin: success response parsed", _wd_success("devin")),
    ("09 kimi: success response parsed", _kimi_success),
    ("10 cline: success response parsed", _cline_success),

    ("11 cursor: three failure kinds stay distinct", _classification("cursor")),
    ("12 copilot: three failure kinds stay distinct",
     _classification("copilot")),
    ("13 windsurf: three failure kinds stay distinct",
     _classification("windsurf")),
    ("14 devin: three failure kinds stay distinct", _classification("devin")),
    ("15 kimi: three failure kinds stay distinct", _classification("kimi")),
    ("16 cline: three failure kinds stay distinct", _classification("cline")),
    ("17 _classify: offline/expired/refresh-failed, bare 400 safe",
     t_classify_primitive),
    ("18 vendors without a refresh flow say so honestly",
     t_no_refresh_vendors_honest),

    ("19 fetch_usage never raises on junk input", t_junk_never_raises),
    ("20 cred_expiry -> None when the shape carries no expiry",
     t_cred_expiry_none_when_unknown),
    ("21 CRED_MAX_STALE exported", t_cred_max_stale_present),

    ("22 TRAP copilot: free-tier shape gives real numbers, not zeros",
     t_copilot_free_tier),
    ("23 TRAP windsurf: omitted percentage means 100% consumed",
     _wd_omitted_is_full("windsurf")),
    ("24 TRAP devin: omitted percentage means 100% consumed",
     _wd_omitted_is_full("devin")),
    ("25 TRAP windsurf: percentage is REMAINING, not used",
     _wd_remaining_not_used("windsurf")),
    ("26 TRAP devin: percentage is REMAINING, not used",
     _wd_remaining_not_used("devin")),
    ("27 TRAP cline: null resetsAt -> absent reset, not the epoch",
     t_cline_null_reset),
    ("28 TRAP kimi: absent window stays absent, not zero usage",
     t_kimi_absent_window_stays_absent),
    ("29 TRAP kimi: legacy count-based shape still handled",
     t_kimi_legacy_count_shape),
    ("30 TRAP cursor: user id derived from the JWT sub claim",
     t_cursor_jwt_user_id),
    ("31 TRAP cursor: malformed JWT -> error, not an exception",
     t_cursor_malformed_jwt),


    ("33 identity: fingerprint survives an expiresAt change (THE BUG)",
     t_identity_stable_across_token_refresh),
    ("34 identity: two different accounts keep two fingerprints",
     t_identity_distinct_accounts_stay_distinct),
    ("35 identity: an OLD fingerprint still matches, byte-for-byte",
     t_identity_legacy_fingerprint_back_compat),
    ("36 identity: is_durable_fingerprint separates durable from legacy",
     t_identity_durable_flag_discriminates),
    ("37 identity: no CLI state -> legacy fp, honestly non-durable",
     t_identity_degrades_without_cli_state),
    ("38 identity: junk credentials never raise", t_identity_junk_never_raises),
    ("39 identity: no email and no token material in any fingerprint",
     t_identity_no_secret_or_email_in_fingerprint),
    ("40 identity: windows + wsl copies of one account collapse to one",
     t_identity_same_account_two_homes_collapses),

    ("32 safety: zero outbound attempts for the whole run",
     s99_no_network_attempted),
]


def main():
    print("net-watch-widget :: AI provider adapter regression harness")
    print("python %s" % sys.version.split()[0])
    print("sandbox: <temp>/%s" % os.path.basename(TMPDIR))
    if _ALT:
        print("module: NW_AIPROVIDERS_DIR override in effect")
    print("")

    # ---- safety gate: abort before any adapter is called -----------------
    missing = sorted(REQUIRED_LAYERS - set(TRIPWIRE_ARMED))
    if missing:
        print("ABORT: the network kill-switch is NOT armed at %r." % missing)
        print("       Refusing to run: a real request would reach a vendor "
              "WAF with the user's own account.")
        cleanup()
        return 2
    print("safety: kill-switch armed and PROBED -- %s all refuse"
          % ", ".join(TRIPWIRE_ARMED))

    ok, detail = s02_paths_sandboxed()
    if not ok:
        print("ABORT: %s" % detail)
        print("       Refusing to run where a real credential path could be "
              "read.")
        cleanup()
        return 2
    print("safety: %s" % detail)
    print("safety: provider transport %s"
          % ("stubbed" if HTTP_STUB_INSTALLED else "NOT stubbed -- see skips"))
    if IMPORT_ERROR:
        print("import: aiproviders failed -- %s" % IMPORT_ERROR)
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
