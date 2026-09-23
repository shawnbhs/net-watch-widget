#!/usr/bin/env python3
"""Standalone regression harness for the isolated per-account login sandbox.

Runs with a bare interpreter: standard library only, no pytest, no psutil.

    python tests/ai_login_sandbox_regression.py

What it covers
--------------
ailogin.py -- the module that gives every interactive login its own disposable
fake home, so a CLI that stores exactly ONE session cannot silently hand back
the account that is already signed in when the user asks for the second one.

The contract under test, expressed as roles rather than names (the harness
resolves each role against the module, so a rename is reported instead of
crashing the run):

  capability    provider slug -> bool
  create        -> handle carrying the sandbox dir, the environment overrides
                   and the expected credential path
  launch        (provider, handle) -> process handle
  wait          (handle, timeout) -> a captured credential, OR a reason that
                   distinguishes cancellation from timeout from CLI error
  teardown      (handle) -> sandbox and everything in it removed
  duplicate     (captured cred, registered accounts) -> new vs same account
  path guard    refuses a path inside a real home configuration directory

Why this harness is written so defensively
------------------------------------------
The code under test exists in order to touch live logins, and the machine that
runs it holds real ones. Five safety layers are installed BEFORE anything
under test is imported, and each one is asserted rather than assumed:

  1. One throwaway temp tree. AI_ACCOUNTS_FILE plus HOME, USERPROFILE,
     HOMEDRIVE, HOMEPATH, APPDATA, LOCALAPPDATA and XDG_CONFIG_HOME are
     redirected into a fake home inside it, and tempfile.tempdir is pointed
     inside it too -- otherwise the module's own mkdtemp would put sandboxes
     in the real system temp directory, outside the tree this harness can
     police.
  2. The appended-default-path trap is closed explicitly.
     aiaccounts.cli_cred_paths() APPENDS the per-user default credential path,
     and the wsl.localhost variants, to whatever the environment names. So the
     fake home has to capture those appended paths as well; the harness
     asserts every resolved path lies inside the temp tree, drops the
     *_CRED_PATHS entries the repository .env contributes, and neutralises the
     wsl.localhost home walk before it can stat a real credential file.
  3. urllib.request.urlopen and socket.socket.connect are tripwires. Both live
     providers geo-block this region, where a real outbound authentication
     request risks the account itself rather than merely failing.
  4. builtins.open and os.open are wrapped. A path outside the temp tree that
     looks like a credential store raises immediately; any other outside path
     is recorded and asserted empty at the end.
  5. shutil.rmtree / os.remove / os.unlink / os.rmdir are wrapped so a
     tear-down bug cannot delete anything outside the temp tree -- it raises.

No token value is ever printed. The fake secrets below are self-evidently not
credentials, and only the PRESENCE of a captured credential is ever reported.

Tests assert the documented contract, not the implementation's incidental
details. Where the two disagree the harness prints a FINDING instead of
quietly bending the test to match the code.
"""

import builtins
import inspect
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request

REPO_ROOT = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))

# Obvious placeholders. Nothing here is, or resembles, a real secret.
FAKE_ACCESS = "FAKE-ACCESS-TOKEN-NOT-A-REAL-CREDENTIAL"
FAKE_REFRESH = "FAKE-REFRESH-TOKEN-NOT-A-REAL-CREDENTIAL"
ACCT_A = "acct-FAKE-AAAA-0001"
ACCT_B = "acct-FAKE-BBBB-0002"


# ---------------------------------------------------------------------------
# result bookkeeping (same shape as tests/ai_accounts_regression.py)
# ---------------------------------------------------------------------------
class Skip(Exception):
    """Raised by a test body when the thing it covers cannot be exercised."""


RESULTS = []
FINDINGS = []


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


def finding(text):
    if text not in FINDINGS:
        FINDINGS.append(text)


# ---------------------------------------------------------------------------
# SAFETY LAYER 1: one temp tree, and every notion of "home" inside it
# ---------------------------------------------------------------------------
TMPDIR = os.path.realpath(tempfile.mkdtemp(prefix="nw_ai_login_"))
FAKE_HOME = os.path.join(TMPDIR, "home")
VAULT = os.path.join(TMPDIR, "vault", "accounts.json")
WORK = os.path.join(TMPDIR, "work")
SANDBOX_TMP = os.path.join(TMPDIR, "tmp")
for d in (FAKE_HOME, os.path.dirname(VAULT), WORK, SANDBOX_TMP):
    os.makedirs(d, exist_ok=True)

os.environ["AI_ACCOUNTS_FILE"] = VAULT
os.environ["HOME"] = FAKE_HOME
os.environ["USERPROFILE"] = FAKE_HOME
os.environ["HOMEDRIVE"] = ""
os.environ["HOMEPATH"] = FAKE_HOME
os.environ["APPDATA"] = os.path.join(TMPDIR, "appdata")
os.environ["LOCALAPPDATA"] = os.path.join(TMPDIR, "localappdata")
os.environ["XDG_CONFIG_HOME"] = os.path.join(FAKE_HOME, ".config")
os.environ["CLAUDE_CRED_PATHS"] = ""
os.environ["CODEX_CRED_PATHS"] = ""
# The module under test creates its sandboxes with tempfile.mkdtemp and
# refuses any sandbox outside tempfile.gettempdir(). Redirect that too, or the
# sandboxes land in the real system temp directory where this harness's
# containment assertions would (correctly) refuse to operate.
os.environ["TMPDIR"] = SANDBOX_TMP
os.environ["TMP"] = SANDBOX_TMP
os.environ["TEMP"] = SANDBOX_TMP
tempfile.tempdir = SANDBOX_TMP

NC = os.path.normcase


def inside_tmp(path):
    try:
        rp = os.path.realpath(path)
    except Exception:
        return False
    return NC(rp) == NC(TMPDIR) or NC(rp).startswith(NC(TMPDIR) + os.sep)


def cleanup():
    _FSGUARD["off"] = True
    _real_rmtree(TMPDIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# SAFETY LAYER 3: network kill-switch
# ---------------------------------------------------------------------------
NETWORK_ATTEMPTS = []


def _blocked_urlopen(*a, **k):
    NETWORK_ATTEMPTS.append("urlopen")
    raise RuntimeError("network access is blocked inside the test harness")


_real_connect = socket.socket.connect


def _blocked_connect(self, addr, *a, **k):
    host = addr[0] if isinstance(addr, tuple) and addr else ""
    if host in ("127.0.0.1", "::1", "localhost"):
        return _real_connect(self, addr, *a, **k)
    NETWORK_ATTEMPTS.append("connect:%s" % (host,))
    raise RuntimeError("network access is blocked inside the test harness")


urllib.request.urlopen = _blocked_urlopen
socket.socket.connect = _blocked_connect


# ---------------------------------------------------------------------------
# SAFETY LAYERS 4 and 5: filesystem guards
# ---------------------------------------------------------------------------
SENSITIVE_TAILS = (
    os.path.join(".claude", ".credentials.json"),
    os.path.join(".codex", "auth.json"),
    os.path.join("net-watch-ui", "accounts.json"),
)

_PY_ROOTS = tuple(
    NC(os.path.realpath(p))
    for p in {sys.prefix, sys.base_prefix, os.path.dirname(os.__file__)}
    if p
)

OPEN_VIOLATIONS = []
HARD_VIOLATIONS = []
DELETE_VIOLATIONS = []
_FSGUARD = {"off": False}

_real_open = builtins.open
_real_os_open = os.open
_real_rmtree = shutil.rmtree
_real_remove = os.remove
_real_unlink = os.unlink
_real_rmdir = os.rmdir


def _looks_sensitive(rp):
    low = NC(rp)
    return any(low.endswith(NC(t)) for t in SENSITIVE_TAILS)


def _classify(path):
    """-> 'ok' | 'allowed' | 'soft' | 'hard'"""
    try:
        rp = os.path.realpath(path)
    except Exception:
        return "soft"
    if inside_tmp(rp):
        return "ok"
    if _looks_sensitive(rp):
        return "hard"
    low = NC(rp)
    if low.startswith(_PY_ROOTS) or low.startswith(NC(os.path.realpath(REPO_ROOT))):
        return "allowed"
    return "soft"


def _guard_path(op, path):
    if _FSGUARD["off"]:
        return
    if isinstance(path, int):
        return
    if not isinstance(path, (str, bytes, os.PathLike)):
        return
    if isinstance(path, bytes):
        try:
            path = path.decode("utf-8", "replace")
        except Exception:
            return
    verdict = _classify(path)
    if verdict == "hard":
        HARD_VIOLATIONS.append("%s:%s" % (op, os.path.basename(str(path))))
        raise PermissionError(
            "test harness refused %s on a real credential store outside the "
            "sandbox" % op)
    if verdict == "soft":
        OPEN_VIOLATIONS.append("%s:%s" % (op, os.path.basename(str(path))))


def _guarded_open(file, *a, **k):
    _guard_path("open", file)
    return _real_open(file, *a, **k)


def _guarded_os_open(path, *a, **k):
    _guard_path("os.open", path)
    return _real_os_open(path, *a, **k)


def _guard_delete(op, path):
    """Deletion outside the temp tree always raises: a tear-down bug must not
    be able to remove anything real, not even something unimportant."""
    if _FSGUARD["off"]:
        return
    if not inside_tmp(path):
        DELETE_VIOLATIONS.append("%s:%s" % (op, os.path.basename(str(path))))
        raise PermissionError("test harness refused %s outside the sandbox" % op)


def _guarded_rmtree(path, *a, **k):
    _guard_delete("rmtree", path)
    return _real_rmtree(path, *a, **k)


def _guarded_remove(path, *a, **k):
    _guard_delete("remove", path)
    return _real_remove(path, *a, **k)


def _guarded_unlink(path, *a, **k):
    _guard_delete("unlink", path)
    return _real_unlink(path, *a, **k)


def _guarded_rmdir(path, *a, **k):
    _guard_delete("rmdir", path)
    return _real_rmdir(path, *a, **k)


def install_fs_guards():
    builtins.open = _guarded_open
    os.open = _guarded_os_open
    shutil.rmtree = _guarded_rmtree
    os.remove = _guarded_remove
    os.unlink = _guarded_unlink
    os.rmdir = _guarded_rmdir


def remove_fs_guards():
    _FSGUARD["off"] = True
    builtins.open = _real_open
    os.open = _real_os_open
    shutil.rmtree = _real_rmtree
    os.remove = _real_remove
    os.unlink = _real_unlink
    os.rmdir = _real_rmdir


# ---------------------------------------------------------------------------
# import the modules under test, then close the trap doors they opened
# ---------------------------------------------------------------------------
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

ailogin = None
aiaccounts = None
aiproviders = None
IMPORT_ERRORS = {}

for _name in ("aiaccounts", "aiproviders", "ailogin"):
    try:
        globals()[_name] = __import__(_name)
    except Exception as e:
        IMPORT_ERRORS[_name] = "%s: %s" % (type(e).__name__, e)

NEUTRALISED = []
for _mod in (aiaccounts, aiproviders, ailogin):
    if _mod is None:
        continue
    # The repository .env names REAL credential files through *_CRED_PATHS and
    # those values outrank the process environment. Drop them: the harness may
    # not read the user's live logins.
    env = getattr(_mod, "ENV", None)
    if isinstance(env, dict):
        dropped = [k for k in env if k.endswith("_CRED_PATHS")]
        for k in dropped:
            env.pop(k, None)
        if dropped:
            NEUTRALISED.append("%s.ENV *_CRED_PATHS dropped" % _mod.__name__)
    # Discovery helper, not logic under test. Left live it would stat real
    # credential files on the wsl.localhost share.
    if hasattr(_mod, "_wsl_homes"):
        _mod._wsl_homes = lambda: []
        NEUTRALISED.append("%s._wsl_homes disabled" % _mod.__name__)


# ---------------------------------------------------------------------------
# resolve the contract's roles against whatever names the module exposes
# ---------------------------------------------------------------------------
ROLE_CANDIDATES = {
    "capability": ["isolated_login_supported", "supports_login",
                   "login_supported", "can_login", "login_available"],
    "create": ["create_sandbox", "make_sandbox", "new_sandbox",
               "sandbox_create", "prepare_sandbox"],
    "launch": ["launch_login", "start_login", "spawn_login", "launch"],
    "wait": ["wait_for_cred", "wait_for_credential", "wait_for_login",
             "await_credential", "wait_capture", "wait"],
    "teardown": ["destroy_sandbox", "teardown_sandbox", "remove_sandbox",
                 "cleanup_sandbox", "close_sandbox", "discard_sandbox"],
    "duplicate": ["classify_account", "classify_capture", "duplicate_check",
                  "detect_duplicate", "is_duplicate", "match_account"],
    "pathguard": ["assert_sandbox_path_safe", "assert_safe_sandbox_path",
                  "assert_safe_path", "is_protected_path", "guard_path"],
}

ROLES = {}


def resolve_roles():
    if ailogin is None:
        return
    for r, names in ROLE_CANDIDATES.items():
        for n in names:
            if hasattr(ailogin, n):
                ROLES[r] = (n, getattr(ailogin, n))
                break


def role(name):
    if ailogin is None:
        raise Skip("ailogin not importable: %s"
                   % IMPORT_ERRORS.get("ailogin", "missing"))
    if name not in ROLES:
        raise Skip("no %s function on ailogin (tried: %s)"
                   % (name, ", ".join(ROLE_CANDIDATES[name])))
    return ROLES[name][1]


def _attr(handle, names, what):
    if handle is None:
        raise Skip("sandbox handle was None")
    if isinstance(handle, dict):
        for n in names:
            if n in handle:
                return handle[n]
    for n in names:
        if hasattr(handle, n):
            return getattr(handle, n)
    raise Skip("sandbox handle exposes no %s (tried: %s)"
               % (what, ", ".join(names)))


def h_dir(h):
    return _attr(h, ["dir", "path", "sandbox_dir", "root", "directory"],
                 "sandbox directory")


def h_env(h):
    return _attr(h, ["env", "env_overrides", "environ", "overrides"],
                 "environment overrides")


def h_cred(h):
    return _attr(h, ["cred_path", "expected_cred_path", "credential_path",
                     "cred_file"], "expected credential path")


# ---------------------------------------------------------------------------
# the child-process probe: a REAL process that reports the home it sees
# ---------------------------------------------------------------------------
PROBE = os.path.join(WORK, "probe_login_cli.py")
PROBE_SRC = '''\
"""Stand-in for a vendor login CLI. Contacts nothing."""
import json, os, sys, time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
cred_path = sys.argv[2] if len(sys.argv) > 2 else ""
report_to = sys.argv[3] if len(sys.argv) > 3 else ""

report = {
    "expanduser": os.path.expanduser("~"),
    "HOME": os.environ.get("HOME", ""),
    "USERPROFILE": os.environ.get("USERPROFILE", ""),
    "CODEX_HOME": os.environ.get("CODEX_HOME", ""),
    "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR", ""),
    "cwd": os.getcwd(),
}
if report_to:
    try:
        with open(report_to, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(report))
    except OSError:
        pass

if mode == "cancel":
    sys.exit(0)            # clean exit, no credential written -> cancelled
if mode == "error":
    sys.exit(3)            # non-zero exit -> CLI error
if mode == "hang":
    time.sleep(900)
    sys.exit(0)

# mode == "ok": write a credential-shaped document where the caller expects it
if cred_path:
    d = os.path.dirname(cred_path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    doc = {"tokens": {"access_token": "FAKE-ACCESS-TOKEN-NOT-A-REAL-CREDENTIAL",
                      "refresh_token": "FAKE-REFRESH-TOKEN-NOT-A-REAL-CREDENTIAL",
                      "account_id": "acct-FAKE-AAAA-0001"},
           "last_refresh": "1970-01-01T00:00:00Z"}
    with open(cred_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
sys.exit(0)
'''


def write_probe():
    with _real_open(PROBE, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(PROBE_SRC)


def probe_argv(mode, cred_path="", report_to=""):
    return [sys.executable, PROBE, mode, cred_path or "", report_to or ""]


def run_probe_directly(env_overrides, mode="ok", cred_path="", timeout=60):
    """Run the probe with a plain subprocess call (harness self-test only)."""
    env = dict(os.environ)
    for k, v in dict(env_overrides or {}).items():
        if v is None:
            env.pop(str(k), None)
        else:
            env[str(k)] = str(v)
    report = os.path.join(WORK, "selftest_report.json")
    if os.path.exists(report):
        _real_remove(report)
    p = subprocess.run(probe_argv(mode, cred_path, report),
                       capture_output=True, text=True, timeout=timeout,
                       env=env, cwd=WORK)
    return p, read_report(report)


def read_report(path):
    try:
        with _real_open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# adapters: call each role flexibly, and report signature drift as a finding
# ---------------------------------------------------------------------------
PROVIDER = "codex"


def call_create(provider=PROVIDER):
    create = role("create")
    try:
        return create(provider)
    except TypeError:
        return create()


def new_sandbox(provider=PROVIDER):
    """Create a sandbox and assert containment before anything is written."""
    box = call_create(provider)
    d = h_dir(box)
    if not inside_tmp(d):
        raise AssertionError(
            "create returned a sandbox OUTSIDE the temp tree; refusing to "
            "continue")
    return box


def call_launch(provider, box, argv, console=False):
    """Launch through the module, forcing our harmless probe as the command.

    The real login command must never be launched from a test: it would open a
    live vendor OAuth flow from a geo-blocked region, in a child process where
    this harness's network tripwires do not apply.
    """
    launch = role("launch")
    try:
        return launch(provider, box, argv=argv, console=console)
    except TypeError:
        try:
            return launch(provider, box, argv)
        except TypeError:
            raise Skip("launch accepts no test argv, so it can only start the "
                       "real vendor CLI; refusing to run a live login")


def call_wait(box, timeout, proc=None):
    waitfn = role("wait")
    attempts = (
        lambda: waitfn(box, proc=proc, timeout=timeout, poll=0.05),
        lambda: waitfn(box, proc=proc, timeout=timeout),
        lambda: waitfn(box, proc, timeout),
        lambda: waitfn(box, timeout),
    )
    last = None
    for fn in attempts:
        try:
            return normalise_wait(fn())
        except TypeError as e:
            last = e
            continue
    raise Skip("could not call wait with any expected signature: %s" % last)


def normalise_wait(result):
    """Reduce whatever wait returns to (cred_or_None, reason_or_None)."""
    if isinstance(result, tuple) and len(result) == 2:
        cred, reason = result
    elif isinstance(result, dict) and ("cred" in result or "reason" in result):
        cred = result.get("cred", result.get("credential"))
        reason = result.get("reason", result.get("error"))
    elif hasattr(result, "cred"):
        cred = result.cred
        reason = getattr(result, "reason", None)
    elif isinstance(result, dict):
        cred, reason = result, None
    elif isinstance(result, str):
        cred, reason = None, result
    elif result is None:
        cred, reason = None, None
    else:
        raise Skip("wait returned an unrecognised shape: %s"
                   % type(result).__name__)
    if isinstance(reason, str) and reason.strip().lower() in ("ok", "success", ""):
        reason = None
    return cred, reason


def call_duplicate(provider, cred, accounts):
    """Call the duplicate detector by INSPECTING its signature.

    Guessing arity here was a real bug in an earlier version of this harness:
    classify_account(provider, cred, accounts=None) happily accepts two
    positional arguments, so a contract-shaped call (cred, accounts) binds
    cred->provider and accounts->cred and returns a confident-looking
    "unknown" instead of a TypeError. Anything that can silently mean the
    wrong thing gets resolved explicitly.
    """
    dup = role("duplicate")
    name = ROLES["duplicate"][0]
    try:
        params = [p for p in inspect.signature(dup).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):
        params = []
    if len(params) >= 3:
        finding(
            "%s takes (provider, cred, accounts); the contract describes "
            "(cred, accounts). Defensible -- the fingerprint is "
            "provider-specific -- but a caller written to the contract binds "
            "its arguments one slot over and gets a wrong answer rather than "
            "a TypeError." % name)
        return dup(provider, cred, accounts)
    return dup(cred, accounts)


def call_teardown(box):
    return role("teardown")(box)


def teardown_quietly(box):
    if box is None or "teardown" not in ROLES:
        return
    try:
        ROLES["teardown"][1](box)
    except Exception:
        pass


def kill_quietly(proc):
    try:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# credential fixtures -- built from the non-secret identifying fields
# ---------------------------------------------------------------------------
def codex_cred(account_id, access=FAKE_ACCESS, refresh=FAKE_REFRESH):
    return {"tokens": {"access_token": access, "refresh_token": refresh,
                       "account_id": account_id}}


def claude_cred(sub="max", expires_ms=1893456000000):
    return {"claudeAiOauth": {"accessToken": FAKE_ACCESS,
                              "refreshToken": FAKE_REFRESH,
                              "expiresAt": expires_ms,
                              "subscriptionType": sub,
                              "scopes": ["user:inference"]}}


def registered(provider, cred, label="fixture", ident="0" * 32):
    return {"id": ident, "provider": provider, "label": label,
            "source": "manual", "cred": cred}


def means_duplicate(out):
    """-> True (same account) / False (new) / None (unreadable)."""
    if isinstance(out, dict):
        st = str(out.get("status") or "").lower()
        if st in ("duplicate", "same", "existing"):
            return True
        if st == "new":
            return False
        for k in ("duplicate", "is_duplicate", "same"):
            if k in out:
                return bool(out[k])
        if "new" in out:
            return not bool(out["new"])
        return None
    if isinstance(out, bool):
        n = ROLES["duplicate"][0].lower()
        return (not out) if "new" in n else out
    if isinstance(out, str):
        low = out.lower()
        if any(k in low for k in ("dup", "same", "existing", "known")):
            return True
        if "new" in low:
            return False
        return None
    if isinstance(out, tuple) and out:
        return means_duplicate(out[0])
    if out is None:
        return False
    if hasattr(out, "status"):
        return means_duplicate({"status": out.status})
    return None


# ===========================================================================
# harness self-tests -- prove the rig itself works
# ===========================================================================
def need_acct():
    if aiaccounts is None:
        raise Skip("aiaccounts not importable")


def s01_probe_child_honours_home_override():
    sub = os.path.join(TMPDIR, "selftest_home")
    os.makedirs(sub, exist_ok=True)
    over = {"HOME": sub, "USERPROFILE": sub, "HOMEDRIVE": "", "HOMEPATH": sub}
    cred = os.path.join(sub, ".codex", "auth.json")
    p, rep = run_probe_directly(over, "ok", cred)
    if p.returncode != 0:
        return False, "probe exited %s: %s" % (p.returncode, p.stderr.strip()[:80])
    seen = os.path.realpath(rep.get("expanduser") or "|none|")
    if NC(seen) != NC(os.path.realpath(sub)):
        return False, "child expanduser('~') did not follow the override"
    if not os.path.isfile(cred):
        return False, "probe wrote no credential-shaped document"
    return True, "child home overridden; credential-shaped doc written"


def s02_delete_guard_refuses_outside_tree():
    outside = os.path.join(os.path.dirname(TMPDIR), "nw-harness-never-created")
    before = len(DELETE_VIOLATIONS)
    try:
        shutil.rmtree(outside)
    except PermissionError:
        ok = len(DELETE_VIOLATIONS) == before + 1
        del DELETE_VIOLATIONS[before:]   # our own deliberate probe
        return ok, "rmtree outside the temp tree raises and is recorded"
    except Exception as e:
        return False, "raised %s, not PermissionError" % type(e).__name__
    return False, "rmtree outside the temp tree was ALLOWED"


def s03_open_guard_refuses_real_cred_store():
    root = "Z:" + os.sep if os.name == "nt" else os.sep
    target = os.path.join(root, "nw-harness-nonexistent-home", ".codex",
                          "auth.json")
    before = len(HARD_VIOLATIONS)
    try:
        _guarded_open(target, "r")
    except PermissionError:
        return len(HARD_VIOLATIONS) == before + 1, \
            "opening a real cred-store path raises and is recorded"
    except Exception as e:
        return False, "raised %s, not PermissionError" % type(e).__name__
    finally:
        del HARD_VIOLATIONS[before:]   # our own deliberate probe
    return False, "a real cred-store path outside the tree was ALLOWED"


def s04_network_tripwire_is_live():
    before = len(NETWORK_ATTEMPTS)
    try:
        urllib.request.urlopen("http://nw-harness.invalid/")
    except RuntimeError:
        pass
    except Exception as e:
        return False, "raised %s, not RuntimeError" % type(e).__name__
    else:
        return False, "urlopen was NOT blocked"
    ok = len(NETWORK_ATTEMPTS) == before + 1
    del NETWORK_ATTEMPTS[before:]      # our own deliberate probe
    try:
        socket.socket().connect(("example.invalid", 443))
    except RuntimeError:
        pass
    except Exception as e:
        return False, "socket.connect raised %s, not RuntimeError" % type(e).__name__
    else:
        return False, "socket.connect was NOT blocked"
    del NETWORK_ATTEMPTS[before:]
    return ok, "urlopen and socket.connect both fail loudly"


def s05_cred_paths_fully_sandboxed():
    """The appended-default trap: prove the fake home captures it."""
    need_acct()
    bad = []
    total = 0
    for provider in ("claude", "codex"):
        for p in aiaccounts.cli_cred_paths(provider):
            total += 1
            if not inside_tmp(p):
                bad.append(p)
    if bad:
        return False, "%d of %d resolved cred path(s) escape the temp tree" % (
            len(bad), total)
    return True, "all %d cli_cred_paths() results lie inside the temp tree" % total


def s06_module_tempdir_is_sandboxed():
    if not inside_tmp(tempfile.gettempdir()):
        return False, "tempfile.gettempdir() is outside the temp tree, so "\
                      "module-created sandboxes would escape the guards"
    return True, "tempfile.gettempdir() redirected into the temp tree"


# ===========================================================================
# contract tests
# ===========================================================================
def t01_sandbox_in_temp_not_repo_not_config():
    box = new_sandbox()
    try:
        d = os.path.realpath(h_dir(box))
        if not os.path.isdir(d):
            return False, "sandbox directory does not exist"
        if NC(d).startswith(NC(os.path.realpath(REPO_ROOT)) + os.sep):
            return False, "sandbox was created INSIDE the repository"
        for var in ("USERPROFILE", "HOME", "APPDATA", "XDG_CONFIG_HOME"):
            cfg = os.environ.get(var) or ""
            if cfg and NC(d).startswith(NC(os.path.realpath(cfg)) + os.sep):
                return False, "sandbox was created inside %s" % var
        if not inside_tmp(d):
            return False, "sandbox escaped the temp tree"
        return True, "sandbox in temp space, outside the repo and every "\
                     "config dir"
    finally:
        teardown_quietly(box)


def t02_sandbox_permissions_restrictive():
    box = new_sandbox()
    try:
        d = h_dir(box)
        mode = stat.S_IMODE(os.stat(d).st_mode)
        if os.name == "nt":
            # POSIX mode bits are not the access-control mechanism on Windows;
            # what is assertable here is that the module asked for 0700 and
            # that the directory sits in per-user temp space.
            if mode & stat.S_IRUSR == 0:
                return False, "sandbox is not even owner-readable"
            return True, ("POSIX bits not enforced on Windows (mode 0o%o); "
                          "containment in per-user temp asserted instead" % mode)
        if mode & 0o077:
            return False, "sandbox is group/other accessible (0o%o)" % mode
        return True, "sandbox dir mode 0o%o" % mode
    finally:
        teardown_quietly(box)


def _child_report(box, provider=PROVIDER):
    """Launch the probe through the module and return what the child saw."""
    report = os.path.join(WORK, "child_report.json")
    if os.path.exists(report):
        _real_remove(report)
    proc = None
    try:
        proc = call_launch(provider, box,
                           probe_argv("ok", h_cred(box), report))
        proc.wait(timeout=60)
    finally:
        kill_quietly(proc)
    return read_report(report)


def t03_env_overrides_reach_a_real_child():
    """The launched child must actually receive the overrides."""
    box = new_sandbox()
    try:
        d = os.path.realpath(h_dir(box))
        env = h_env(box)
        if not env:
            return False, "handle carries no environment overrides at all"
        rep = _child_report(box)
        if not rep:
            return False, "child produced no report; it may not have started"
        carried = [k for k in ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR")
                   if rep.get(k) and inside_tmp(rep[k])
                   and NC(os.path.realpath(rep[k])).startswith(NC(d))]
        if not carried:
            return False, ("no home/config variable reached the child pointing "
                           "at the sandbox; a real CLI would reuse the "
                           "existing login")
        if not inside_tmp(rep.get("cwd") or "|none|"):
            return False, "child cwd is outside the temp tree"
        return True, "child received %s pointing into the sandbox" % (
            ", ".join(carried))
    finally:
        teardown_quietly(box)


def t03b_child_resolves_home_inside_the_sandbox():
    """Setting HOME is not the goal; what a CLI RESOLVES as ~ is the goal."""
    box = new_sandbox()
    try:
        d = os.path.realpath(h_dir(box))
        rep = _child_report(box)
        if not rep:
            return False, "child produced no report; it may not have started"
        seen = os.path.realpath(rep.get("expanduser") or "|none|")
        if NC(seen) == NC(d) or NC(seen).startswith(NC(d) + os.sep):
            return True, "child resolves ~ inside the sandbox"
        if not inside_tmp(seen):
            return False, "child's home resolved OUTSIDE the temp tree"
        finding(
            "the sandbox overrides HOME but not USERPROFILE / HOMEDRIVE+"
            "HOMEPATH, and on Windows os.path.expanduser('~') prefers "
            "USERPROFILE. A CLI that resolves ~ the Windows way therefore "
            "still reads the user's REAL home and reuses the existing login "
            "-- exactly the failure this module exists to prevent. Add those "
            "names to home_vars for a Windows-side launch.")
        return False, ("child resolved ~ to the surrounding home, not the "
                       "sandbox: HOME is overridden but USERPROFILE is not")
    finally:
        teardown_quietly(box)


def t04_expected_cred_path_inside_sandbox():
    box = new_sandbox()
    try:
        d = os.path.realpath(h_dir(box))
        cp = os.path.realpath(h_cred(box))
        if not inside_tmp(cp):
            return False, "expected credential path escapes the temp tree"
        if not NC(cp).startswith(NC(d) + os.sep):
            return False, "expected credential path is not inside the sandbox"
        return True, "expected credential path sits under the sandbox dir"
    finally:
        teardown_quietly(box)


def t05_successful_capture_returns_cred_from_sandbox():
    box = new_sandbox()
    proc = None
    try:
        cp = h_cred(box)
        proc = call_launch(PROVIDER, box, probe_argv("ok", cp))
        cred, reason = call_wait(box, 30, proc)
        if cred is None:
            return False, "wait reported no credential (reason=%r)" % (reason,)
        if reason:
            return False, "wait returned a credential AND a failure reason %r" % (
                reason,)
        if not os.path.isfile(cp):
            return False, "credential did not land at the expected path"
        if not inside_tmp(cp):
            return False, "credential landed outside the temp tree"
        tok = cred.get("tokens") if isinstance(cred, dict) else None
        if not isinstance(tok, dict) or not tok.get("account_id"):
            return False, "captured credential lost its identifying fields"
        return True, "credential captured from inside the sandbox "\
                     "(value deliberately not shown)"
    finally:
        kill_quietly(proc)
        teardown_quietly(box)


def t06_teardown_removes_sandbox_and_credential():
    box = new_sandbox()
    d = h_dir(box)
    cp = h_cred(box)
    os.makedirs(os.path.dirname(cp), exist_ok=True)
    with _real_open(cp, "w", encoding="utf-8") as fh:
        json.dump(codex_cred(ACCT_A), fh)
    call_teardown(box)
    if os.path.exists(cp):
        return False, "the credential file survived tear-down"
    if os.path.exists(d):
        return False, "the sandbox directory survived tear-down"
    return True, "sandbox and the credential inside it are gone"


def t07_teardown_after_cli_error():
    box = new_sandbox()
    d = h_dir(box)
    proc = None
    try:
        proc = call_launch(PROVIDER, box, probe_argv("error", h_cred(box)))
        cred, reason = call_wait(box, 20, proc)
        if cred is not None:
            return False, "a failing CLI still produced a credential"
        call_teardown(box)
        if os.path.exists(d):
            return False, "sandbox left behind after a CLI error"
        return True, "sandbox removed after a CLI error (reason=%r)" % (reason,)
    finally:
        kill_quietly(proc)


def t08_teardown_after_child_killed():
    box = new_sandbox()
    d = h_dir(box)
    cp = h_cred(box)
    proc = None
    try:
        proc = call_launch(PROVIDER, box, probe_argv("hang", cp))
        time.sleep(0.4)
        # Simulate the credential already on disk when the process dies: this
        # is the dangerous case, a live credential in an orphaned sandbox.
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        with _real_open(cp, "w", encoding="utf-8") as fh:
            json.dump(codex_cred(ACCT_A), fh)
        proc.kill()
        proc.wait(timeout=20)
        call_teardown(box)
        if os.path.exists(cp):
            return False, "credential survived tear-down after a kill"
        if os.path.exists(d):
            return False, "sandbox left behind after the child was killed"
        return True, "sandbox and credential removed after the child was killed"
    finally:
        kill_quietly(proc)
        teardown_quietly(box)


def t09_teardown_after_timeout():
    """Requirement: tear-down still happens when the login TIMES OUT.

    Deliberately does not kill the child first. A timeout means the CLI is
    still running, and that is precisely when an abandoned sandbox holding a
    credential is most likely to be left on disk.
    """
    box = new_sandbox()
    d = h_dir(box)
    proc = None
    try:
        proc = call_launch(PROVIDER, box, probe_argv("hang", h_cred(box)))
        cred, reason = call_wait(box, 1, proc)
        if cred is not None:
            return False, "wait invented a credential on timeout"
        call_teardown(box)
        if os.path.exists(d):
            finding(
                "destroy_sandbox cannot remove a sandbox while the launched "
                "CLI is still running: the child's cwd is the sandbox root, "
                "which Windows refuses to delete. On the timeout path the "
                "process is by definition still alive, so the sandbox -- and "
                "any credential already written into it -- survives. "
                "destroy_sandbox should terminate the process it was launched "
                "with (or the caller must, and the contract should say so).")
            return False, ("sandbox survived tear-down after a timeout while "
                           "the CLI was still running")
        return True, "timeout path tears the sandbox down (reason=%r)" % (reason,)
    finally:
        kill_quietly(proc)
        teardown_quietly(box)
        _real_rmtree(d, ignore_errors=True)


def t09b_teardown_after_timeout_then_kill():
    """The same path once the caller stops the CLI: must leave nothing."""
    box = new_sandbox()
    d = h_dir(box)
    proc = None
    try:
        proc = call_launch(PROVIDER, box, probe_argv("hang", h_cred(box)))
        cred, _reason = call_wait(box, 1, proc)
        if cred is not None:
            return False, "wait invented a credential on timeout"
        kill_quietly(proc)
        call_teardown(box)
        if os.path.exists(d):
            return False, "sandbox left behind after timeout + kill"
        return True, "timeout followed by a kill leaves nothing on disk"
    finally:
        kill_quietly(proc)
        _real_rmtree(d, ignore_errors=True)


def _reason_for(mode, timeout):
    box = new_sandbox()
    proc = None
    try:
        proc = call_launch(PROVIDER, box, probe_argv(mode, h_cred(box)))
        return call_wait(box, timeout, proc)
    finally:
        kill_quietly(proc)
        teardown_quietly(box)


def t10_three_failure_reasons_stay_distinct():
    cancel_cred, cancel = _reason_for("cancel", 30)
    err_cred, err = _reason_for("error", 30)
    to_cred, to = _reason_for("hang", 1)
    if any(c is not None for c in (cancel_cred, err_cred, to_cred)):
        return False, "a failing login still produced a credential"
    reasons = [str(cancel or ""), str(err or ""), str(to or "")]
    if not all(reasons):
        return False, "a failure produced no reason at all: %r" % (reasons,)
    if len({r.lower() for r in reasons}) != 3:
        return False, "failure modes collapsed into one reason: %r" % (reasons,)
    low = [r.lower() for r in reasons]
    if "cancel" not in low[0]:
        finding("cancellation reason %r does not name cancellation" % reasons[0])
    if not any(k in low[1] for k in ("error", "exit", "fail")):
        finding("CLI-error reason %r does not name a CLI failure" % reasons[1])
    if not any(k in low[2] for k in ("timeout", "timed out")):
        finding("timeout reason %r does not name a timeout" % reasons[2])
    for r in reasons:
        if FAKE_ACCESS in r or FAKE_REFRESH in r:
            return False, "a reason string leaked token material"
    return True, "cancel / cli-error / timeout are three distinct reasons"


def t11_duplicate_same_account_twice():
    cred = codex_cred(ACCT_A)
    out = call_duplicate("codex", cred,
                         [registered("codex", codex_cred(ACCT_A), "personal")])
    same = means_duplicate(out)
    if same is None:
        raise Skip("duplicate detector returned an unreadable verdict: %r"
                   % (type(out).__name__,))
    return same, "the same account signed in twice is reported as the same"


def t12_duplicate_two_different_accounts():
    out = call_duplicate("codex", codex_cred(ACCT_B),
                         [registered("codex", codex_cred(ACCT_A), "personal")])
    same = means_duplicate(out)
    if same is None:
        raise Skip("duplicate detector returned an unreadable verdict")
    return (not same), "a genuinely different account is reported as new"


def t13_duplicate_survives_token_rotation():
    """Identity must come from stable fields, never from token values."""
    rotated = codex_cred(ACCT_A, access="FAKE-ROTATED-ACCESS-NOT-REAL",
                         refresh="FAKE-ROTATED-REFRESH-NOT-REAL")
    out = call_duplicate("codex", rotated,
                         [registered("codex", codex_cred(ACCT_A))])
    same = means_duplicate(out)
    if same is None:
        raise Skip("duplicate detector returned an unreadable verdict")
    return same, "rotated tokens, same account -> still the same account"


def t14_duplicate_ignores_other_providers():
    """A claude row must not make a codex capture look like a duplicate."""
    out = call_duplicate("codex", codex_cred(ACCT_A),
                         [registered("claude", claude_cred(), "work")])
    same = means_duplicate(out)
    if same is None:
        raise Skip("duplicate detector returned an unreadable verdict")
    return (not same), "a different provider's row is not a duplicate match"


def t15_duplicate_unrecognisable_cred_is_not_new():
    """A shape nobody recognises must not be announced as a new account."""
    out = call_duplicate("codex", {"nonsense": True},
                         [registered("codex", codex_cred(ACCT_A))])
    if isinstance(out, dict) and "status" in out:
        st = str(out.get("status") or "").lower()
        if st == "new":
            return False, "an unrecognisable credential was reported as a "\
                          "new account"
        return True, "unrecognisable credential reported as %r" % st
    same = means_duplicate(out)
    if same is None:
        return True, "unrecognisable credential yields no confident verdict"
    if same is False:
        return False, "an unrecognisable credential was reported as new"
    return True, "unrecognisable credential not reported as new"


def t16_refuses_path_in_real_home_config_dir():
    guard = role("pathguard")
    name = ROLES["pathguard"][0]
    targets = [os.path.join(FAKE_HOME, ".codex"),
               os.path.join(FAKE_HOME, ".claude"),
               FAKE_HOME,
               REPO_ROOT]
    verdicts = []
    for t in targets:
        try:
            out = guard(t)
        except Exception:
            verdicts.append(True)     # refused by raising
            continue
        if isinstance(out, bool):
            flagging = ("is_" in name or "protected" in name)
            verdicts.append(out if flagging else (not out))
        else:
            verdicts.append(False)
    if all(verdicts):
        return True, "%s refuses home, both config dirs and the repo" % name
    bad = [os.path.basename(t) or t for t, v in zip(targets, verdicts) if not v]
    return False, "%s ACCEPTED protected path(s): %s" % (name, ", ".join(bad))


def t17_guard_accepts_a_legitimate_temp_path():
    """The guard must not be so blunt that no sandbox can ever be made."""
    guard = role("pathguard")
    probe = tempfile.mkdtemp(prefix="nw-guard-probe-")
    try:
        out = guard(probe)
        if isinstance(out, bool) and out is False:
            name = ROLES["pathguard"][0]
            if not ("is_" in name or "protected" in name):
                return False, "the guard rejects a fresh temp directory"
        return True, "a fresh temp directory is accepted"
    except Exception as e:
        return False, "the guard rejects a fresh temp directory: %s" % e
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def t18_capability_false_for_planned_providers():
    cap = role("capability")
    if aiproviders is None:
        raise Skip("aiproviders not importable")
    planned = [p.get("id") for p in aiproviders.PROVIDERS
               if (p.get("status") or "planned") != "live"]
    if not planned:
        raise Skip("no planned providers in the registry")
    bad = [pid for pid in planned if cap(pid) is not False]
    if bad:
        return False, "capability claims login support for planned "\
                      "provider(s): %s" % ", ".join(sorted(map(str, bad))[:5])
    return True, "all %d planned providers report no login support" % len(planned)


def t19_capability_false_for_unknown_and_junk():
    cap = role("capability")
    if cap("nw-not-a-provider") is not False:
        return False, "an unknown slug did not return False"
    for junk in (None, "", 0, [], {}):
        try:
            if cap(junk) is not False:
                return False, "junk input %r did not return False" % (junk,)
        except Exception as e:
            finding(
                "the capability check raises %s on a non-string argument "
                "(%r): `provider not in _PROVIDER_ENV` is a dict membership "
                "test, so an unhashable value propagates out of a function "
                "the UI calls to decide whether to enable a button. It should "
                "answer False for anything that is not a known slug."
                % (type(e).__name__, junk))
            return False, "junk input %r raised %s" % (junk, type(e).__name__)
    return True, "unknown and junk slugs return False without raising"


def t20_capability_is_a_real_bool():
    cap = role("capability")
    if aiproviders is None:
        raise Skip("aiproviders not importable")
    live = [p.get("id") for p in aiproviders.PROVIDERS
            if (p.get("status") or "") == "live"]
    if not live:
        raise Skip("no live providers in the registry")
    vals = {}
    for pid in live:
        v = cap(pid)
        if not isinstance(v, bool):
            return False, "capability returned %s, not a bool, for a live "\
                          "provider" % type(v).__name__
        vals[pid] = v
    return True, "live providers answer with a bool (%s)" % ", ".join(
        "%s=%s" % (k, v) for k, v in sorted(vals.items()))


def t21_launch_refuses_unsupported_provider():
    launch = role("launch")
    box = new_sandbox()
    try:
        try:
            out = launch("nw-not-a-provider", box,
                         argv=probe_argv("ok"), console=False)
        except TypeError:
            raise Skip("launch has no test argv parameter")
        except Exception:
            return True, "launching an unknown provider is refused by raising"
        kill_quietly(out)
        return False, "an unknown provider was actually launched"
    finally:
        teardown_quietly(box)


def t22_launch_refuses_a_destroyed_sandbox():
    box = new_sandbox()
    call_teardown(box)
    try:
        proc = call_launch(PROVIDER, box, probe_argv("ok"))
    except Skip:
        raise
    except Exception:
        return True, "launching into a destroyed sandbox is refused"
    kill_quietly(proc)
    return False, "a login was launched into an already-destroyed sandbox"


def t23_two_sandboxes_are_independent():
    a = new_sandbox()
    b = new_sandbox()
    try:
        da, db = os.path.realpath(h_dir(a)), os.path.realpath(h_dir(b))
        if NC(da) == NC(db):
            return False, "two logins share one sandbox, so the second would "\
                          "see the first one's session"
        call_teardown(a)
        if not os.path.isdir(db):
            return False, "tearing one sandbox down destroyed the other"
        return True, "sandboxes are per-login and independently torn down"
    finally:
        teardown_quietly(b)
        teardown_quietly(a)


def t24_teardown_is_idempotent():
    box = new_sandbox()
    d = h_dir(box)
    call_teardown(box)
    try:
        call_teardown(box)
    except Exception as e:
        return False, "a second tear-down raised %s" % type(e).__name__
    return (not os.path.exists(d)), "tear-down is safe to call twice"


# ===========================================================================
# WSL in-distro child tests -- the ONLY cases here that cross the interop
# boundary, and the only ones that could have caught the HOME leak
# ===========================================================================
# Every other test in this file inspects the environment dict that ailogin
# BUILDS on the Python side. That dict was perfectly correct the whole time
# four separate defects were live, because none of them lived on the Python
# side:
#
#   1. WSL's interop layer resets HOME from /etc/passwd on the far side of the
#      boundary no matter what WSLENV carries, so the sandbox HOME never
#      reached the in-distro process and the CLI read the user's real
#      ~/.claude.json -- handing back the FIRST account again.
#   2. The user's shell rc exports ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN.
#      `bash -lic` sources that rc, which put the CLI in API mode, so the OAuth
#      flow silently never ran at all.
#   3. `claude auth login` was never appended, so a bare invocation opened the
#      interactive REPL instead of performing a sign-in.
#   4. `wslpath` was handed a native Windows path containing BACKSLASHES. The
#      interop layer eats backslashes as escapes, so "C:\Users\..." arrived
#      inside the distro as "C:Usersshaya..." and wslpath exited 1 -- which
#      made wsl_root None, which made fix (1) a no-op even after it was
#      written.
#
# A Python-side env-dict assertion cannot observe any of those. The only
# honest probe is to spawn a REAL child through the SAME wrapper the login
# path uses and read what that child actually printed.

WSL_PROVIDER = "claude"       # the provider whose command targets wsl.exe
_WSL_REPORTS = {}             # spawning an in-distro child costs seconds


def _wsl_launcher_argv():
    """The configured login argv, only if it really launches wsl.exe."""
    if ailogin is None:
        return None
    try:
        argv = ailogin.login_command(WSL_PROVIDER) or []
    except Exception:
        return None
    if not argv:
        return None
    if os.path.basename(str(argv[0])).lower() not in ("wsl", "wsl.exe"):
        return None
    if "--" not in argv:
        return None
    return list(argv)


def wsl_unavailable_reason():
    """Why these tests cannot run here, or None when they can.

    Detection is deliberately layered, because "no WSL" is not one condition
    and a suite that must stay runnable on a machine without WSL may not fail
    on any of them:

      * not Windows at all -- there is no interop boundary to cross;
      * wsl.exe is not on PATH -- WSL is not installed;
      * the configured login command does not go through wsl.exe -- the
        wrapper under test is never reached on this machine's configuration;
      * `wsl.exe -d <distro> -- true` does not exit 0 -- WSL is installed but
        the distro is missing, stopped, or broken.

    Each returns a Skip reason, never a failure.
    """
    if ailogin is None:
        return "ailogin not importable"
    if os.name != "nt":
        return "not Windows: there is no WSL interop boundary to cross"
    if shutil.which("wsl.exe") is None:
        return "wsl.exe is not on PATH (WSL not installed)"
    argv = _wsl_launcher_argv()
    if argv is None:
        return ("the configured %s login command does not launch wsl.exe, so "
                "the in-distro wrapper is never used here" % WSL_PROVIDER)
    dd = argv.index("--")
    probe = list(argv[:dd + 1]) + ["true"]
    try:
        p = subprocess.run(probe, capture_output=True, text=True, timeout=90)
    except Exception as e:
        return "WSL probe raised %s (distro unavailable)" % type(e).__name__
    if p.returncode != 0:
        return ("WSL probe exited %s (distro missing or not running)"
                % p.returncode)
    return None


def need_wsl():
    why = wsl_unavailable_reason()
    if why:
        raise Skip(why)


# ---------------------------------------------------------------------------
# the in-distro probe command
# ---------------------------------------------------------------------------
# `printenv`, NEVER `echo "$HOME"`.
#
# THIS IS NOT A STYLE PREFERENCE. DO NOT "SIMPLIFY" IT BACK.
#
# The fix under test works by prefixing the command bash runs with
# `env HOME=<sandbox> ...`. bash expands $HOME while PARSING the command
# string -- before the `env HOME=...` prefix has executed -- so an
# echo-based probe reports the OLD value and makes a WORKING fix look
# broken. Measured on this machine through the fixed wrapper:
#
#     env HOME=<sandbox> echo "$HOME"   ->  /home/shawn      (the trap)
#     env HOME=<sandbox> printenv       ->  HOME=<sandbox>   (the truth)
#
# printenv is a separate executable that reads the environment it is
# actually handed at exec time, which is the only thing this test wants to
# know. An earlier cycle was lost to exactly this trap.
#
# The trailing "#" absorbs the provider's login subcommand: the wrapper
# appends `auth login` to the command tail, and bash treats everything after
# the # as a comment. So the probe stays `printenv` and nothing resembling a
# vendor login is ever executed.
WSL_PROBE_COMMAND = "printenv #"


def _parse_env_dump(text):
    """printenv output -> {name: value}. Values are never printed by callers."""
    env = {}
    for line in (text or "").splitlines():
        if "=" in line:
            name, _, value = line.partition("=")
            if name:
                env[name] = value
    return env


def _spawn_in_distro(box, wrapped):
    """Run a probe argv with the sandbox's env and report what it printed."""
    p = subprocess.run(wrapped, capture_output=True, text=True, timeout=180,
                       env=h_env(box), cwd=tempfile.gettempdir())
    return p, _parse_env_dump(p.stdout)


def wsl_child_report(raw=False):
    """Spawn ONE in-distro child and cache what it saw.

    raw=False  through ailogin's own wrapper -- the production path.
    raw=True   the unwrapped `wsl.exe ... bash -lic <cmd>` argv, i.e. exactly
               what the login ran BEFORE the fix. Used as a sensitivity
               control: if the raw child does not leak, this machine cannot
               demonstrate the bug and the wrapped assertions would be
               vacuous, so that is reported rather than silently passed.

    Returns (sandbox_dir, wsl_root, env_dict, returncode).
    """
    key = bool(raw)
    if key in _WSL_REPORTS:
        return _WSL_REPORTS[key]
    need_wsl()
    argv = _wsl_launcher_argv()
    dd = argv.index("--")
    rest = argv[dd + 1:]
    if len(rest) < 3 or "c" not in str(rest[1]):
        raise Skip("configured command is not a `bash -<flags>c <cmd>` shape")
    probe_argv = list(argv[:dd + 1]) + list(rest[:2]) + [WSL_PROBE_COMMAND]

    box = new_sandbox(WSL_PROVIDER)
    try:
        wsl_root = getattr(box, "wsl_root", None)
        if raw:
            wrapped = probe_argv
        else:
            wrap = getattr(ailogin, "_wrap_wsl_login_command", None)
            if wrap is None:
                # NOT a Skip. A module with no in-distro wrapper launches the
                # raw argv, and the raw argv is precisely what leaked the real
                # HOME and the rc file's ANTHROPIC* variables. Falling back to
                # it means the assertions below FAIL on such a module instead
                # of quietly excusing it -- which is the whole point of these
                # cases, since the Python-side tests passed throughout.
                finding("ailogin exposes no _wrap_wsl_login_command, so the "
                        "login command reaches the distro unmodified: HOME "
                        "cannot be forced past the interop boundary and the "
                        "shell rc's variables are never removed.")
                wrapped = probe_argv
            else:
                wrapped = wrap(WSL_PROVIDER, probe_argv, wsl_root)
        p, env = _spawn_in_distro(box, wrapped)
        out = (os.path.realpath(h_dir(box)), wsl_root, env, p.returncode)
    finally:
        teardown_quietly(box)
    _WSL_REPORTS[key] = out
    return out


def _linux_tail(win_path):
    """The distro-visible tail of a Windows sandbox path, lower-cased.

    The sandbox root is compared by its final component rather than by whole
    string: /mnt/c/... vs C:\... are the same directory seen from two sides,
    and the mount prefix is not what this test is about.
    """
    return os.path.basename(str(win_path or "").rstrip("\\/")).lower()


def w01_wsl_child_is_reachable():
    """The rig itself: an in-distro child really runs and really reports."""
    d, wsl_root, env, rc = wsl_child_report()
    if rc != 0:
        return False, "the in-distro probe exited %s" % rc
    if not env:
        return False, "the in-distro child printed no environment at all"
    return True, ("in-distro child ran and reported %d variables (probe was "
                  "`%s`, no vendor CLI involved)" % (len(env),
                                                     WSL_PROBE_COMMAND))


def w02_wslpath_survives_a_windows_path():
    """The backslash defect, asserted directly.

    create_sandbox translates the sandbox root with `wslpath`. It was passing
    the NATIVE path, backslashes and all; the interop layer consumes those as
    escapes, wslpath receives "C:Usersshaya..." and exits 1, and wsl_root
    silently becomes None -- which disables the in-distro HOME fix entirely.
    Two things are asserted: the translation succeeded at all, and a
    backslash-bearing path really is the thing that breaks it (so this case
    cannot pass for an unrelated reason).
    """
    need_wsl()
    d, wsl_root, _env, _rc = wsl_child_report()
    if not wsl_root:
        return False, ("create_sandbox produced no WSL-side root: `wslpath` "
                       "failed, so HOME cannot be forced inside the distro "
                       "and the sandbox is bypassed entirely")
    if _linux_tail(wsl_root) != _linux_tail(d):
        return False, ("WSL-side root %r does not name the sandbox directory"
                       % os.path.basename(str(wsl_root)))
    if "/mnt/" in wsl_root and wsl_root.count("/mnt/") > 1:
        return False, "WSL-side root was double-translated (/mnt/c/mnt/c/...)"
    # Now prove the failure mode is real and is about backslashes.
    back = subprocess.run(["wsl.exe", "wslpath", "-a", "-u", d],
                          capture_output=True, text=True, timeout=90)
    fwd = subprocess.run(["wsl.exe", "wslpath", "-a", "-u",
                          d.replace("\\", "/")],
                         capture_output=True, text=True, timeout=90)
    if fwd.returncode != 0:
        return False, "wslpath rejected even the forward-slash form"
    if back.returncode == 0:
        finding("wslpath accepted a backslash-bearing Windows path on this "
                "machine, so this case can no longer distinguish the fixed "
                "form from the broken one; the interop escaping behaviour may "
                "have changed and the assertion needs re-grounding.")
        return True, ("wsl_root resolved; backslash form also accepted here, "
                      "so the negative control did not fire")
    return True, ("wsl_root resolved from the forward-slash form; the "
                  "backslash form still exits %s (the original defect)"
                  % back.returncode)


def w03_in_distro_child_home_is_the_sandbox():
    """THE regression. What HOME does the process inside the distro see?

    Before the fix this was /home/shawn -- the user's real home, holding the
    session the CLI then reused, which is the entire reported bug.
    """
    d, wsl_root, env, rc = wsl_child_report()
    home = env.get("HOME") or ""
    if not home:
        return False, "the in-distro child reported no HOME at all"
    if not wsl_root:
        return False, ("HOME=%r: no WSL-side sandbox root was computed, so "
                       "nothing overrode the distro's own HOME" % home)
    if home.rstrip("/") != str(wsl_root).rstrip("/"):
        return False, ("the in-distro child sees HOME=%r, NOT the sandbox "
                       "root %r: the CLI would read the user's existing "
                       "session and sign the FIRST account in again"
                       % (home, wsl_root))
    if _linux_tail(home) != _linux_tail(d):
        return False, "HOME does not name this sandbox directory"
    return True, ("the in-distro child's HOME is the sandbox root (probed "
                  "with printenv, not echo -- see the comment above)")


def w04_in_distro_child_config_dir_is_the_sandbox():
    """CLAUDE_CONFIG_DIR must cross the boundary and land in the sandbox.

    Some CLIs honour the dedicated config variable and ignore HOME, so this
    is a second, independent route to the user's real credential file.
    """
    d, _wsl_root, env, _rc = wsl_child_report()
    spec = None
    try:
        spec = ailogin.provider_env_spec(WSL_PROVIDER)
    except Exception:
        pass
    var = (spec or {}).get("config_var") or "CLAUDE_CONFIG_DIR"
    val = env.get(var) or ""
    if not val:
        return False, ("%s never reached the in-distro child: a CLI that "
                       "honours it would read the real config directory"
                       % var)
    tail = _linux_tail(d)
    parts = [p.lower() for p in val.replace("\\", "/").split("/") if p]
    if tail not in parts:
        return False, ("%s=%r does not point inside this sandbox" % (var, val))
    if parts[-1] != str((spec or {}).get("config_rel") or ".claude").lower():
        finding("%s reached the child but its last component is %r rather "
                "than the configured config_rel" % (var, parts[-1]))
    return True, "%s points inside the sandbox on the distro side" % var


def w05_in_distro_child_sees_no_anthropic_vars():
    """The rc-file defect: the login child must see ZERO ANTHROPIC* names.

    The user's shell rc exports a proxy base URL and auth token. `bash -lic`
    sources that rc, so those variables exist INSIDE the distro no matter what
    the Windows parent's env dict contains -- and their presence puts the CLI
    into API mode, where the OAuth sign-in silently never happens and the
    login appears to "work" while adding nothing.

    Only the COUNT and the NAMES are reported; no value is ever read or
    printed. The user's rc files are not modified by this test -- the removal
    happens only in the login child's own command line.
    """
    _d, _wsl_root, env, _rc = wsl_child_report()
    names = sorted(k for k in env if k.upper().startswith("ANTHROPIC"))
    if names:
        return False, ("the in-distro login child still sees %d ANTHROPIC* "
                       "variable(s) (%s): the CLI runs in API mode and the "
                       "OAuth login never happens" % (len(names),
                                                      ", ".join(names)))
    return True, "the in-distro login child sees 0 ANTHROPIC* variables"


def w06_unwrapped_child_demonstrates_the_bug():
    """Sensitivity control: the probe must be able to SEE the old behaviour.

    Runs the same probe through the UNwrapped argv -- byte for byte what the
    login executed before the fix. That child is expected to report the user's
    real home and/or the rc file's ANTHROPIC* variables. If it does not, this
    machine cannot exhibit the defect at all and w03/w05 would be passing
    vacuously, which is reported as a Skip rather than a green tick.
    """
    d, _wsl_root, wrapped_env, _rc = wsl_child_report(raw=False)
    _d2, _wr2, raw_env, raw_rc = wsl_child_report(raw=True)
    if raw_rc != 0 or not raw_env:
        raise Skip("the unwrapped control child did not run")
    raw_home = raw_env.get("HOME") or ""
    raw_anth = sorted(k for k in raw_env if k.upper().startswith("ANTHROPIC"))
    leaks_home = _linux_tail(raw_home) != _linux_tail(_d2)
    if not leaks_home and not raw_anth:
        raise Skip("this machine's distro leaks neither HOME nor ANTHROPIC* "
                   "without the wrapper, so the wrapped assertions cannot be "
                   "shown to be non-vacuous here")
    wrapped_anth = [k for k in wrapped_env if k.upper().startswith("ANTHROPIC")]
    if leaks_home and _linux_tail(wrapped_env.get("HOME") or "") != _linux_tail(d):
        return False, "the wrapper did not change the HOME the child sees"
    if raw_anth and wrapped_anth:
        return False, "the wrapper did not remove the rc file's ANTHROPIC* vars"
    return True, ("unwrapped child leaks (home_outside_sandbox=%s, "
                  "ANTHROPIC*=%d); the wrapper removes both, so the "
                  "assertions above are not vacuous"
                  % (leaks_home, len(raw_anth)))


# ---- safety assertions, re-checked after everything has run ----------------
def z01_no_network_attempted():
    if NETWORK_ATTEMPTS:
        return False, "%d outbound attempt(s): %s" % (
            len(NETWORK_ATTEMPTS), ", ".join(sorted(set(NETWORK_ATTEMPTS))[:4]))
    return True, "no outbound network call was attempted"


def z02_no_credential_store_touched():
    if HARD_VIOLATIONS:
        return False, "%d attempt(s) to open a real credential store: %s" % (
            len(HARD_VIOLATIONS), ", ".join(sorted(set(HARD_VIOLATIONS))[:4]))
    return True, "no real credential store was opened"


def z03_no_opens_outside_the_tree():
    if OPEN_VIOLATIONS:
        return False, "%d open(s) outside the temp tree: %s" % (
            len(OPEN_VIOLATIONS), ", ".join(sorted(set(OPEN_VIOLATIONS))[:5]))
    return True, "every file opened lay inside the temp tree, the repo or the "\
                 "toolchain"


def z04_no_deletes_outside_the_tree():
    if DELETE_VIOLATIONS:
        return False, "%d deletion attempt(s) outside the temp tree: %s" % (
            len(DELETE_VIOLATIONS), ", ".join(sorted(set(DELETE_VIOLATIONS))[:4]))
    return True, "no deletion was attempted outside the temp tree"


def z05_vault_and_cred_paths_still_sandboxed():
    need_acct()
    if not inside_tmp(aiaccounts.accounts_file()):
        return False, "accounts_file() no longer resolves inside the temp tree"
    for provider in ("claude", "codex"):
        for p in aiaccounts.cli_cred_paths(provider):
            if not inside_tmp(p):
                return False, "a credential path escaped the temp tree during "\
                              "the run"
    return True, "vault and every credential path still inside the temp tree"


def z06_no_sandbox_left_behind():
    live = getattr(ailogin, "_LIVE", None) if ailogin is not None else None
    if isinstance(live, dict) and live:
        stranded = [p for p in live if os.path.exists(p)]
        if stranded:
            return False, "%d sandbox(es) still on disk after the run" % len(
                stranded)
    leftovers = []
    if os.path.isdir(SANDBOX_TMP):
        leftovers = [n for n in os.listdir(SANDBOX_TMP)
                     if n.startswith("ailogin-")]
    if leftovers:
        return False, "%d ailogin-* directory(ies) left in temp space" % len(
            leftovers)
    return True, "no sandbox directory survived the run"


# ===========================================================================
TESTS = [
    ("s01 harness: child process honours a home override", s01_probe_child_honours_home_override),
    ("s02 harness: deletes outside the tree raise", s02_delete_guard_refuses_outside_tree),
    ("s03 harness: opening a real cred store raises", s03_open_guard_refuses_real_cred_store),
    ("s04 harness: network tripwires fire", s04_network_tripwire_is_live),
    ("s05 harness: appended default cred paths sandboxed", s05_cred_paths_fully_sandboxed),
    ("s06 harness: module temp dir redirected into the tree", s06_module_tempdir_is_sandboxed),

    ("01 sandbox in temp space, not repo, not a config dir", t01_sandbox_in_temp_not_repo_not_config),
    ("02 sandbox permissions are restrictive", t02_sandbox_permissions_restrictive),
    ("03 env overrides reach a REAL child process", t03_env_overrides_reach_a_real_child),
    ("03b child RESOLVES ~ inside the sandbox", t03b_child_resolves_home_inside_the_sandbox),
    ("04 expected credential path is inside the sandbox", t04_expected_cred_path_inside_sandbox),
    ("05 successful capture returns the credential", t05_successful_capture_returns_cred_from_sandbox),
    ("06 teardown removes the sandbox and the credential", t06_teardown_removes_sandbox_and_credential),
    ("07 teardown still happens after a CLI error", t07_teardown_after_cli_error),
    ("08 teardown still happens after the child is killed", t08_teardown_after_child_killed),
    ("09 teardown still happens after a timeout", t09_teardown_after_timeout),
    ("09b timeout + kill leaves nothing on disk", t09b_teardown_after_timeout_then_kill),
    ("10 cancel / cli-error / timeout stay distinct", t10_three_failure_reasons_stay_distinct),
    ("11 duplicate: same account twice -> same", t11_duplicate_same_account_twice),
    ("12 duplicate: two different accounts -> new", t12_duplicate_two_different_accounts),
    ("13 duplicate: identity survives token rotation", t13_duplicate_survives_token_rotation),
    ("14 duplicate: other providers do not match", t14_duplicate_ignores_other_providers),
    ("15 duplicate: unknown shape is not called new", t15_duplicate_unrecognisable_cred_is_not_new),
    ("16 refuses paths in home/config dirs and the repo", t16_refuses_path_in_real_home_config_dir),
    ("17 guard still accepts a legitimate temp path", t17_guard_accepts_a_legitimate_temp_path),
    ("18 capability: planned providers have no login", t18_capability_false_for_planned_providers),
    ("19 capability: unknown/junk slugs return False", t19_capability_false_for_unknown_and_junk),
    ("20 capability: live providers answer with a bool", t20_capability_is_a_real_bool),
    ("21 launch refuses an unknown provider", t21_launch_refuses_unsupported_provider),
    ("22 launch refuses a destroyed sandbox", t22_launch_refuses_a_destroyed_sandbox),
    ("23 two sandboxes are independent", t23_two_sandboxes_are_independent),
    ("24 teardown is idempotent", t24_teardown_is_idempotent),

    ("w01 WSL: a real in-distro child runs and reports", w01_wsl_child_is_reachable),
    ("w02 WSL: wslpath converts the sandbox root (backslash bug)", w02_wslpath_survives_a_windows_path),
    ("w03 WSL: in-distro child HOME is the sandbox, not ~shawn", w03_in_distro_child_home_is_the_sandbox),
    ("w04 WSL: in-distro child config dir is in the sandbox", w04_in_distro_child_config_dir_is_the_sandbox),
    ("w05 WSL: in-distro child sees 0 ANTHROPIC* variables", w05_in_distro_child_sees_no_anthropic_vars),
    ("w06 WSL: unwrapped child still shows the old leak", w06_unwrapped_child_demonstrates_the_bug),

    ("z01 safety: no network request was attempted", z01_no_network_attempted),
    ("z02 safety: no real credential store was opened", z02_no_credential_store_touched),
    ("z03 safety: no file opened outside the temp tree", z03_no_opens_outside_the_tree),
    ("z04 safety: no deletion outside the temp tree", z04_no_deletes_outside_the_tree),
    ("z05 safety: vault + cred paths still sandboxed", z05_vault_and_cred_paths_still_sandboxed),
    ("z06 safety: no sandbox left behind on disk", z06_no_sandbox_left_behind),
]


def safety_gate():
    """Hard preconditions. Returns an abort message, or None to proceed."""
    if not inside_tmp(FAKE_HOME) or not inside_tmp(VAULT):
        return "the fake home / vault are not inside the temp tree"
    if not inside_tmp(tempfile.gettempdir()):
        return "tempfile.gettempdir() is outside the temp tree"
    if NC(os.path.realpath(os.path.expanduser("~"))) != NC(os.path.realpath(FAKE_HOME)):
        return ("expanduser('~') does not resolve to the fake home; the "
                "environment redirect did not take")
    if aiaccounts is not None:
        if hasattr(aiaccounts, "accounts_file") and \
                not inside_tmp(aiaccounts.accounts_file()):
            return "accounts_file() resolved OUTSIDE the temp tree"
        if hasattr(aiaccounts, "cli_cred_paths"):
            for provider in ("claude", "codex"):
                for p in aiaccounts.cli_cred_paths(provider):
                    if not inside_tmp(p):
                        return ("cli_cred_paths(%s) resolved %r outside the "
                                "temp tree: the appended-default trap is still "
                                "open" % (provider, os.path.basename(p)))
    if ailogin is not None and hasattr(ailogin, "_PROVIDER_ENV"):
        if PROVIDER not in ailogin._PROVIDER_ENV:
            return "the provider used by the tests is not in the override table"
    return None


def main():
    print("net-watch-widget :: isolated login sandbox regression harness")
    print("python %s" % sys.version.split()[0])
    print("temp tree : <temp>/%s" % os.path.basename(TMPDIR))
    print("")

    abort = safety_gate()
    if abort is not None:
        print("ABORT: %s" % abort)
        print("       Refusing to run: the harness could touch real logins.")
        cleanup()
        return 2

    print("safety: fake home, vault and sandbox temp dir all inside the tree")
    print("safety: expanduser('~') resolves into the temp tree")
    print("safety: cli_cred_paths() incl. the appended default is sandboxed")
    for n in NEUTRALISED:
        print("safety: %s" % n)
    print("safety: outbound network disabled (urlopen + socket tripwires)")
    print("safety: open()/os.open guarded; deletes outside the tree raise")
    print("safety: the real vendor login command is never launched")
    print("safety: no token value, real or fake, is printed")
    for mod, err in sorted(IMPORT_ERRORS.items()):
        print("import: %s failed -- %s" % (mod, err))

    resolve_roles()
    if ailogin is not None:
        for r in ("capability", "create", "launch", "wait", "teardown",
                  "duplicate", "pathguard"):
            print("contract: %-11s -> %s"
                  % (r, ROLES[r][0] if r in ROLES else "NOT FOUND"))
    print("")

    write_probe()
    install_fs_guards()
    try:
        for name, fn in TESTS:
            check(name, fn)
    finally:
        remove_fs_guards()

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
        print("skipped (NOT executed -- no fabricated pass):")
        for s, n, d in RESULTS:
            if s == "SKIP":
                print("  - %s :: %s" % (n, d))
    if FINDINGS:
        print("")
        print("findings (contract vs implementation):")
        for f in FINDINGS:
            print("  - %s" % f)
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
