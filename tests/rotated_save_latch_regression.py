#!/usr/bin/env python3
"""Regression: a rotated credential whose save failed must not be thrown away.

    python3 tests/rotated_save_latch_regression.py

The bug
-------
A refresh rotates the refresh token: the old one dies the moment the new one
is issued. When the new credential could not be written down it was dropped,
and the next poll re-sent the dead grant -- invalid_grant, "login expired",
a real login destroyed. Pinned here, for the vault path and both legacy CLI
files: the unsaved credential is kept, its save is retried first, and nothing
refreshes again while it is pending.

No network (urlopen and connects are tripwires; refresh, usage and the vault
are fakes). Every file lives in a throwaway directory. Fake secrets only,
never printed.
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
SANDBOX = tempfile.mkdtemp(prefix="nw-latch-")
os.environ["HOME"] = os.environ["USERPROFILE"] = SANDBOX
os.environ["APPDATA"] = os.environ["LOCALAPPDATA"] = os.path.join(SANDBOX, "ad")
os.environ["AI_RL_FILE"] = os.path.join(SANDBOX, "ai_policy.json")
os.environ["AI_ACCOUNTS_FILE"] = os.path.join(SANDBOX, "accounts.json")
os.environ["CLAUDE_CRED_PATHS"] = os.environ["CODEX_CRED_PATHS"] = ""


def _no_net(*a, **k):
    raise RuntimeError("network blocked in test")


urllib.request.urlopen = _no_net
socket.socket.connect = lambda self, *a, **k: _no_net()
sys.path.insert(0, REPO_ROOT)
if os.name != "nt":  # core.py is a Windows module; inert stand-ins elsewhere
    import types
    for _m in ("winreg", "netfast"):
        try:
            __import__(_m)
        except Exception:
            sys.modules[_m] = types.ModuleType(_m)

import core  # noqa: E402

REAL_WRITE = core._write_json_atomic


class FakeProv:
    def __init__(self):
        self.refreshes = 0
        self.fetched = []

    def provider(self, pid):
        return {"status": "live"}

    def is_unrecoverable(self, pid, cred):
        return False

    def cred_expiry(self, pid, cred):
        return (cred or {}).get("exp") if pid != "codex" else 1

    def refresh_cred(self, pid, cred):
        self.refreshes += 1
        if pid == "codex":
            return {"tokens": {"access_token": jwt(7200, "new"),
                               "refresh_token": "FAKE-R-new",
                               "account_id": "acct-1"}}, None
        return {"tok": "FAKE-ROTATED", "exp": time.time() + 7200}, None

    def fetch_usage(self, pid, cred):
        self.fetched.append(cred.get("tok"))
        return {}


class FakeVault:
    def __init__(self, accts):
        self.accts = accts
        self.ok = False
        self.saved = []

    def list_accounts(self):
        return self.accts

    def update_cred(self, aid, cred, **kw):
        if self.ok:
            self.saved.append(cred.get("tok"))
        return self.ok


# Rotated fakes must outlive AI_REFRESH_LEAD (3600 s) or they read as due.
def jwt(secs, tag):
    seg = base64.urlsafe_b64encode(
        b' {"exp": %d}' % int(time.time() + secs)).decode().rstrip("=")
    return "hdr.%s.FAKE-SIG-%s" % (seg, tag)


def rig(vault=None, prov=None):
    core._vault, core._prov = vault, prov
    core.AI_UNSAVED.clear()
    getattr(core, "AI_PENDING", {}).clear()
    getattr(core, "_LEGACY_PENDING", {}).clear()


def acct():
    return {"id": "a1", "provider": "claude", "label": "c",
            "cred": {"tok": "FAKE-OLD", "exp": time.time() - 60}}


# (a) vault: the next poll saves the pending credential instead of refreshing
def t_poll_retries_pending_save():
    a, prov = acct(), FakeProv()
    v = FakeVault([a])
    rig(v, prov)
    first = core.ai_account_poll(a)
    v.ok = True
    second = core.ai_account_poll(a)
    ok = (first["status"] == "error" and second["status"] == "ok"
          and prov.refreshes == 1 and v.saved == ["FAKE-ROTATED"]
          and prov.fetched == ["FAKE-ROTATED"])
    return ok, "first %s, second %s %r, refreshes %d, saved %r, fetched %r" % (
        first["status"], second["status"], second["error"], prov.refreshes,
        v.saved, prov.fetched)


# (b) vault keepalive: a pending credential is saved, never refreshed past
def t_keepalive_never_refreshes_pending():
    a, prov = acct(), FakeProv()
    v = FakeVault([a])
    rig(v, prov)
    core._vault_store_cred("a1", {"tok": "FAKE-ROTATED",
                                  "exp": time.time() + 7200})
    core._vault_keepalive()          # save still failing
    still = prov.refreshes
    v.ok = True
    core._vault_keepalive()          # save now works
    ok = still == 0 and prov.refreshes == 0 and v.saved == ["FAKE-ROTATED"]
    return ok, "refreshes %d/%d, saved %r" % (still, prov.refreshes, v.saved)


# (c) legacy Codex file: write retried, no second refresh
def t_codex_write_retried_not_refreshed():
    p = os.path.join(SANDBOX, "auth.json")
    REAL_WRITE(p, {"tokens": {"access_token": jwt(-60, "old"),
                              "refresh_token": "FAKE-R-old",
                              "account_id": "acct-1"}})
    prov, state = FakeProv(), {"ok": False, "calls": 0}

    def write(path, doc):
        state["calls"] += 1
        return REAL_WRITE(path, doc) if state["ok"] else False

    rig(None, prov)
    core.CODEX_CRED_PATHS = [p]
    core._write_json_atomic = write
    try:
        r1 = core._codex_token()
        r2 = core._codex_token()            # write still failing
        state["ok"] = True
        r3 = core._codex_token()            # write works now
        r4 = core._codex_token()            # file holds the new token
    finally:
        core._write_json_atomic = REAL_WRITE
    new_tag = "FAKE-SIG-new"
    ok = (r1[0] is None and prov.refreshes == 1
          and (r2[0] or "").endswith(new_tag)
          and (r3[0] or "").endswith(new_tag)
          and (r4[0] or "").endswith(new_tag)
          and state["calls"] == 3)
    return ok, "refreshes %d, writes %d, tokens new? %s" % (
        prov.refreshes, state["calls"],
        [bool(r[0] and r[0].endswith(new_tag)) for r in (r1, r2, r3, r4)])


# (d) legacy Claude file: failed write still answers, next call retries it
def t_claude_write_retried_not_refreshed():
    p = os.path.join(SANDBOX, "cred.json")
    REAL_WRITE(p, {"claudeAiOauth": {
        "accessToken": "FAKE-A-old", "refreshToken": "FAKE-R-old",
        "expiresAt": int((time.time() - 60) * 1000),
        "scopes": ["user:profile"]}})
    state = {"ok": False, "writes": 0, "http": 0}

    def write(path, doc):
        state["writes"] += 1
        return REAL_WRITE(path, doc) if state["ok"] else False

    def http(url, headers, data=None, timeout=None):
        state["http"] += 1
        return 200, {"access_token": "FAKE-A-new",
                     "refresh_token": "FAKE-R-new", "expires_in": 7200}, ""

    rig(None, None)
    core.CLAUDE_CRED_PATHS = [p]
    core._write_json_atomic, real_http = write, core._http_json
    core._http_json = http
    try:
        r1 = core._claude_token()
        r2 = core._claude_token()           # write still failing
        state["ok"] = True
        r3 = core._claude_token()           # write works now
        r4 = core._claude_token()
    finally:
        core._write_json_atomic, core._http_json = REAL_WRITE, real_http
    toks = [r[0] for r in (r1, r2, r3, r4)]
    ok = toks == ["FAKE-A-new"] * 4 and state["http"] == 1 \
        and state["writes"] == 3
    return ok, "http %d, writes %d, new tokens %s" % (
        state["http"], state["writes"], [t == "FAKE-A-new" for t in toks])


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("t_")]


def main():
    failed = 0
    for fn in TESTS:
        try:
            ok, why = fn()
        except Exception:
            ok, why = False, traceback.format_exc()
        failed += not ok
        print("%s  %s  %s" % ("PASS" if ok else "FAIL", fn.__name__,
                              "" if ok else why))
    shutil.rmtree(SANDBOX, ignore_errors=True)
    print("\n%d/%d passed" % (len(TESTS) - failed, len(TESTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
