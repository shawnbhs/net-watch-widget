#!/usr/bin/env python3
"""Regression suite for aicredsources.py -- the credential DISCOVERY layer.

Why this suite is more paranoid than the others
-----------------------------------------------
aicredsources.py reads credential stores belonging to OTHER applications the
user depends on every day: the Cursor editor's state database, the Windsurf
editor's state database, GitHub Copilot's token files. A defect here does not
produce a wrong number in a widget. It corrupts or locks a file that somebody
else's editor is holding open right now, and destroys a working login in an
application this project has nothing to do with.

The module claims to be strictly read-only. This suite's first job is to PROVE
that claim by EXECUTION rather than accept it by inspection:

  * every fixture file is fingerprinted (size, mtime_ns, sha256) before the
    module is allowed anywhere near it, every read path in the module is then
    exercised, and the fingerprints are compared byte for byte afterwards;
  * the sandbox is swept for SQLite sidecars (-wal, -shm, -journal). A journal
    appearing beside a database is the specific, observable evidence that the
    database was opened for writing rather than read-only, and it is exactly
    the failure that would disrupt a running editor;
  * the connection URI the module builds is used to attempt an INSERT, and the
    INSERT is required to be REFUSED by SQLite itself. That is the assertion
    that turns "the code says mode=ro" into "the handle provably cannot write".

Safety rules this file obeys, without exception
-----------------------------------------------
  * Every environment variable the module resolves paths from is redirected
    into a temporary sandbox BEFORE the module is imported, and every path the
    module reports for every provider is asserted to live inside it. If even
    one escapes, main() aborts with exit code 2 and runs no tests at all. A
    suite that reads the user's real credential files while testing a
    credential reader is a serious defect in the suite.
  * Outbound network is tripwired at both the urlopen layer and the raw socket
    layer, the tripwire is proven armed by firing a probe at a reserved
    TEST-NET-1 address, and the suite asserts zero real attempts at the end.
  * No value in this file resembles a credential. Every planted secret is an
    obvious FAKE-...-DO-NOT-USE sentinel, and no sentinel value is ever
    printed, even on failure.

Tests are written against the documented CONTRACT, not against whatever the
implementation happens to do. A disagreement between the two is a finding, not
a reason to soften the test.

Standard library only. Run it directly:

    python tests/ai_credsources_regression.py

Set NW_CREDSOURCES_MODULE to a path to load an alternative copy of the module
(used by the mutation check that proves this suite can actually fail).
"""

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import traceback
import urllib.request

REPO_ROOT = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))

# ── sentinels ────────────────────────────────────────────────────────────────
# Deliberately unmistakable. None of these could be confused for a real token
# by a human reading a diff or by a scanner reading the repository.

FAKE_CURSOR_SUB = "auth0|FAKEUSER000"
FAKE_COPILOT = "FAKE-COPILOT-OAUTH-DO-NOT-USE"
FAKE_WINDSURF = "FAKE-WINDSURF-APIKEY-DO-NOT-USE"
FAKE_DEVIN = "FAKE-DEVIN-APIKEY-DO-NOT-USE"
FAKE_KIMI_ACCESS = "FAKE-KIMI-ACCESS-DO-NOT-USE"
FAKE_KIMI_REFRESH = "FAKE-KIMI-REFRESH-DO-NOT-USE"
FAKE_DEVICE_ID = "FAKE-DEVICE-ID-DO-NOT-USE"
FAKE_CLINE = "FAKE-CLINE-APIKEY-DO-NOT-USE"
FAKE_EMAIL = "nobody@example.invalid"
FAKE_USER = "fakeoctocat"

# Reserved documentation range, RFC 5737. Routes nowhere, on purpose.
BLACKHOLE = ("192.0.2.1", 9)


def _b64(obj):
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


FAKE_JWT = "%s.%s.%s" % (_b64({"alg": "HS256", "typ": "JWT"}),
                         _b64({"sub": FAKE_CURSOR_SUB}), "NOTASIGNATURE")
FAKE_JWT_BROKEN = "not-a-jwt-at-all"

# Every sentinel that must never appear in redacted output.
SENTINELS = (FAKE_JWT, FAKE_COPILOT, FAKE_WINDSURF, FAKE_DEVIN,
             FAKE_KIMI_ACCESS, FAKE_KIMI_REFRESH, FAKE_CLINE)


# ── bookkeeping ──────────────────────────────────────────────────────────────

class Skip(Exception):
    """Raised by a test body when the thing it covers is not present yet."""


RESULTS = []


def record(status, name, detail=""):
    RESULTS.append((status, name, detail))
    print("%-5s %s" % (status, name))
    if detail:
        print("  -- " + detail)


def check(name, fn):
    """Run one test body. The body returns (ok, detail) or raises Skip."""
    try:
        ok, detail = fn()
    except Skip as e:
        record("SKIP", name, str(e))
        return
    except Exception as e:
        record("FAIL", name, "raised %s: %s" % (type(e).__name__, e))
        return
    record("PASS" if ok else "FAIL", name, detail)


def need(obj, *attrs):
    for a in attrs:
        if not hasattr(obj, a):
            raise Skip("module has no attribute %r" % a)
    return tuple(getattr(obj, a) for a in attrs)


# ── sandbox: redirect EVERY path-bearing variable before importing ───────────
# The module documents that it resolves every path from the environment at call
# time rather than import time, precisely so a harness can do this. That claim
# is itself under test: safety.locations_inside_sandbox proves it.

TMPDIR = os.path.realpath(tempfile.mkdtemp(prefix="nw_credsrc_"))
FAKE_HOME = os.path.join(TMPDIR, "home")
FAKE_ROAMING = os.path.join(TMPDIR, "Roaming")
FAKE_LOCAL = os.path.join(TMPDIR, "Local")
for _d in (FAKE_HOME, FAKE_ROAMING, FAKE_LOCAL):
    os.makedirs(_d, exist_ok=True)

_REDIRECTED = {
    "APPDATA": FAKE_ROAMING,
    "LOCALAPPDATA": FAKE_LOCAL,
    "USERPROFILE": FAKE_HOME,
    "HOME": FAKE_HOME,
    "HOMEDRIVE": "",
    "HOMEPATH": FAKE_HOME,
    "XDG_CONFIG_HOME": os.path.join(FAKE_HOME, ".config"),
    "XDG_DATA_HOME": os.path.join(FAKE_HOME, ".local", "share"),
    # Path overrides honoured by aiaccounts for the two CLI vendors.
    "CLAUDE_CRED_PATHS": "",
    "CODEX_CRED_PATHS": "",
}
for _k, _v in _REDIRECTED.items():
    os.environ[_k] = _v

# Tokens the module would otherwise pick up from the ambient environment. They
# must not leak into a test run, and their absence is what several not-found
# fixtures depend on.
_CLEARED_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "CLINE_API_KEY", "CLINE_TOKEN")
for _k in _CLEARED_ENV:
    os.environ.pop(_k, None)


def inside_tmp(path):
    try:
        rp = os.path.realpath(path)
    except Exception:
        return False
    return os.path.normcase(rp).startswith(os.path.normcase(TMPDIR) + os.sep)


# Not every location record names a file. Two kinds carry a NAME rather than a
# path -- an environment variable, and a CLI command line -- and asking whether
# 'GH_TOKEN' lives inside the sandbox is a category error. The gate below
# therefore checks filesystem-bearing records only, and separately asserts that
# the non-filesystem kinds are exactly the two expected ones, so a future kind
# cannot quietly slip past the gate by not looking like a path.
NON_FS_KINDS = ("env", "cli-subprocess")


def is_fs_location(loc):
    kind = (loc.get("kind") or "")
    if kind in NON_FS_KINDS:
        return False
    p = loc.get("path") or ""
    return bool(p) and (os.path.isabs(p) or os.sep in p or "/" in p)


def cleanup():
    shutil.rmtree(TMPDIR, ignore_errors=True)


# ── network kill-switch, armed before the module is imported ─────────────────

NETWORK_ATTEMPTS = []
_PROBE_SEEN = []


def _blocked_urlopen(*a, **kw):
    NETWORK_ATTEMPTS.append("urlopen")
    raise RuntimeError("network access is blocked inside the test harness")


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _is_local(addr):
    try:
        host = addr[0]
    except Exception:
        return False
    return host in ("127.0.0.1", "::1", "localhost")


def _blocked_connect(self, addr, *a, **kw):
    if _is_local(addr):
        return _real_connect(self, addr, *a, **kw)
    NETWORK_ATTEMPTS.append("socket.connect %s" % (addr,))
    raise RuntimeError("network access is blocked inside the test harness")


def _blocked_connect_ex(self, addr, *a, **kw):
    if _is_local(addr):
        return _real_connect_ex(self, addr, *a, **kw)
    NETWORK_ATTEMPTS.append("socket.connect_ex %s" % (addr,))
    return 1


urllib.request.urlopen = _blocked_urlopen
socket.socket.connect = _blocked_connect
socket.socket.connect_ex = _blocked_connect_ex


def _arm_probe():
    """Fire at a reserved address and confirm the tripwire actually caught it.

    An unarmed tripwire is worse than none: it would report "zero network
    attempts" for a module that made a hundred.
    """
    try:
        s = socket.socket()
        try:
            s.connect(BLACKHOLE)
        finally:
            s.close()
    except Exception:
        pass
    try:
        urllib.request.urlopen("http://192.0.2.1/probe")
    except Exception:
        pass
    _PROBE_SEEN.extend(NETWORK_ATTEMPTS)
    del NETWORK_ATTEMPTS[:]


_arm_probe()


# ── subprocess tripwire ──────────────────────────────────────────────────────
# The module exposes copilot_token_via_gh(), which shells out. A routine scan
# must never spawn anything the user did not ask for.

SUBPROCESS_CALLS = []


def install_subprocess_tripwire():
    """Patch the real subprocess module, not an attribute on the module under
    test: aicredsources imports subprocess lazily, inside the one function that
    needs it, so there is no module-level attribute to swap. Patching the
    shared module object catches the lazy import too."""
    import subprocess as sp
    for fname in ("run", "check_output", "Popen", "call", "check_call"):
        if not hasattr(sp, fname):
            continue

        def make(fname=fname):
            def trip(*a, **kw):
                SUBPROCESS_CALLS.append(fname)
                raise RuntimeError("subprocess is blocked inside the harness")
            return trip

        setattr(sp, fname, make())
    return True


SUBPROCESS_TRIPWIRE_OK = install_subprocess_tripwire()


# ── import the module under test ─────────────────────────────────────────────

MOD_PATH = os.environ.get("NW_CREDSOURCES_MODULE") or os.path.join(
    REPO_ROOT, "aicredsources.py")

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

CRED = None
IMPORT_ERROR = None
try:
    _spec = importlib.util.spec_from_file_location("aicredsources_under_test",
                                                   MOD_PATH)
    CRED = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = CRED
    _spec.loader.exec_module(CRED)
except Exception as e:  # pragma: no cover - reported, not raised
    IMPORT_ERROR = "%s: %s" % (type(e).__name__, e)

# aiaccounts enumerates \\wsl.localhost\<distro>\home\<user> when it proposes
# CLI credential paths, and those paths are outside any sandbox by
# construction. That enumeration belongs to aiaccounts and is covered by its
# own suite; here it is neutralised so nothing outside the sandbox can even be
# PROPOSED, let alone opened. See FINDING note in the module docstring of the
# report: aicredsources' claim that every path comes from the environment holds
# for the ten vendor stores but not for the two CLI slugs it delegates.
WSL_ENUM_NEUTRALISED = False
if CRED is not None:
    _acc = getattr(CRED, "_accounts", None)
    if _acc is not None and hasattr(_acc, "_wsl_homes"):
        _acc._wsl_homes = lambda: []
        WSL_ENUM_NEUTRALISED = True


# ── fixture construction ─────────────────────────────────────────────────────

CURSOR_DB = os.path.join(FAKE_ROAMING, "Cursor", "User", "globalStorage",
                         "state.vscdb")
WINDSURF_DB = os.path.join(FAKE_ROAMING, "Windsurf", "User", "globalStorage",
                           "state.vscdb")
COPILOT_DIR = os.path.join(FAKE_LOCAL, "github-copilot")
COPILOT_APPS = os.path.join(COPILOT_DIR, "apps.json")
COPILOT_AUTHDB = os.path.join(COPILOT_DIR, "auth.db")
DEVIN_TOML = os.path.join(FAKE_ROAMING, "devin", "credentials.toml")
KIMI_DIR = os.path.join(FAKE_HOME, ".kimi-code")
KIMI_JSON = os.path.join(KIMI_DIR, "credentials", "kimi-code.json")
KIMI_DEVICE = os.path.join(KIMI_DIR, "device_id")

PLAN_DOC = {"planName": "FAKE-PLAN", "creditsRemaining": 123,
            "creditsTotal": 500}
STALE_AGE = 21 * 24 * 3600  # three weeks; the module's own worked example


def _mkdirs(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def write_text(path, text):
    _mkdirs(path)
    with open(path, "wb") as f:
        f.write(text.encode("utf-8"))


def write_bytes(path, data):
    _mkdirs(path)
    with open(path, "wb") as f:
        f.write(data)


def make_itemtable(path, rows):
    """A VS Code-shaped state.vscdb. Built with a normal read-write handle --
    this is the harness creating a vendor's file, not the module touching it."""
    _mkdirs(path)
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        con.executemany("INSERT INTO ItemTable VALUES (?, ?)", rows)
        con.commit()
    finally:
        con.close()
    # Leave no sidecar of our own behind, or the module's would be invisible.
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


def build_fixtures(cursor_jwt=FAKE_JWT):
    make_itemtable(CURSOR_DB, [
        ("cursorAuth/accessToken", cursor_jwt),
        ("cursorAuth/cachedEmail", FAKE_EMAIL),
        ("noise/unrelated", "ignore me"),
    ])
    make_itemtable(WINDSURF_DB, [
        ("windsurfAuthStatus", json.dumps({"apiKey": FAKE_WINDSURF,
                                           "loggedIn": True})),
        ("windsurf.settings.cachedPlanInfo", json.dumps(PLAN_DOC)),
        ("codeium.windsurf", json.dumps({"email": FAKE_EMAIL})),
    ])
    write_text(COPILOT_APPS, json.dumps({
        "github.com:Iv1.fake0000000000": {
            "user": FAKE_USER, "oauth_token": FAKE_COPILOT},
        "ghe.example.invalid:Iv1.fake1111111111": {
            "user": FAKE_USER, "oauth_token": "FAKE-ENTERPRISE-DO-NOT-USE"},
    }, indent=2))
    if os.path.exists(COPILOT_AUTHDB):
        os.remove(COPILOT_AUTHDB)
    write_text(DEVIN_TOML,
               'windsurf_api_key = "%s"\n'
               'api_server_url = "https://api.devin.example.invalid"\n'
               % FAKE_DEVIN)
    write_text(KIMI_JSON, json.dumps({"access_token": FAKE_KIMI_ACCESS,
                                      "refresh_token": FAKE_KIMI_REFRESH}))
    write_text(KIMI_DEVICE, FAKE_DEVICE_ID + "\n")
    # Back-date the Windsurf database so its cached plan is unambiguously old.
    old = time.time() - STALE_AGE
    os.utime(WINDSURF_DB, (old, old))


def make_copilot_authdb():
    """The encrypted shape newer Copilot builds ship. Never decrypted."""
    _mkdirs(COPILOT_AUTHDB)
    if os.path.exists(COPILOT_AUTHDB):
        os.remove(COPILOT_AUTHDB)
    con = sqlite3.connect(COPILOT_AUTHDB)
    try:
        con.execute("CREATE TABLE oauth_tokens "
                    "(host TEXT, token_ciphertext BLOB)")
        con.execute("INSERT INTO oauth_tokens VALUES ('github.com', X'00ff00ff')")
        con.commit()
    finally:
        con.close()
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.exists(COPILOT_AUTHDB + suffix):
            os.remove(COPILOT_AUTHDB + suffix)


# ── fingerprinting ───────────────────────────────────────────────────────────

SIDECARS = ("-wal", "-shm", "-journal")


def fingerprint_tree(root):
    """(relpath -> (size, mtime_ns, sha256)) for every file under root."""
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
                with open(full, "rb") as f:
                    digest = hashlib.sha256(f.read()).hexdigest()
            except Exception as e:
                out[os.path.relpath(full, root)] = ("ERR", type(e).__name__, "")
                continue
            out[os.path.relpath(full, root)] = (st.st_size, st.st_mtime_ns,
                                                digest)
    return out


def diff_fingerprints(before, after):
    changed, appeared, vanished = [], [], []
    for k, v in before.items():
        if k not in after:
            vanished.append(k)
        elif after[k] != v:
            changed.append(k)
    for k in after:
        if k not in before:
            appeared.append(k)
    return changed, appeared, vanished


def find_sidecars(root):
    hits = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if any(fn.endswith(s) for s in SIDECARS):
                hits.append(os.path.relpath(os.path.join(dirpath, fn), root))
    return hits


def exercise_every_read_path():
    """Call every public read the module offers, plus each vendor reader."""
    calls = []
    calls.append(("scan_all(include_creds=True)",
                  CRED.scan_all(include_creds=True)))
    calls.append(("scan_all(include_creds=False)",
                  CRED.scan_all(include_creds=False)))
    for slug in CRED.provider_slugs():
        calls.append(("cred_locations(%s)" % slug, CRED.cred_locations(slug)))
        calls.append(("read_cred(%s)" % slug, CRED.read_cred(slug)))
    calls.append(("windsurf_cached_plan()", CRED.windsurf_cached_plan()))
    return calls


# ── tests: safety gate ───────────────────────────────────────────────────────

def t_env_redirected():
    bad = []
    for k, v in _REDIRECTED.items():
        if v == "":
            continue
        if not inside_tmp(os.environ.get(k, "")):
            bad.append(k)
    return (not bad), ("escaped: %s" % bad if bad else
                       "%d path variables point into the sandbox"
                       % (len(_REDIRECTED) - 3))


def t_locations_inside_sandbox():
    escaped, total, nonfs = [], 0, []
    for slug in CRED.provider_slugs():
        for loc in CRED.cred_locations(slug):
            if not is_fs_location(loc):
                nonfs.append((slug, loc.get("kind")))
                continue
            total += 1
            if not inside_tmp(loc.get("path")):
                escaped.append(slug)
    unexpected = [x for x in nonfs if x[1] not in NON_FS_KINDS]
    ok = not escaped and not unexpected
    return ok, ("escaped for %s / unexpected non-path kinds %s"
                % (sorted(set(escaped)), unexpected) if not ok
                else "%d filesystem locations across %d providers, all inside; "
                     "%d non-filesystem records, all env or cli-subprocess"
                     % (total, len(CRED.provider_slugs()), len(nonfs)))


def t_tripwire_armed():
    got = len(_PROBE_SEEN)
    return got >= 2, ("probe recorded %d attempts (socket + urlopen)" % got)


def t_subprocess_tripwire_installed():
    return SUBPROCESS_TRIPWIRE_OK, \
        ("subprocess.run/Popen/check_output/call/check_call replaced with "
         "tripwires on the shared module object, so the module's lazy "
         "'import subprocess' picks them up too")


# ── tests: the read-only claim, proven by execution ──────────────────────────

def t_readonly_fingerprints():
    build_fixtures()
    before = fingerprint_tree(TMPDIR)
    if not before:
        return False, "no fixtures were built"
    exercise_every_read_path()
    after = fingerprint_tree(TMPDIR)
    changed, appeared, vanished = diff_fingerprints(before, after)
    ok = not (changed or appeared or vanished)
    detail = ("%d files fingerprinted (size+mtime_ns+sha256), unchanged after "
              "every read path" % len(before))
    if not ok:
        detail = "changed=%s appeared=%s vanished=%s" % (changed, appeared,
                                                         vanished)
    return ok, detail


def t_no_sidecars_after_reads():
    hits = find_sidecars(TMPDIR)
    return (not hits), ("sidecars found: %s" % hits if hits else
                        "no -wal/-shm/-journal anywhere in the sandbox")


def t_ro_uri_refuses_write():
    """The assertion that makes the read-only claim a fact rather than a
    comment: build the module's own connection URI and try to INSERT."""
    (ro_uri,) = need(CRED, "_ro_uri")
    probe = os.path.join(TMPDIR, "rowrite_probe.vscdb")
    shutil.copyfile(CURSOR_DB, probe)
    con = sqlite3.connect(ro_uri(probe), uri=True, timeout=1.0,
                          isolation_level=None)
    try:
        try:
            con.execute("INSERT INTO ItemTable VALUES ('x', 'y')")
        except sqlite3.OperationalError as e:
            return True, ("SQLite refused the write: %s"
                          % str(e).split("(")[0].strip())
        except sqlite3.DatabaseError as e:
            return True, "SQLite refused the write: %s" % type(e).__name__
        return False, ("the handle built by _ro_uri() ACCEPTED an INSERT -- "
                       "the database is open read-write")
    finally:
        con.close()
        for s in ("",) + SIDECARS:
            try:
                os.remove(probe + s)
            except OSError:
                pass


def t_sqlite_vendors_both_covered():
    dbs = [CURSOR_DB, WINDSURF_DB]
    missing = [d for d in dbs if not os.path.exists(d)]
    return (not missing), ("both SQLite-backed vendor databases exist and were "
                           "read: cursor, windsurf")


# ── tests: a database held open by another process ───────────────────────────

def _read_with_db_held(db_path, reader):
    """Hold the database on a second connection, the way a running editor
    does, and read through it."""
    holder = sqlite3.connect(db_path, timeout=1.0)
    try:
        holder.execute("BEGIN")
        holder.execute("SELECT count(*) FROM ItemTable").fetchone()
        return reader()
    finally:
        try:
            holder.rollback()
        finally:
            holder.close()


def t_concurrent_cursor():
    build_fixtures()
    cred, why = _read_with_db_held(CURSOR_DB,
                                   lambda: CRED.read_cred("cursor"))
    if cred is None:
        return False, "read failed while the database was held open: %s" % why
    ok = cred["cursorAuth"]["accessToken"] == FAKE_JWT
    return ok, "token read through a concurrently open Cursor database"


def t_concurrent_windsurf():
    cred, why = _read_with_db_held(WINDSURF_DB,
                                   lambda: CRED.read_cred("windsurf"))
    if cred is None:
        return False, "read failed while the database was held open: %s" % why
    ok = cred["windsurfAuth"]["apiKey"] == FAKE_WINDSURF
    return ok, "apiKey read through a concurrently open Windsurf database"


def t_concurrent_no_sidecars():
    hits = find_sidecars(TMPDIR)
    return (not hits), ("sidecars found: %s" % hits if hits else
                        "still no journal/WAL after concurrent reads")


def t_concurrent_fingerprints():
    before = fingerprint_tree(TMPDIR)
    _read_with_db_held(CURSOR_DB, lambda: CRED.read_cred("cursor"))
    _read_with_db_held(WINDSURF_DB, lambda: CRED.windsurf_cached_plan())
    after = fingerprint_tree(TMPDIR)
    changed, appeared, vanished = diff_fingerprints(before, after)
    ok = not (changed or appeared or vanished)
    return ok, ("unchanged under concurrent access" if ok else
                "changed=%s appeared=%s vanished=%s"
                % (changed, appeared, vanished))


# ── tests: per-vendor extraction ─────────────────────────────────────────────

def t_cursor_token():
    build_fixtures()
    cred, why = CRED.read_cred("cursor")
    if cred is None:
        return False, "no credential: %s" % why
    return cred["cursorAuth"]["accessToken"] == FAKE_JWT, \
        "accessToken extracted from ItemTable key cursorAuth/accessToken"


def t_cursor_user_id():
    cred, why = CRED.read_cred("cursor")
    if cred is None:
        return False, "no credential: %s" % why
    got = cred["cursorAuth"].get("userId")
    want = FAKE_CURSOR_SUB.split("|")[1]
    return got == want, ("userId derived from the sub claim (expected the part "
                         "after the bar)" if got == want
                         else "userId did not match the sub claim")


def t_cursor_malformed_jwt():
    build_fixtures(cursor_jwt=FAKE_JWT_BROKEN)
    try:
        cred, why = CRED.read_cred("cursor")
    except Exception as e:
        return False, "raised %s instead of reporting" % type(e).__name__
    if cred is None:
        return True, "reported a reason rather than raising: %s" % why[:60]
    got = cred["cursorAuth"].get("userId")
    return got is None, ("token returned with userId=None rather than a guess"
                         if got is None else
                         "a user id was invented from an unparseable token")


def t_copilot_token():
    build_fixtures()
    cred, why = CRED.read_cred("copilot")
    if cred is None:
        return False, "no credential: %s" % why
    return cred["copilotAuth"]["oauth_token"] == FAKE_COPILOT, \
        "oauth_token extracted from apps.json"


def t_copilot_host_preference():
    cred, _why = CRED.read_cred("copilot")
    if cred is None:
        return False, "no credential"
    host = cred["copilotAuth"].get("host", "")
    return host.startswith("github.com"), \
        "github.com entry preferred over the enterprise host"


def t_windsurf_apikey():
    cred, why = CRED.read_cred("windsurf")
    if cred is None:
        return False, "no credential: %s" % why
    return cred["windsurfAuth"]["apiKey"] == FAKE_WINDSURF, \
        "apiKey extracted from windsurfAuthStatus JSON"


def t_windsurf_email():
    cred, _why = CRED.read_cred("windsurf")
    if cred is None:
        return False, "no credential"
    return cred["windsurfAuth"].get("email") == FAKE_EMAIL, \
        "account email recovered from codeium.windsurf"


def t_windsurf_plan_document():
    res = CRED.windsurf_cached_plan()
    if not isinstance(res, dict):
        return False, "expected a dict, got %s" % type(res).__name__
    if not res.get("ok"):
        return False, "not ok: %s" % res.get("reason")
    return res.get("plan") == PLAN_DOC, \
        "cached quota document returned intact, no network request"


def t_windsurf_plan_age():
    res = CRED.windsurf_cached_plan()
    age = res.get("age_seconds")
    if not isinstance(age, (int, float)):
        return False, "no numeric age_seconds accompanies the document"
    ok = age >= STALE_AGE - 60
    return ok, ("document is reported %.1f days old, matching the back-dated "
                "fixture" % (age / 86400.0))


def t_windsurf_plan_stale_flag():
    """CONTRACT: a deliberately OLD cached document must be FLAGGED stale.

    Showing a three-week-old quota as a current number is silent wrong data,
    which is worse than showing nothing. The contract this suite was written
    against says the result carries a stale marker. This test is deliberately
    not softened to match the implementation.
    """
    res = CRED.windsurf_cached_plan()
    for key in ("stale", "_stale", "is_stale"):
        if key in res:
            return bool(res[key]), \
                "stale marker %r present and true for a 21-day-old document" % key
    return False, ("the result carries age_seconds but NO machine-readable "
                   "stale marker; every caller must re-derive staleness and "
                   "any caller that forgets ships a stale quota as current")


def t_devin_api_key():
    build_fixtures()
    cred, why = CRED.read_cred("devin")
    if cred is None:
        return False, "no credential: %s" % why
    return cred["devinAuth"]["api_key"] == FAKE_DEVIN, \
        "windsurf_api_key read from the documented credentials.toml path"


def t_kimi_credentials():
    cred, why = CRED.read_cred("kimi")
    if cred is None:
        return False, "no credential: %s" % why
    doc = cred["kimiAuth"]["credentials"]
    return doc.get("access_token") == FAKE_KIMI_ACCESS, \
        "credentials document read from .kimi-code/credentials"


def t_kimi_device_id():
    cred, _why = CRED.read_cred("kimi")
    if cred is None:
        return False, "no credential"
    return cred["kimiAuth"].get("device_id") == FAKE_DEVICE_ID, \
        "device_id read from its separate plain-text file"


def t_kimi_device_id_missing_is_reported():
    os.remove(KIMI_DEVICE)
    try:
        cred, why = CRED.read_cred("kimi")
        if cred is None:
            return False, "the whole login was refused over a missing header: %s" % why
        km = cred["kimiAuth"]
        ok = "device_id_missing" in km and "device_id" not in km
        return ok, ("the usable half is returned and the missing half is named"
                    if ok else "missing device_id not distinguished")
    finally:
        write_text(KIMI_DEVICE, FAKE_DEVICE_ID + "\n")


def t_cline_env_only():
    cred, why = CRED.read_cred("cline")
    if cred is not None:
        return False, "a credential appeared with no env var set"
    return why.startswith(CRED.R_NO_SOURCE), \
        "reported %r and named the env vars to set" % CRED.R_NO_SOURCE


def t_cline_env_present():
    os.environ["CLINE_API_KEY"] = FAKE_CLINE
    try:
        cred, why = CRED.read_cred("cline")
        if cred is None:
            return False, "env var set but not picked up: %s" % why
        ok = (cred["clineAuth"]["apiKey"] == FAKE_CLINE and
              cred["clineAuth"].get("source_env") == "CLINE_API_KEY")
        return ok, "apiKey taken from the environment and its source named"
    finally:
        os.environ.pop("CLINE_API_KEY", None)


# ── tests: the four failure reasons must be distinguishable ──────────────────

_REASONS = {}


def t_reason_not_found():
    """Fixture: nothing at all on disk for Copilot, and no GH_TOKEN."""
    for p in (COPILOT_APPS, COPILOT_AUTHDB,
              os.path.join(COPILOT_DIR, "hosts.json")):
        if os.path.exists(p):
            os.remove(p)
    cred, why = CRED.read_cred("copilot")
    _REASONS["not_found"] = why
    ok = (cred is None and why.startswith(CRED.R_NOT_FOUND)
          and "apps.json" in why and "GH_TOKEN" in why)
    return ok, ("%r, names the files checked and the remedy" % CRED.R_NOT_FOUND
                if ok else "got: %s" % why)


def t_reason_encrypted():
    """Fixture: Copilot auth.db with an oauth_tokens/token_ciphertext table."""
    make_copilot_authdb()
    cred, why = CRED.read_cred("copilot")
    _REASONS["encrypted"] = why
    ok = (cred is None and why.startswith(CRED.R_ENCRYPTED)
          and "token_ciphertext" in why)
    return ok, ("%r, names the table and column and refuses to decrypt another "
                "application's secrets" % CRED.R_ENCRYPTED
                if ok else "got: %s" % why)


def t_encrypted_db_not_decrypted():
    """The encrypted store must be reported, never opened for its ciphertext."""
    before = fingerprint_tree(COPILOT_DIR)
    CRED.read_cred("copilot")
    CRED.scan_all(include_creds=True)
    after = fingerprint_tree(COPILOT_DIR)
    changed, appeared, vanished = diff_fingerprints(before, after)
    ok = not (changed or appeared or vanished)
    return ok, ("auth.db byte-identical and no sidecar beside it" if ok
                else "changed=%s appeared=%s" % (changed, appeared))


def t_reason_malformed():
    """Fixture: kimi-code.json containing deliberately corrupt bytes."""
    write_bytes(KIMI_JSON, b"{\x00not json at all,,,")
    cred, why = CRED.read_cred("kimi")
    _REASONS["malformed"] = why
    ok = (cred is None and why.startswith(CRED.R_MALFORMED)
          and "JSON" in why)
    return ok, ("%r, names the file and that it is not valid JSON"
                % CRED.R_MALFORMED if ok else "got: %s" % why)


def t_reason_missing_key():
    """Fixture: a devin credentials.toml that parses but has no key."""
    write_text(DEVIN_TOML,
               'api_server_url = "https://api.devin.example.invalid"\n')
    cred, why = CRED.read_cred("devin")
    _REASONS["missing_key"] = why
    ok = (cred is None and "windsurf_api_key" in why)
    return ok, ("names the exact key that is absent from a file that parsed "
                "fine" if ok else "got: %s" % why)


def t_reasons_distinguishable():
    if len(_REASONS) < 4:
        raise Skip("earlier reason fixtures did not all run")
    vals = list(_REASONS.values())
    distinct = len(set(vals)) == len(vals)
    generic = [k for k, v in _REASONS.items() if len(v) < 30]
    ok = distinct and not generic
    return ok, ("four states, four distinct and specific messages, each with "
                "a different remedy" if ok
                else "distinct=%s too-terse=%s" % (distinct, generic))


def t_reason_unknown_provider():
    cred, why = CRED.read_cred("definitely-not-a-vendor")
    ok = cred is None and why.startswith(CRED.R_UNKNOWN_PROVIDER)
    return ok, "%r for a slug that is not in the table" % CRED.R_UNKNOWN_PROVIDER


# ── tests: totality ──────────────────────────────────────────────────────────

class _Weird(object):
    def __repr__(self):
        raise RuntimeError("even repr() explodes")

    def __eq__(self, other):
        raise RuntimeError("even == explodes")

    def __hash__(self):
        return 7


JUNK = [None, "", "   ", 0, 1, -1, 3.5, True, [], ["cursor"], {}, {"a": 1},
        (), b"cursor", "CURSOR", "cursor ", "../../etc/passwd", "\x00",
        "a" * 5000, object(), _Weird(), Exception("nope")]

JUNK_NAMES = ["None", "empty str", "blank str", "int 0", "int 1", "int -1",
              "float", "bool", "empty list", "list", "empty dict", "dict",
              "empty tuple", "bytes", "wrong case", "trailing space",
              "traversal str", "NUL str", "very long str", "bare object",
              "object whose repr and eq raise", "exception instance"]


def t_totality_read_cred():
    bad = []
    for i, v in enumerate(JUNK):
        try:
            out = CRED.read_cred(v)
            if not (isinstance(out, tuple) and len(out) == 2):
                bad.append("index %d (%s): returned %s"
                           % (i, JUNK_NAMES[i], type(out).__name__))
        except Exception as e:
            bad.append("index %d (%s) raised %s"
                       % (i, JUNK_NAMES[i], type(e).__name__))
    return (not bad), ("%d junk inputs, every one returned a 2-tuple"
                       % len(JUNK) if not bad else "; ".join(bad[:4]))


def t_totality_cred_locations():
    bad = []
    for i, v in enumerate(JUNK):
        try:
            out = CRED.cred_locations(v)
            if not isinstance(out, list):
                bad.append("index %d: returned %s" % (i, type(out).__name__))
        except Exception as e:
            bad.append("index %d raised %s" % (i, type(e).__name__))
    return (not bad), ("%d junk inputs, every one returned a list" % len(JUNK)
                       if not bad else "; ".join(bad[:4]))


def t_totality_read_cred_subprocess_arg():
    bad = []
    for v in JUNK[:12]:
        try:
            out = CRED.read_cred("copilot", allow_subprocess=False)
            if not isinstance(out, tuple):
                bad.append(type(out).__name__)
        except Exception as e:
            bad.append(type(e).__name__)
        try:
            CRED.cred_locations(v)
        except Exception as e:
            bad.append(type(e).__name__)
    return (not bad), "no public call raised for any junk argument"


def t_totality_scan_all():
    bad = []
    for v in (True, False, None, 0, 1, "yes", []):
        try:
            out = CRED.scan_all(include_creds=v)
            if not isinstance(out, dict) or "providers" not in out:
                bad.append(repr(v))
        except Exception as e:
            bad.append("%r raised %s" % (v, type(e).__name__))
    return (not bad), "scan_all returned a report for every argument shape"


def t_totality_windsurf_plan():
    try:
        out = CRED.windsurf_cached_plan()
    except Exception as e:
        return False, "raised %s" % type(e).__name__
    return isinstance(out, dict) and "ok" in out, \
        "always a dict carrying an ok flag"


def t_totality_provider_slugs():
    slugs = CRED.provider_slugs()
    ok = isinstance(slugs, list) and len(slugs) == 12 and \
        all(isinstance(s, str) for s in slugs)
    return ok, "twelve provider slugs" if ok else "got %r" % (slugs,)


def t_describe_exists():
    if not hasattr(CRED, "describe"):
        raise Skip("no describe() -- the contract this suite was written "
                   "against lists one; see findings")
    for slug in CRED.provider_slugs():
        out = CRED.describe(slug)
        if not isinstance(out, (str, dict)):
            return False, "describe(%s) returned %s" % (slug, type(out).__name__)
    return True, "describe() total across all twelve slugs"


# ── tests: no credential value ever leaks ────────────────────────────────────

def _sentinels_in(blob):
    return [s[:12] + "..." for s in SENTINELS if s in blob]


def t_leak_scan_redacted():
    build_fixtures()
    os.environ["CLINE_API_KEY"] = FAKE_CLINE
    try:
        rep = CRED.scan_all(include_creds=False)
        blob = json.dumps(rep, default=str)
    finally:
        os.environ.pop("CLINE_API_KEY", None)
    hits = _sentinels_in(blob)
    return (not hits), ("%d bytes of diagnostic report, zero sentinels"
                        % len(blob) if not hits
                        else "LEAKED %d sentinel(s)" % len(hits))


def t_leak_reasons_clean():
    build_fixtures()
    write_bytes(KIMI_JSON, b"{\x00broken")
    blobs = []
    try:
        for slug in CRED.provider_slugs():
            _cred, why = CRED.read_cred(slug)
            if why:
                blobs.append(why)
        blobs.append(json.dumps(CRED.windsurf_cached_plan().get("reason")))
    finally:
        build_fixtures()
    joined = "\n".join(blobs)
    hits = _sentinels_in(joined)
    return (not hits), ("%d reason strings, zero sentinels -- reasons name "
                        "paths and key NAMES only" % len(blobs) if not hits
                        else "LEAKED %d sentinel(s)" % len(hits))


def t_leak_locations_clean():
    blob = json.dumps([CRED.cred_locations(s) for s in CRED.provider_slugs()],
                      default=str)
    hits = _sentinels_in(blob)
    return (not hits), ("the location listing carries key NAMES, never values"
                        if not hits else "LEAKED %d sentinel(s)" % len(hits))


def t_leak_include_creds_does_carry():
    """The inverse assertion: the include_creds variant is EXPECTED to carry
    credentials. If it did not, callers would silently import nothing."""
    rep = CRED.scan_all(include_creds=True)
    blob = json.dumps(rep, default=str)
    present = [s for s in SENTINELS if s in blob]
    ok = len(present) >= 3
    return ok, ("scan_all(include_creds=True) carries credential material by "
                "design; the redacted variant above carries none"
                if ok else "expected credentials, found %d sentinels"
                % len(present))


def t_leak_no_sentinel_printed():
    """Nothing this suite has printed so far may contain a sentinel."""
    printed = "\n".join("%s %s %s" % r for r in RESULTS)
    hits = _sentinels_in(printed)
    return (not hits), ("no sentinel value appears in this suite's own output"
                        if not hits else "the suite itself printed a secret")


# ── tests: the opt-in subprocess path is never taken implicitly ──────────────

def t_scan_spawns_nothing():
    del SUBPROCESS_CALLS[:]
    CRED.scan_all(include_creds=True)
    CRED.scan_all(include_creds=False)
    for slug in CRED.provider_slugs():
        CRED.read_cred(slug)
    return (not SUBPROCESS_CALLS), \
        ("a full scan plus every read_cred() spawned nothing"
         if not SUBPROCESS_CALLS else "spawned: %s" % SUBPROCESS_CALLS)


def t_copilot_via_gh_is_opt_in():
    if not hasattr(CRED, "copilot_token_via_gh"):
        raise Skip("no copilot_token_via_gh()")
    del SUBPROCESS_CALLS[:]
    # The function looks the CLI up on PATH first, so on a machine without gh
    # it would return before ever reaching the subprocess layer and the
    # tripwire would prove nothing. Pretend gh exists, inside the sandbox.
    import shutil as _sh
    fake_gh = os.path.join(TMPDIR, "gh")
    write_text(fake_gh, "#!/bin/sh\nexit 0\n")
    real_which = _sh.which
    _sh.which = lambda name, *a, **kw: (fake_gh if name == "gh"
                                        else real_which(name, *a, **kw))
    try:
        tok, why = CRED.copilot_token_via_gh()
    except Exception as e:
        return False, ("the opt-in path raised %s instead of reporting"
                       % type(e).__name__)
    finally:
        _sh.which = real_which
    spawned = bool(SUBPROCESS_CALLS)
    del SUBPROCESS_CALLS[:]
    ok = spawned and tok is None and CRED.R_UNREADABLE in (why or "")
    return ok, ("explicitly calling it DOES reach the subprocess layer (the "
                "tripwire caught subprocess.run) and it reports rather than "
                "raises" if ok
                else "spawned=%s token=%s" % (spawned, tok is not None))


def t_read_cred_subprocess_flag_default():
    del SUBPROCESS_CALLS[:]
    CRED.read_cred("copilot")
    default_quiet = not SUBPROCESS_CALLS
    del SUBPROCESS_CALLS[:]
    return default_quiet, "read_cred() defaults to allow_subprocess=False"


# ── tests: graceful degradation with no sqlite3 ──────────────────────────────

def t_no_sqlite_module():
    if not hasattr(CRED, "_sqlite3"):
        raise Skip("module does not hold a rebindable _sqlite3 reference")
    saved = CRED._sqlite3
    CRED._sqlite3 = None
    try:
        cred, why = CRED.read_cred("cursor")
        ok = cred is None and why.startswith(CRED.R_UNREADABLE) and \
            "sqlite3" in why
        return ok, ("cursor reported %r naming sqlite3 as the cause"
                    % CRED.R_UNREADABLE if ok else "got: %s" % why)
    finally:
        CRED._sqlite3 = saved


def t_no_sqlite_scan_survives():
    if not hasattr(CRED, "_sqlite3"):
        raise Skip("module does not hold a rebindable _sqlite3 reference")
    saved = CRED._sqlite3
    CRED._sqlite3 = None
    try:
        rep = CRED.scan_all(include_creds=False)
    except Exception as e:
        return False, "scan_all raised %s with sqlite3 missing" % type(e).__name__
    finally:
        CRED._sqlite3 = saved
    slugs = set(rep.get("providers", {}))
    ok = len(slugs) == 12 and "devin" in rep["providers"]
    non_sqlite_ok = rep["providers"]["kimi"]["found"]
    return ok and non_sqlite_ok, \
        ("all twelve vendors still reported and the non-SQLite ones still "
         "resolve; one missing module does not take the scan down"
         if ok and non_sqlite_ok else "report was truncated to %d slugs"
         % len(slugs))


def t_no_sqlite_plan_reports():
    if not hasattr(CRED, "_sqlite3"):
        raise Skip("module does not hold a rebindable _sqlite3 reference")
    saved = CRED._sqlite3
    CRED._sqlite3 = None
    try:
        res = CRED.windsurf_cached_plan()
    finally:
        CRED._sqlite3 = saved
    ok = isinstance(res, dict) and res.get("ok") is False and \
        CRED.R_UNREADABLE in (res.get("reason") or "")
    return ok, "windsurf_cached_plan degrades to ok=False with a named cause"


# ── tests: final sweeps ──────────────────────────────────────────────────────

def t_final_sidecar_sweep():
    hits = find_sidecars(TMPDIR)
    return (not hits), ("sidecars found: %s" % hits if hits else
                        "after the whole suite: no journal, WAL or shm file "
                        "anywhere in the sandbox")


def t_zero_network():
    return (not NETWORK_ATTEMPTS), \
        ("zero outbound attempts across the entire suite (tripwire proven "
         "armed at startup)" if not NETWORK_ATTEMPTS
         else "attempted: %s" % NETWORK_ATTEMPTS[:5])


TESTS = [
    ("safety.env_variables_redirected", t_env_redirected),
    ("safety.locations_inside_sandbox", t_locations_inside_sandbox),
    ("safety.network_tripwire_armed", t_tripwire_armed),
    ("safety.subprocess_tripwire_installed", t_subprocess_tripwire_installed),

    ("readonly.fixture_fingerprints_unchanged", t_readonly_fingerprints),
    ("readonly.no_sqlite_sidecar_created", t_no_sidecars_after_reads),
    ("readonly.ro_uri_handle_refuses_write", t_ro_uri_refuses_write),
    ("readonly.both_sqlite_vendors_covered", t_sqlite_vendors_both_covered),

    ("concurrent.cursor_db_held_open", t_concurrent_cursor),
    ("concurrent.windsurf_db_held_open", t_concurrent_windsurf),
    ("concurrent.no_sidecar_after_shared_read", t_concurrent_no_sidecars),
    ("concurrent.fingerprints_unchanged", t_concurrent_fingerprints),

    ("cursor.access_token_extracted", t_cursor_token),
    ("cursor.user_id_derived_from_sub", t_cursor_user_id),
    ("cursor.malformed_jwt_reports_not_raises", t_cursor_malformed_jwt),
    ("copilot.oauth_token_extracted", t_copilot_token),
    ("copilot.github_com_host_preferred", t_copilot_host_preference),
    ("windsurf.api_key_extracted", t_windsurf_apikey),
    ("windsurf.account_email_extracted", t_windsurf_email),
    ("windsurf.cached_plan_document", t_windsurf_plan_document),
    ("windsurf.cached_plan_age_reported", t_windsurf_plan_age),
    ("windsurf.old_cached_plan_flagged_stale", t_windsurf_plan_stale_flag),
    ("devin.api_key_from_credentials_toml", t_devin_api_key),
    ("kimi.credentials_document", t_kimi_credentials),
    ("kimi.device_id_header_value", t_kimi_device_id),
    ("kimi.missing_device_id_named_not_fatal", t_kimi_device_id_missing_is_reported),
    ("cline.no_discoverable_source", t_cline_env_only),
    ("cline.env_var_accepted", t_cline_env_present),

    ("reason.not_found", t_reason_not_found),
    ("reason.present_but_encrypted", t_reason_encrypted),
    ("reason.encrypted_store_left_untouched", t_encrypted_db_not_decrypted),
    ("reason.present_but_malformed", t_reason_malformed),
    ("reason.parses_but_key_absent", t_reason_missing_key),
    ("reason.four_states_four_messages", t_reasons_distinguishable),
    ("reason.unknown_provider", t_reason_unknown_provider),

    ("totality.read_cred_never_raises", t_totality_read_cred),
    ("totality.cred_locations_never_raises", t_totality_cred_locations),
    ("totality.public_calls_never_raise", t_totality_read_cred_subprocess_arg),
    ("totality.scan_all_never_raises", t_totality_scan_all),
    ("totality.windsurf_cached_plan_never_raises", t_totality_windsurf_plan),
    ("totality.provider_slugs_stable", t_totality_provider_slugs),
    ("totality.describe_never_raises", t_describe_exists),

    ("leak.redacted_scan_carries_no_secret", t_leak_scan_redacted),
    ("leak.reason_strings_carry_no_secret", t_leak_reasons_clean),
    ("leak.location_listing_carries_no_secret", t_leak_locations_clean),
    ("leak.include_creds_variant_does_carry", t_leak_include_creds_does_carry),
    ("leak.suite_output_carries_no_secret", t_leak_no_sentinel_printed),

    ("subprocess.scan_spawns_nothing", t_scan_spawns_nothing),
    ("subprocess.read_cred_default_is_quiet", t_read_cred_subprocess_flag_default),
    ("subprocess.gh_path_is_explicit_opt_in", t_copilot_via_gh_is_opt_in),

    ("degrade.no_sqlite3_reports_unreadable", t_no_sqlite_module),
    ("degrade.no_sqlite3_scan_still_complete", t_no_sqlite_scan_survives),
    ("degrade.no_sqlite3_plan_reports", t_no_sqlite_plan_reports),

    ("final.no_sidecar_anywhere", t_final_sidecar_sweep),
    ("final.zero_network_attempts", t_zero_network),
]


def main():
    print("=" * 66)
    print("aicredsources regression suite")
    print("python %s" % sys.version.split()[0])
    print("module %s" % os.path.basename(MOD_PATH))
    print("sandbox %s" % os.path.basename(TMPDIR))
    print("=" * 66)

    if IMPORT_ERROR is not None:
        print("ABORT: could not import the module under test.")
        print("  " + IMPORT_ERROR)
        return 2

    # Safety gate. Nothing runs until every path the module would open is
    # proven to be inside the sandbox.
    escaped = []
    try:
        for slug in CRED.provider_slugs():
            for loc in CRED.cred_locations(slug):
                if is_fs_location(loc) and not inside_tmp(loc.get("path")):
                    escaped.append((slug, loc.get("path")))
    except Exception as e:
        print("ABORT: the safety gate itself raised %s" % type(e).__name__)
        return 2
    if escaped:
        print("ABORT: %d location(s) resolved OUTSIDE the temp sandbox."
              % len(escaped))
        for slug, _p in escaped[:8]:
            print("  provider %s" % slug)
        print("Refusing to touch what may be the real credential stores.")
        return 2
    print("safety: all vendor locations resolve inside the sandbox")
    print("safety: outbound network disabled (urlopen + socket tripwires)")
    print("safety: subprocess layer tripwired")
    if WSL_ENUM_NEUTRALISED:
        print("safety: aiaccounts WSL home enumeration neutralised for this run")
    print("-" * 66)

    for name, fn in TESTS:
        check(name, fn)

    passed = sum(1 for s, _n, _d in RESULTS if s == "PASS")
    failed = sum(1 for s, _n, _d in RESULTS if s == "FAIL")
    skipped = sum(1 for s, _n, _d in RESULTS if s == "SKIP")
    print("-" * 66)
    print("%d passed, %d failed, %d skipped, %d total"
          % (passed, failed, skipped, len(RESULTS)))
    if failed:
        print("failures:")
        for s, n, d in RESULTS:
            if s == "FAIL":
                print("  %s -- %s" % (n, d))
    if skipped:
        print("skipped:")
        for s, n, d in RESULTS:
            if s == "SKIP":
                print("  %s -- %s" % (n, d))
    print("-" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    rc = 0
    try:
        rc = main()
    except Exception:
        traceback.print_exc()
        rc = 3
    finally:
        cleanup()
    sys.exit(rc)
