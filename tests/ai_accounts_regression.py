#!/usr/bin/env python3
"""Standalone regression harness for the multi-account AI usage feature.

Runs with a bare interpreter: standard library only, no pytest, no psutil.

    python tests/ai_accounts_regression.py

What it covers
--------------
The vault (aiaccounts.py) and the provider layer (aiproviders.py). Both hold
live logins, so the harness is written defensively:

  * AI_ACCOUNTS_FILE is redirected into a throwaway temp directory before the
    modules under test are imported, and the resolved vault path is asserted to
    live inside that directory. If the assertion fails the run aborts before a
    single byte is written -- a harness that writes into the real vault would
    destroy the exact thing the feature exists to protect.
  * The HTTP layer is stubbed and, as a second independent layer,
    urllib.request.urlopen and socket.socket.connect are replaced with
    tripwires. Both providers geo-block the user's country; a real request from
    a test run risks the actual account.
  * Credential file reads performed by the code under test are wrapped with a
    guard that refuses any path outside the temp directory, and the guard's
    refusals are themselves asserted on.
  * No token value, fake or otherwise, is ever printed. Fake secrets are
    written as obvious placeholders and only their PRESENCE is reported.

Missing or half-written modules are reported as SKIP with a reason rather than
crashing the run. Nothing under test is stubbed to make an assertion pass: the
only monkeypatching is the network kill-switch, the filesystem read guard, and
the WSL-share enumerator (a discovery helper that would otherwise reach into
real credential stores).

Tests are written against the documented contract, not against the
implementation. A disagreement between the two is a finding, not a reason to
soften the test.
"""

import datetime
import io
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import time
import traceback
import urllib.request

REPO_ROOT = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))

# Obvious placeholders. Nothing here is or resembles a real secret.
FAKE_ACCESS = "FAKE-ACCESS-TOKEN-DO-NOT-USE"
FAKE_REFRESH = "FAKE-REFRESH-TOKEN-DO-NOT-USE"
FAKE_ROTATED_REFRESH = "FAKE-ROTATED-REFRESH-TOKEN-DO-NOT-USE"
FAKE_ROTATED_ACCESS = "FAKE-ROTATED-ACCESS-TOKEN-DO-NOT-USE"


# ---------------------------------------------------------------------------
# result bookkeeping
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
    """Run one test body. Returns nothing; never propagates."""
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
    """Skip unless the module imported and carries every named attribute."""
    if mod is None:
        raise Skip("module not importable")
    missing = [a for a in attrs if not hasattr(mod, a)]
    if missing:
        raise Skip("missing from module: %s" % ", ".join(missing))


# ---------------------------------------------------------------------------
# sandbox: this must happen before anything under test is imported
# ---------------------------------------------------------------------------
TMPDIR = os.path.realpath(tempfile.mkdtemp(prefix="nw_ai_vault_"))
VAULT = os.path.join(TMPDIR, "vault", "accounts.json")
FAKE_HOME = os.path.join(TMPDIR, "home")
os.makedirs(os.path.dirname(VAULT), exist_ok=True)
os.makedirs(FAKE_HOME, exist_ok=True)

os.environ["AI_ACCOUNTS_FILE"] = VAULT
# Redirect every notion of "home" the CLI-import path might consult, so no
# discovery walk can reach the user's real credential files.
for var in ("HOME", "USERPROFILE"):
    os.environ[var] = FAKE_HOME
os.environ["HOMEDRIVE"] = ""
os.environ["HOMEPATH"] = FAKE_HOME
os.environ["APPDATA"] = os.path.join(TMPDIR, "appdata")
os.environ["CLAUDE_CRED_PATHS"] = ""
os.environ["CODEX_CRED_PATHS"] = ""


def inside_tmp(path):
    try:
        rp = os.path.realpath(path)
    except Exception:
        return False
    return os.path.normcase(rp).startswith(os.path.normcase(TMPDIR) + os.sep)


def cleanup():
    shutil.rmtree(TMPDIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# network kill-switch (layer 2; the HTTP stub below is layer 1)
# ---------------------------------------------------------------------------
NETWORK_ATTEMPTS = []


def _blocked_urlopen(*a, **k):
    NETWORK_ATTEMPTS.append("urlopen")
    raise RuntimeError("network access is blocked inside the test harness")


_real_connect = socket.socket.connect


def _blocked_connect(self, addr, *a, **k):
    # Loopback is left alone so nothing local (should it ever be needed)
    # breaks; anything outbound is refused and recorded.
    host = addr[0] if isinstance(addr, tuple) and addr else ""
    if host in ("127.0.0.1", "::1", "localhost"):
        return _real_connect(self, addr, *a, **k)
    NETWORK_ATTEMPTS.append("connect:%s" % (host,))
    raise RuntimeError("network access is blocked inside the test harness")


urllib.request.urlopen = _blocked_urlopen
socket.socket.connect = _blocked_connect


# ---------------------------------------------------------------------------
# import the modules under test
# ---------------------------------------------------------------------------
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

aiaccounts = None
aiproviders = None
IMPORT_ERRORS = {}

try:
    import aiaccounts  # noqa: F811
except Exception as e:
    IMPORT_ERRORS["aiaccounts"] = "%s: %s" % (type(e).__name__, e)
try:
    import aiproviders  # noqa: F811
except Exception as e:
    IMPORT_ERRORS["aiproviders"] = "%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------------------
# HTTP stub (layer 1) -- installed over the provider module's transport
# ---------------------------------------------------------------------------
HTTP = {"resp": (0, None, "StubbedTransport"), "calls": []}


def _stub_http_json(url, headers, data=None, timeout=None):
    HTTP["calls"].append(url)
    r = HTTP["resp"]
    if callable(r):
        return r(url, headers, data)
    return r


HTTP_STUB_INSTALLED = False
if aiproviders is not None and hasattr(aiproviders, "_http_json"):
    aiproviders._http_json = _stub_http_json
    HTTP_STUB_INSTALLED = True


def set_http(status, parsed, raw):
    HTTP["resp"] = (status, parsed, raw)


# ---------------------------------------------------------------------------
# filesystem read guard for the CLI-import path
# ---------------------------------------------------------------------------
GUARD_VIOLATIONS = []


def install_read_guard():
    """Wrap aiaccounts._read_json so it can only read inside the sandbox."""
    if aiaccounts is None or not hasattr(aiaccounts, "_read_json"):
        return False
    original = aiaccounts._read_json

    def guarded(path):
        if not inside_tmp(path):
            GUARD_VIOLATIONS.append(path)
            return None
        return original(path)

    aiaccounts._read_json = guarded
    return True


def disable_wsl_walk():
    """Stop the import path enumerating real home directories.

    This is a discovery helper, not the logic under test; leaving it live
    would have the harness stat real credential files on a share.
    """
    if aiaccounts is None or not hasattr(aiaccounts, "_wsl_homes"):
        return False
    aiaccounts._wsl_homes = lambda: []
    return True


# ---------------------------------------------------------------------------
# credential fixtures
# ---------------------------------------------------------------------------
def claude_cred(expires_at_s=None, refresh=True, sub="max"):
    o = {
        "accessToken": FAKE_ACCESS,
        "expiresAt": int((expires_at_s if expires_at_s is not None
                          else time.time() + 3600) * 1000),
        "subscriptionType": sub,
        "scopes": ["user:inference"],
    }
    if refresh:
        o["refreshToken"] = FAKE_REFRESH
    return {"claudeAiOauth": o}


def codex_cred(account_id="acct-fake-0001", refresh=True):
    t = {"access_token": FAKE_ACCESS, "account_id": account_id}
    if refresh:
        t["refresh_token"] = FAKE_REFRESH
    return {"tokens": t}


def reset_vault():
    """Empty the sandbox vault directory between tests."""
    d = os.path.dirname(VAULT)
    for name in os.listdir(d):
        try:
            os.remove(os.path.join(d, name))
        except OSError:
            pass


def vault_dir_listing():
    return sorted(os.listdir(os.path.dirname(VAULT)))


# ===========================================================================
# vault mechanics
# ===========================================================================
def t01_missing_vault_is_empty():
    need(aiaccounts, "load", "accounts_file")
    reset_vault()
    doc = aiaccounts.load()
    ok = isinstance(doc, dict) and doc.get("accounts") == []
    return ok, "load() -> %r" % (list(doc) if isinstance(doc, dict) else doc,)


def t02_corrupt_vault_is_empty():
    need(aiaccounts, "load")
    reset_vault()
    with open(VAULT, "wb") as f:
        f.write(b'{"version": 1, "accounts": [{"id": "a", "cr\x00\xff truncated')
    doc = aiaccounts.load()
    ok = isinstance(doc, dict) and doc.get("accounts") == []
    return ok, "garbage bytes -> empty store, no exception"


def t03_add_account_gets_id_and_is_listed():
    need(aiaccounts, "add_account", "list_accounts")
    reset_vault()
    a = aiaccounts.add_account("claude", "personal", claude_cred())
    if not isinstance(a, dict) or not a.get("id"):
        return False, "add_account returned no id"
    ids = [x.get("id") for x in aiaccounts.list_accounts()]
    required = ("id", "provider", "label", "added_at", "source", "cred")
    missing = [k for k in required if k not in a]
    if missing:
        return False, "returned dict missing contract keys: %s" % missing
    return a["id"] in ids, "id present and listed"


def t04_update_cred_survives_reload():
    need(aiaccounts, "add_account", "update_cred", "get_account")
    reset_vault()
    a = aiaccounts.add_account("claude", "personal", claude_cred())
    new = claude_cred(expires_at_s=time.time() + 7200)
    new["claudeAiOauth"]["refreshToken"] = FAKE_ROTATED_REFRESH
    rv = aiaccounts.update_cred(a["id"], new)
    got = aiaccounts.get_account(a["id"])  # get_account re-reads from disk
    stored = (got or {}).get("cred", {}).get("claudeAiOauth", {})
    ok = (rv is not False
          and stored.get("refreshToken") == FAKE_ROTATED_REFRESH
          and stored.get("expiresAt") == new["claudeAiOauth"]["expiresAt"])
    return ok, "rotated credential re-read from disk intact"


def t05_remove_account_booleans():
    need(aiaccounts, "add_account", "remove_account", "list_accounts")
    reset_vault()
    a = aiaccounts.add_account("codex", "work", codex_cred())
    first = aiaccounts.remove_account(a["id"])
    gone = all(x.get("id") != a["id"] for x in aiaccounts.list_accounts())
    second = aiaccounts.remove_account("no-such-id-0000")
    ok = (first is True) and gone and (second is False)
    return ok, "remove=%r, removed_from_list=%r, remove_missing=%r" % (
        first, gone, second)


def t06_rename_preserves_cred():
    need(aiaccounts, "add_account", "rename_account", "get_account")
    reset_vault()
    cred = claude_cred()
    a = aiaccounts.add_account("claude", "old-label", cred)
    rv = aiaccounts.rename_account(a["id"], "new-label")
    got = aiaccounts.get_account(a["id"]) or {}
    ok = (rv is not False and got.get("label") == "new-label"
          and got.get("cred") == cred)
    return ok, "label changed, credential byte-identical"


def t07_same_provider_multiple_accounts():
    need(aiaccounts, "add_account", "list_accounts")
    reset_vault()
    a = aiaccounts.add_account("claude", "personal", claude_cred(sub="max"))
    b = aiaccounts.add_account("claude", "work", claude_cred(sub="pro"))
    c = aiaccounts.add_account("claude", "client", claude_cred(sub="team"))
    rows = [x for x in aiaccounts.list_accounts() if x.get("provider") == "claude"]
    ids = {x["id"] for x in rows}
    labels = {x["label"] for x in rows}
    ok = (len(rows) == 3 and ids == {a["id"], b["id"], c["id"]}
          and labels == {"personal", "work", "client"})
    return ok, "%d claude accounts, %d distinct labels" % (len(rows), len(labels))


def t08_no_sidecar_files():
    need(aiaccounts, "add_account", "update_cred", "save", "load")
    reset_vault()
    a = aiaccounts.add_account("claude", "personal", claude_cred())
    for i in range(3):
        aiaccounts.update_cred(a["id"], claude_cred(expires_at_s=time.time() + i))
    listing = vault_dir_listing()
    strays = [n for n in listing
              if n != os.path.basename(VAULT)
              and (n.endswith(".bak") or n.endswith(".tmp")
                   or "backup" in n.lower() or n.endswith("~"))]
    extra = [n for n in listing if n != os.path.basename(VAULT)]
    ok = not strays and not extra
    return ok, "vault dir contains %r" % (listing,)


def t08b_restrictive_permissions():
    need(aiaccounts, "add_account")
    reset_vault()
    aiaccounts.add_account("claude", "personal", claude_cred())
    if os.name == "nt":
        raise Skip("POSIX mode bits are not enforced on Windows; "
                   "checked the 0600 open path by inspection only")
    mode = stat.S_IMODE(os.stat(VAULT).st_mode)
    ok = (mode & 0o077) == 0
    return ok, "mode=%o (group/other bits must be clear)" % mode


def t09_failed_save_preserves_vault():
    need(aiaccounts, "add_account", "save", "load", "list_accounts")
    reset_vault()
    aiaccounts.add_account("claude", "keeper", claude_cred())
    before_bytes = open(VAULT, "rb").read()

    # A credential that json cannot serialise makes the write fail partway:
    # the temp file is opened and partially written, then the dump raises.
    doc = aiaccounts.load()
    doc["accounts"].append({"id": "bad", "provider": "claude",
                            "label": "poison", "cred": {1, 2, 3}})
    try:
        aiaccounts.save(doc)
    except Exception:
        pass  # save() is documented as fire-and-forget; either way is fine

    after_bytes = open(VAULT, "rb").read()
    reloaded = aiaccounts.load()
    labels = [a.get("label") for a in reloaded["accounts"]]
    strays = [n for n in vault_dir_listing() if n != os.path.basename(VAULT)]
    ok = (after_bytes == before_bytes and labels == ["keeper"] and not strays)
    return ok, "vault unchanged=%r, labels=%r, strays=%r" % (
        after_bytes == before_bytes, labels, strays)


# ===========================================================================
# provider layer
# ===========================================================================
# The live set grows as each vendor gains a VERIFIED endpoint. Pinning it here
# rather than deriving it from PROVIDERS is deliberate: a provider silently
# flipping to live without a reviewed adapter is exactly what this asserts against.
EXPECTED_LIVE = {"claude", "codex", "cursor", "copilot",
                 "windsurf", "devin", "kimi", "cline"}


def t10_registry_shape():
    need(aiproviders, "PROVIDERS", "provider_ids", "provider")
    ids = aiproviders.provider_ids()
    live = {p["id"] for p in aiproviders.PROVIDERS if p.get("status") == "live"}
    ok = (len(aiproviders.PROVIDERS) == 12 and len(set(ids)) == 12
          and live == EXPECTED_LIVE)
    return ok, "%d providers, live=%s" % (len(aiproviders.PROVIDERS),
                                          sorted(live))


def t10b_provider_lookup():
    need(aiproviders, "provider", "provider_ids")
    ids = aiproviders.provider_ids()
    ok = all(isinstance(aiproviders.provider(i), dict) for i in ids)
    ok = ok and aiproviders.provider("definitely-not-a-provider") is None
    return ok, "provider() resolves every id, unknown id -> None"


def t11_planned_provider_not_supported():
    need(aiproviders, "fetch_usage", "PROVIDERS")
    planned = [p["id"] for p in aiproviders.PROVIDERS
               if p.get("status") != "live"]
    if not planned:
        return False, "no planned providers registered"
    bad = []
    for pid in planned:
        out = aiproviders.fetch_usage(pid, {})
        if not isinstance(out, dict) or out.get("error") != "not supported yet":
            bad.append((pid, out))
    return not bad, "%d planned providers, mismatches=%r" % (len(planned), bad)


def t12_fetch_usage_never_raises():
    need(aiproviders, "fetch_usage", "provider_ids")
    if not HTTP_STUB_INSTALLED:
        raise Skip("could not stub aiproviders._http_json; refusing to call "
                   "fetch_usage without a guaranteed-offline transport")
    set_http(0, None, "StubbedTransport")
    junk = [None, {}, [], "", 0, {"claudeAiOauth": None},
            {"claudeAiOauth": {}}, {"tokens": "not-a-dict"},
            {"tokens": {}}, {"unexpected": {"shape": True}},
            {"claudeAiOauth": {"accessToken": FAKE_ACCESS}}]
    bad = []
    for pid in list(aiproviders.provider_ids()) + ["no-such-provider"]:
        for c in junk:
            try:
                out = aiproviders.fetch_usage(pid, c)
            except Exception as e:
                bad.append((pid, type(e).__name__))
                continue
            if not isinstance(out, dict):
                bad.append((pid, "non-dict %r" % type(out).__name__))
            elif "error" not in out:
                bad.append((pid, "no error key for junk credential"))
    return not bad, "%d provider/credential combinations, problems=%r" % (
        (len(aiproviders.provider_ids()) + 1) * len(junk), bad[:4])


def _refresh_reason(pid, cred, status, parsed, raw):
    set_http(status, parsed, raw)
    new, why = aiproviders.refresh_cred(pid, cred)
    return new, why


def t13_failure_classification_distinct():
    need(aiproviders, "refresh_cred")
    if not HTTP_STUB_INSTALLED:
        raise Skip("could not stub aiproviders._http_json")
    reasons = {}
    for pid, cred in (("claude", claude_cred()), ("codex", codex_cred())):
        body400 = '{"error":"invalid_grant","error_description":"expired"}'
        _, dead400 = _refresh_reason(pid, cred, 400, json.loads(body400), body400)
        _, dead401 = _refresh_reason(pid, cred, 401, json.loads(body400), body400)
        _, off = _refresh_reason(pid, cred, 0, None, "URLError")
        _, srv = _refresh_reason(pid, cred, 500, None, "upstream boom")
        reasons[pid] = (dead400, dead401, off, srv)
        problems = []
        if dead400 != "login expired":
            problems.append("400/invalid_grant -> %r" % dead400)
        if dead401 != "login expired":
            problems.append("401/invalid_grant -> %r" % dead401)
        if off != "offline":
            problems.append("transport failure -> %r" % off)
        if not (isinstance(srv, str) and "refresh failed" in srv
                and "500" in srv):
            problems.append("500 -> %r" % srv)
        if len({dead400, off, srv}) != 3:
            problems.append("reasons collapsed: %r" % ((dead400, off, srv),))
        if problems:
            return False, "%s: %s" % (pid, "; ".join(problems))
    return True, "claude+codex both: %r" % (reasons["claude"][0::2],)


def t13b_bare_400_is_not_login_expired():
    need(aiproviders, "refresh_cred")
    if not HTTP_STUB_INSTALLED:
        raise Skip("could not stub aiproviders._http_json")
    _, why = _refresh_reason("claude", claude_cred(), 400, None,
                             '{"error":"malformed_request"}')
    ok = why != "login expired" and "refresh failed" in (why or "")
    return ok, "400 without a grant marker -> %r" % (why,)


def t14_refresh_returns_complete_rotated_cred():
    need(aiproviders, "refresh_cred")
    if not HTTP_STUB_INSTALLED:
        raise Skip("could not stub aiproviders._http_json")
    problems = []

    cred = claude_cred()
    cred["claudeAiOauth"]["subscriptionType"] = "max"
    cred["extraTopLevelKey"] = {"keep": "me"}
    set_http(200, {"access_token": FAKE_ROTATED_ACCESS,
                   "refresh_token": FAKE_ROTATED_REFRESH,
                   "expires_in": 3600,
                   "scope": "user:inference user:profile"}, "{}")
    new, why = aiproviders.refresh_cred("claude", cred)
    if why is not None or not isinstance(new, dict):
        problems.append("claude: why=%r type=%r" % (why, type(new).__name__))
    else:
        o = new.get("claudeAiOauth") or {}
        if "refreshToken" not in o:
            problems.append("claude: rotated refresh token DROPPED")
        elif o["refreshToken"] != FAKE_ROTATED_REFRESH:
            problems.append("claude: refresh token not rotated")
        if o.get("accessToken") != FAKE_ROTATED_ACCESS:
            problems.append("claude: access token not updated")
        if o.get("subscriptionType") != "max":
            problems.append("claude: unrelated field lost")
        if new.get("extraTopLevelKey") != {"keep": "me"}:
            problems.append("claude: top-level key lost (shape not preserved)")
        if new is cred:
            problems.append("claude: mutated the caller's object in place")
        if (cred["claudeAiOauth"].get("refreshToken") != FAKE_REFRESH):
            problems.append("claude: input credential mutated")

    ccred = codex_cred()
    ccred["last_refresh"] = "2026-01-01T00:00:00Z"
    set_http(200, {"access_token": FAKE_ROTATED_ACCESS,
                   "refresh_token": FAKE_ROTATED_REFRESH,
                   "id_token": "FAKE.ID.TOKEN"}, "{}")
    new2, why2 = aiproviders.refresh_cred("codex", ccred)
    if why2 is not None or not isinstance(new2, dict):
        problems.append("codex: why=%r" % (why2,))
    else:
        t = new2.get("tokens") or {}
        if "refresh_token" not in t:
            problems.append("codex: rotated refresh token DROPPED")
        elif t["refresh_token"] != FAKE_ROTATED_REFRESH:
            problems.append("codex: refresh token not rotated")
        if t.get("account_id") != "acct-fake-0001":
            problems.append("codex: account_id lost")
        if new2.get("last_refresh") != "2026-01-01T00:00:00Z":
            problems.append("codex: top-level key lost")

    return not problems, ("complete rotated credential returned for both"
                          if not problems else "; ".join(problems))


def t14b_refresh_rotation_survives_vault_roundtrip():
    """The rotated token is only safe once it is on disk."""
    need(aiproviders, "refresh_cred")
    need(aiaccounts, "add_account", "update_cred", "get_account")
    if not HTTP_STUB_INSTALLED:
        raise Skip("could not stub aiproviders._http_json")
    reset_vault()
    a = aiaccounts.add_account("claude", "personal", claude_cred())
    set_http(200, {"access_token": FAKE_ROTATED_ACCESS,
                   "refresh_token": FAKE_ROTATED_REFRESH,
                   "expires_in": 3600}, "{}")
    new, why = aiproviders.refresh_cred("claude", a["cred"])
    if why is not None:
        return False, "refresh failed: %r" % (why,)
    aiaccounts.update_cred(a["id"], new)
    stored = (aiaccounts.get_account(a["id"]) or {}).get("cred", {})
    got = (stored.get("claudeAiOauth") or {}).get("refreshToken")
    return got == FAKE_ROTATED_REFRESH, "rotated refresh token persisted=%r" % (
        got == FAKE_ROTATED_REFRESH,)


def t15_is_unrecoverable_window():
    need(aiproviders, "is_unrecoverable", "CRED_MAX_STALE")
    mx = aiproviders.CRED_MAX_STALE
    now = time.time()
    cases = [
        ("claude far past max stale", "claude",
         claude_cred(expires_at_s=now - mx - 86400), True),
        ("claude freshly expired", "claude",
         claude_cred(expires_at_s=now - 60), False),
        ("claude still valid", "claude",
         claude_cred(expires_at_s=now + 3600), False),
        ("claude expired inside window", "claude",
         claude_cred(expires_at_s=now - (mx / 2)), False),
        ("claude gutted (no refresh token)", "claude",
         claude_cred(expires_at_s=0, refresh=False), True),
        ("unknown shape is not dead", "claude", {"nonsense": 1}, False),
    ]
    bad = []
    for name, pid, cred, expect in cases:
        got = aiproviders.is_unrecoverable(pid, cred)
        if bool(got) != expect:
            bad.append("%s: expected %r got %r" % (name, expect, got))
    return not bad, ("max_stale=%ds; %d cases" % (mx, len(cases))
                     if not bad else "; ".join(bad))


# ===========================================================================
# cross-layer
# ===========================================================================
def t16_import_from_cli_idempotent():
    need(aiaccounts, "import_from_cli", "list_accounts")
    if not inside_tmp(os.path.expanduser("~")):
        raise Skip("expanduser('~') did not redirect into the sandbox; "
                   "refusing to run an import that could read real "
                   "credential files")
    if not install_read_guard():
        raise Skip("could not install the credential read guard")
    disable_wsl_walk()
    reset_vault()

    cdir = os.path.join(FAKE_HOME, ".claude")
    xdir = os.path.join(FAKE_HOME, ".codex")
    os.makedirs(cdir, exist_ok=True)
    os.makedirs(xdir, exist_ok=True)
    cpath = os.path.join(cdir, ".credentials.json")
    xpath = os.path.join(xdir, "auth.json")
    with open(cpath, "w", encoding="utf-8") as f:
        json.dump(claude_cred(expires_at_s=time.time() + 3600), f)
    with open(xpath, "w", encoding="utf-8") as f:
        json.dump(codex_cred(account_id="acct-fake-0001"), f)
    os.environ["CLAUDE_CRED_PATHS"] = cpath
    os.environ["CODEX_CRED_PATHS"] = xpath

    first = aiaccounts.import_from_cli()
    n1 = len(aiaccounts.list_accounts())
    second = aiaccounts.import_from_cli()
    n2 = len(aiaccounts.list_accounts())
    third = aiaccounts.import_from_cli()
    n3 = len(aiaccounts.list_accounts())

    ok = (n1 == 2 and n2 == 2 and n3 == 2
          and len(first) == 2 and second == [] and third == [])
    return ok, "counts after 1/2/3 imports = %d/%d/%d, new rows = %d/%d/%d" % (
        n1, n2, n3, len(first), len(second), len(third))


def t17_no_network_attempted():
    return not NETWORK_ATTEMPTS, (
        "no outbound request attempted"
        if not NETWORK_ATTEMPTS
        else "TRIPWIRE FIRED: %r" % (NETWORK_ATTEMPTS[:3],))


def t18_no_reads_outside_sandbox():
    return not GUARD_VIOLATIONS, (
        "no credential read outside the sandbox"
        if not GUARD_VIOLATIONS
        else "TRIPWIRE FIRED on %d path(s)" % len(GUARD_VIOLATIONS))


def t19_vault_path_still_sandboxed():
    need(aiaccounts, "accounts_file")
    p = aiaccounts.accounts_file()
    return inside_tmp(p), "accounts_file() resolves inside the sandbox"


# ===========================================================================
# durable deletion -- the tombstone lifecycle
# ===========================================================================
# Why this needs its own block rather than folding into t05/t16: remove_account()
# returning True and dropping the row is not the property the user cares about.
# What he reported is that an account he deleted comes back on its own, so the
# property under test is "deleted stays deleted across the next CLI import, and
# across the vault document itself being wiped". That is a lifecycle, and it
# needs the CLI-import fixture rather than a bare vault.
#
# Every test here runs against the sandbox vault (AI_ACCOUNTS_FILE, set at the
# top of this file before aiaccounts was imported) and tears down BOTH the vault
# and its sibling tombstone file. A leaked .forgotten is the one failure mode
# that would make a later test pass for the wrong reason: it would still be
# blocking an import the next test believes it un-blocked.

TOMBSTONE_API = ("forget_account", "is_tombstoned", "list_forgotten",
                 "allow_reimport")


def tombstone_file():
    """The sibling tombstone path, recomputed from the sandbox vault path.

    Deliberately derived here instead of asked of the module: if the store ever
    moves (back into the vault document, or somewhere outside the sandbox) these
    tests must notice rather than follow it there.
    """
    return VAULT + ".forgotten"


def missing_tombstone_api():
    if aiaccounts is None:
        return list(TOMBSTONE_API)
    return [a for a in TOMBSTONE_API if not hasattr(aiaccounts, a)]


def require_tombstone_api():
    """Absence of the durable-delete API is a FAIL, not a SKIP.

    The rest of this harness skips what is not built yet. This one does not:
    these functions exist because "delete" did not stick, so a build without
    them is a build with the original bug, and reporting that as "skipped"
    would hide exactly the regression this block is here to catch.
    """
    missing = missing_tombstone_api()
    if missing:
        return "durable-delete API missing from aiaccounts: %s" % (
            ", ".join(missing),)
    return None


def reset_vault_and_tombstones():
    """Teardown: the vault AND the sibling tombstone file, proven empty."""
    reset_vault()
    leftovers = sorted(os.listdir(os.path.dirname(VAULT)))
    if leftovers:
        raise AssertionError("sandbox vault dir not clean: %r" % (leftovers,))
    if os.path.exists(tombstone_file()):
        raise AssertionError("tombstone file survived teardown")


_READ_GUARD_ARMED = []


def cli_sandbox():
    """Arm the CLI-import sandbox, or Skip if it cannot be trusted.

    Same gate t16 uses: without the redirected home and the read guard, an
    import walk would stat the user's real credential files.
    """
    if not inside_tmp(os.path.expanduser("~")):
        raise Skip("expanduser('~') did not redirect into the sandbox; "
                   "refusing to run an import that could read real "
                   "credential files")
    if not _READ_GUARD_ARMED:
        if not install_read_guard():
            raise Skip("could not install the credential read guard")
        _READ_GUARD_ARMED.append(True)
    disable_wsl_walk()


def write_cli_creds(claude=None, codex=None):
    """Lay fake CLI credential files inside the sandbox; return their paths.

    Passing None for a provider removes that file, so a test can narrow the
    import to one provider. The env vars are pointed at the sandbox paths on
    every call, because a previous test may have left them elsewhere.
    """
    cdir = os.path.join(FAKE_HOME, ".claude")
    xdir = os.path.join(FAKE_HOME, ".codex")
    os.makedirs(cdir, exist_ok=True)
    os.makedirs(xdir, exist_ok=True)
    cpath = os.path.join(cdir, ".credentials.json")
    xpath = os.path.join(xdir, "auth.json")
    for path, cred in ((cpath, claude), (xpath, codex)):
        if cred is None:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cred, f)
    os.environ["CLAUDE_CRED_PATHS"] = cpath
    os.environ["CODEX_CRED_PATHS"] = xpath
    return cpath, xpath


def first_of(rows, provider):
    for a in rows or []:
        if a.get("provider") == provider:
            return a
    return None


def t20_forget_blocks_reimport():
    """Remove -> a tombstone exists -> import_from_cli() does not undo it."""
    need(aiaccounts, "add_account", "import_from_cli", "list_accounts")
    bad = require_tombstone_api()
    if bad:
        return False, bad
    cli_sandbox()
    reset_vault_and_tombstones()
    try:
        cpath, _ = write_cli_creds(
            claude=claude_cred(expires_at_s=time.time() + 3600),
            codex=codex_cred(account_id="acct-fake-0001"))
        first = aiaccounts.import_from_cli()
        row = first_of(first, "claude")
        if row is None:
            return False, "fixture failed: import produced no claude account"
        forgot = aiaccounts.forget_account(row["id"])
        stones = aiaccounts.list_forgotten()
        on_disk = os.path.exists(tombstone_file())
        again = aiaccounts.import_from_cli()
        rows = aiaccounts.list_accounts()
        # codex is the positive control: it must still be there, which proves
        # the second import really ran and was capable of adding rows.
        ok = (forgot is True
              and len(stones) == 1
              and stones[0].get("provider") == "claude"
              and on_disk
              and first_of(again, "claude") is None
              and first_of(rows, "claude") is None
              and first_of(rows, "codex") is not None)
        return ok, ("forget=%r, tombstones=%d, sibling file on disk=%r, "
                    "claude re-imported=%r, providers left=%r"
                    % (forgot, len(stones), on_disk,
                       first_of(again, "claude") is not None,
                       sorted(a.get("provider") for a in rows)))
    finally:
        reset_vault_and_tombstones()


def t21_tombstone_survives_vault_wipe():
    """The scenario the sibling-file storage exists for.

    An uninstall/reinstall, a corrupted-vault recovery, or a user deleting
    accounts.json to "reset" the widget all destroy the vault document. A
    tombstone kept inside that document would die with it and silently
    un-delete every account the user ever removed -- the same bug, one level
    up. Wiping the vault here is the whole point of the test, not incidental
    setup.
    """
    need(aiaccounts, "import_from_cli", "list_accounts", "load")
    bad = require_tombstone_api()
    if bad:
        return False, bad
    cli_sandbox()
    reset_vault_and_tombstones()
    try:
        cpath, _ = write_cli_creds(
            claude=claude_cred(expires_at_s=time.time() + 3600),
            codex=codex_cred(account_id="acct-fake-0001"))
        row = first_of(aiaccounts.import_from_cli(), "claude")
        if row is None:
            return False, "fixture failed: import produced no claude account"
        if not aiaccounts.forget_account(row["id"]):
            return False, "forget_account() reported failure"

        os.remove(VAULT)
        wiped = (aiaccounts.load().get("accounts") == [])
        survived = os.path.exists(tombstone_file())

        again = aiaccounts.import_from_cli()
        rows = aiaccounts.list_accounts()
        # codex coming back from an empty vault is the positive control: the
        # import genuinely re-populated the wiped vault, so claude's absence
        # is a decision, not an import that did nothing.
        ok = (wiped and survived
              and first_of(again, "claude") is None
              and first_of(rows, "claude") is None
              and first_of(rows, "codex") is not None)
        return ok, ("vault wiped=%r, tombstone survived=%r, "
                    "claude back=%r, codex re-imported=%r"
                    % (wiped, survived,
                       first_of(rows, "claude") is not None,
                       first_of(rows, "codex") is not None))
    finally:
        reset_vault_and_tombstones()


def t22_allow_reimport_undoes_the_tombstone():
    """A mistaken forget is recoverable: after undo, the account comes back."""
    need(aiaccounts, "import_from_cli", "list_accounts")
    bad = require_tombstone_api()
    if bad:
        return False, bad
    cli_sandbox()
    reset_vault_and_tombstones()
    try:
        cpath, _ = write_cli_creds(
            claude=claude_cred(expires_at_s=time.time() + 3600), codex=None)
        row = first_of(aiaccounts.import_from_cli(), "claude")
        if row is None:
            return False, "fixture failed: import produced no claude account"
        if not aiaccounts.forget_account(row["id"]):
            return False, "forget_account() reported failure"
        blocked = first_of(aiaccounts.import_from_cli(), "claude") is None

        cleared = aiaccounts.allow_reimport("claude", cred_path=cpath)
        left = aiaccounts.list_forgotten()
        back = aiaccounts.import_from_cli()
        rows = aiaccounts.list_accounts()
        ok = (blocked and cleared == 1 and left == []
              and first_of(back, "claude") is not None
              and first_of(rows, "claude") is not None)
        return ok, ("blocked before undo=%r, cleared=%r, tombstones left=%d, "
                    "re-imported after undo=%r"
                    % (blocked, cleared, len(left),
                       first_of(back, "claude") is not None))
    finally:
        reset_vault_and_tombstones()


def t23_coerce_preserves_allowlisted_top_level_keys():
    """imported_from_cli must survive save/load; unknown keys must not.

    _coerce() used to return exactly {version, accounts} and drop everything
    else, so core._mark_migrated()'s stamp was eaten on the very next save and
    never actually persisted. The fix is an allowlist, so this asserts both
    halves: the known key sticks, and an arbitrary key still cannot smuggle
    itself into the vault. Two round trips, because the old bug ate the key on
    EVERY save -- one cycle would not prove it stays.
    """
    need(aiaccounts, "load", "save", "add_account")
    reset_vault_and_tombstones()
    try:
        aiaccounts.add_account("claude", "personal", claude_cred())
        doc = aiaccounts.load()
        doc["imported_from_cli"] = True
        doc["definitely_not_allowlisted"] = {"smuggled": True}
        wrote = aiaccounts.save(doc)

        back = aiaccounts.load()
        aiaccounts.save(back)
        back2 = aiaccounts.load()

        ok = (wrote is not False
              and back.get("imported_from_cli") is True
              and back2.get("imported_from_cli") is True
              and "definitely_not_allowlisted" not in back
              and "definitely_not_allowlisted" not in back2
              and len(back2.get("accounts") or []) == 1)
        return ok, ("imported_from_cli after 1/2 round trips = %r/%r, "
                    "unknown key dropped=%r, accounts kept=%d"
                    % (back.get("imported_from_cli"),
                       back2.get("imported_from_cli"),
                       "definitely_not_allowlisted" not in back2,
                       len(back2.get("accounts") or [])))
    finally:
        reset_vault_and_tombstones()


def t24_fingerprint_drift_still_tombstoned():
    """A refreshed Claude credential is still recognised as forgotten.

    cred_fingerprint("claude", ...) is subscriptionType + expiresAt, and
    expiresAt is rewritten on every token issue. A tombstone keyed on that
    alone goes stale the first time the CLI refreshes, quietly un-blocking a
    deliberately removed account -- the exact complaint. This is why matching
    is on cred_path too, and this test is what stops anyone dropping that
    second key as redundant.
    """
    need(aiaccounts, "import_from_cli", "list_accounts", "cred_fingerprint")
    bad = require_tombstone_api()
    if bad:
        return False, bad
    cli_sandbox()
    reset_vault_and_tombstones()
    try:
        before = claude_cred(expires_at_s=time.time() + 3600)
        cpath, _ = write_cli_creds(claude=before, codex=None)
        row = first_of(aiaccounts.import_from_cli(), "claude")
        if row is None:
            return False, "fixture failed: import produced no claude account"
        if not aiaccounts.forget_account(row["id"]):
            return False, "forget_account() reported failure"

        # Simulate the token refresh: same login, same file, new expiresAt.
        after = claude_cred(expires_at_s=time.time() + 99999)
        fp_before = aiaccounts.cred_fingerprint("claude", before)
        fp_after = aiaccounts.cred_fingerprint("claude", after)
        drifted = bool(fp_before) and bool(fp_after) and fp_before != fp_after
        if not drifted:
            return False, ("fixture failed: fingerprint did not drift across a "
                           "simulated refresh, so this test proves nothing")
        write_cli_creds(claude=after, codex=None)

        still = aiaccounts.is_tombstoned("claude", after, cpath)
        again = aiaccounts.import_from_cli()
        rows = aiaccounts.list_accounts()
        ok = (still is True
              and first_of(again, "claude") is None
              and first_of(rows, "claude") is None)
        return ok, ("fingerprint drifted=%r, is_tombstoned after drift=%r, "
                    "resurrected=%r"
                    % (drifted, still, first_of(rows, "claude") is not None))
    finally:
        reset_vault_and_tombstones()


def t25_shared_cred_path_stays_blocked_until_undo():
    """The false-positive direction, asserted honestly.

    Because cred_path is a match key, a genuinely DIFFERENT login that later
    occupies the same credential file (an ordinary CLI account switch, not a
    reinstall) is also blocked. That is a deliberate trade-off, conservative in
    the user's favour: a false "still forgotten" costs one allow_reimport()
    call, while the alternative -- trusting the drifting fingerprint alone --
    fails as a silent resurrection of an account the user deleted.

    This asserts what the code actually does, not what would be nicer. If
    someone later makes the match narrower, this test should be changed
    deliberately, with the resurrection risk reconsidered -- not quietly
    because it started failing.
    """
    need(aiaccounts, "import_from_cli", "list_accounts", "cred_fingerprint")
    bad = require_tombstone_api()
    if bad:
        return False, bad
    cli_sandbox()
    reset_vault_and_tombstones()
    try:
        original = claude_cred(expires_at_s=time.time() + 3600, sub="max")
        cpath, _ = write_cli_creds(claude=original, codex=None)
        row = first_of(aiaccounts.import_from_cli(), "claude")
        if row is None:
            return False, "fixture failed: import produced no claude account"
        if not aiaccounts.forget_account(row["id"]):
            return False, "forget_account() reported failure"

        # A different subscription tier AND a different expiry: by fingerprint
        # this is unambiguously another account. Only the file path is shared.
        other = claude_cred(expires_at_s=time.time() + 123456, sub="pro")
        if (aiaccounts.cred_fingerprint("claude", other)
                == aiaccounts.cred_fingerprint("claude", original)):
            return False, ("fixture failed: the 'different' account shares a "
                           "fingerprint, so path matching is untested")
        write_cli_creds(claude=other, codex=None)

        blocked = aiaccounts.is_tombstoned("claude", other, cpath)
        denied = first_of(aiaccounts.import_from_cli(), "claude") is None

        cleared = aiaccounts.allow_reimport("claude", cred_path=cpath)
        back = first_of(aiaccounts.import_from_cli(), "claude")
        ok = (blocked is True and denied and cleared >= 1 and back is not None
              and aiaccounts.cred_fingerprint("claude", back.get("cred"))
              == aiaccounts.cred_fingerprint("claude", other))
        return ok, ("different account on the same path blocked=%r, "
                    "import denied=%r, cleared by undo=%r, admitted after "
                    "undo=%r" % (blocked, denied, cleared, back is not None))
    finally:
        reset_vault_and_tombstones()


# ===========================================================================
TESTS = [
    ("01 missing vault file yields an empty store", t01_missing_vault_is_empty),
    ("02 corrupt/truncated vault yields an empty store", t02_corrupt_vault_is_empty),
    ("03 add_account returns a generated id and is listed", t03_add_account_gets_id_and_is_listed),
    ("04 update_cred survives a reload from disk", t04_update_cred_survives_reload),
    ("05 remove_account booleans (hit and miss)", t05_remove_account_booleans),
    ("06 rename_account keeps the credential", t06_rename_preserves_cred),
    ("07 several accounts of the same provider coexist", t07_same_provider_multiple_accounts),
    ("08 no .bak/.tmp sidecar next to the vault", t08_no_sidecar_files),
    ("08b vault file permissions are restrictive", t08b_restrictive_permissions),
    ("09 a save that fails partway preserves the vault", t09_failed_save_preserves_vault),
    ("10 twelve providers, exactly claude+codex live", t10_registry_shape),
    ("10b provider() resolves ids, unknown -> None", t10b_provider_lookup),
    ("11 planned provider -> error 'not supported yet'", t11_planned_provider_not_supported),
    ("12 fetch_usage never raises on junk input", t12_fetch_usage_never_raises),
    ("13 expired / offline / refresh-failed are distinct", t13_failure_classification_distinct),
    ("13b bare 400 is not reported as 'login expired'", t13b_bare_400_is_not_login_expired),
    ("14 refresh_cred returns the complete rotated cred", t14_refresh_returns_complete_rotated_cred),
    ("14b rotated refresh token survives the vault", t14b_refresh_rotation_survives_vault_roundtrip),
    ("15 is_unrecoverable: dead vs freshly expired", t15_is_unrecoverable_window),
    ("16 import_from_cli is idempotent", t16_import_from_cli_idempotent),
    ("20 forget_account blocks the next CLI re-import", t20_forget_blocks_reimport),
    ("21 tombstone survives a full vault wipe", t21_tombstone_survives_vault_wipe),
    ("22 allow_reimport lets the account back in", t22_allow_reimport_undoes_the_tombstone),
    ("23 _coerce keeps allowlisted top-level keys", t23_coerce_preserves_allowlisted_top_level_keys),
    ("24 drifted claude fingerprint is still tombstoned", t24_fingerprint_drift_still_tombstoned),
    ("25 shared cred_path stays blocked until undo", t25_shared_cred_path_stays_blocked_until_undo),
    ("17 safety: no network request was attempted", t17_no_network_attempted),
    ("18 safety: no credential read outside the sandbox", t18_no_reads_outside_sandbox),
    ("19 safety: vault path still inside the sandbox", t19_vault_path_still_sandboxed),
]


def main():
    print("net-watch-widget :: multi-account AI feature regression harness")
    print("python %s" % sys.version.split()[0])
    print("sandbox vault dir : <temp>/%s" % os.path.basename(TMPDIR))
    print("")

    # ---- safety gate ------------------------------------------------------
    if aiaccounts is not None and hasattr(aiaccounts, "accounts_file"):
        resolved = aiaccounts.accounts_file()
        if not inside_tmp(resolved):
            print("ABORT: accounts_file() resolved OUTSIDE the temp sandbox.")
            print("       Refusing to touch what may be the real vault.")
            cleanup()
            return 2
        print("safety: vault path confirmed inside the sandbox")
    else:
        print("safety: aiaccounts not importable; vault tests will SKIP")
    print("safety: outbound network disabled (urlopen + socket tripwires)")
    print("safety: provider transport %s"
          % ("stubbed" if HTTP_STUB_INSTALLED else "NOT stubbed -- see skips"))
    for mod, err in sorted(IMPORT_ERRORS.items()):
        print("import: %s failed -- %s" % (mod, err))
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
