#!/usr/bin/env python3
"""Offline regression: a vault save that fails must never read as success.

    python3 tests/account_save_failure_regression.py

Covers D10-F2 / D11-F2 (add/import swallowed save()==False, the login path
announced "account added" and destroyed the sandbox holding the only copy of
the new credential) and D13-2 (a per-account refusal during an error hold said
"polled recently").

Every store is redirected into a throwaway temp tree before anything under test
is imported; urlopen and outbound connects are tripwires. Fake secrets only,
never printed.
"""
import os
import shutil
import socket
import sys
import tempfile
import unittest
import urllib.request
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.realpath(tempfile.mkdtemp(prefix="nw_save_fail_"))
HOME = os.path.join(TMP, "home")
for d in (HOME, os.path.join(TMP, "vault"), os.path.join(TMP, "rl"),
          os.path.join(TMP, "appdata"), os.path.join(TMP, "box")):
    os.makedirs(d, exist_ok=True)
os.environ["AI_ACCOUNTS_FILE"] = os.path.join(TMP, "vault", "accounts.json")
os.environ["AI_RL_FILE"] = os.path.join(TMP, "rl", "rl.json")
for v in ("HOME", "USERPROFILE", "HOMEPATH"):
    os.environ[v] = HOME
os.environ["HOMEDRIVE"] = ""
for v in ("APPDATA", "LOCALAPPDATA"):
    os.environ[v] = os.path.join(TMP, "appdata")
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(TMP, "data")
os.environ["CLAUDE_CRED_PATHS"] = ""
os.environ["CODEX_CRED_PATHS"] = ""
tempfile.tempdir = os.path.join(TMP, "box")

_real_connect = socket.socket.connect


def _no_net(*a, **k):
    raise RuntimeError("network blocked in test")


def _connect(self, addr, *a, **k):
    if isinstance(addr, tuple) and addr and addr[0] in ("127.0.0.1", "::1"):
        return _real_connect(self, addr, *a, **k)
    raise RuntimeError("network blocked in test")


urllib.request.urlopen = _no_net
socket.socket.connect = _connect
sys.path.insert(0, ROOT)

import aiaccounts  # noqa: E402
import aipolicy  # noqa: E402
import sidecar  # noqa: E402

assert aiaccounts.accounts_file().startswith(TMP), "vault not redirected"
CRED = {"tokens": {"access_token": "FAKE-ACCESS", "refresh_token": "FAKE-REFRESH",
                   "account_id": "acct-fake-0001"}}


def fresh_vault():
    for p in (aiaccounts.accounts_file(), aiaccounts.accounts_file() + ".forgotten"):
        if os.path.exists(p):
            os.remove(p)


class VaultContract(unittest.TestCase):
    def setUp(self):
        fresh_vault()

    def test_add_account_raises_when_save_fails(self):
        with patch.object(aiaccounts, "save", return_value=False):
            with self.assertRaises(Exception) as ctx:
                aiaccounts.add_account("codex", "x", dict(CRED))
        self.assertIn("not saved", str(ctx.exception))
        self.assertEqual(aiaccounts.list_accounts(), [])

    def test_add_account_succeeds_when_saved(self):
        acct = aiaccounts.add_account("codex", "x", dict(CRED))
        self.assertEqual([a["id"] for a in aiaccounts.list_accounts()], [acct["id"]])

    def test_import_from_cli_raises_when_save_fails(self):
        path = os.path.join(TMP, "fake-auth.json")
        with patch.object(aiaccounts, "save", return_value=False), \
                patch.object(aiaccounts, "cli_cred_paths",
                             lambda p: [path] if p == "codex" else []), \
                patch.object(aiaccounts, "cli_cred_states", lambda p: {}), \
                patch.object(aiaccounts, "_read_json", lambda p: dict(CRED)):
            with self.assertRaises(Exception) as ctx:
                aiaccounts.import_from_cli()
        self.assertIn("not saved", str(ctx.exception))


class _Box:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="box_")
        self.cred_path = os.path.join(self.dir, "auth.json")


class LoginAdd(unittest.TestCase):
    def run_login(self, save_ok):
        fresh_vault()
        emits, torn = [], []

        def wait_for_cred(sandbox, proc=None, timeout=5):
            return dict(CRED)

        def classify_account(provider, cred, accounts):
            return False

        api = {
            "supported": ("isolated_login_supported", lambda p: True),
            "sandbox": ("create_sandbox", lambda p: _Box()),
            "launch": ("launch_login", lambda p, h: None),
            "wait": ("wait_for_cred", wait_for_cred),
            "teardown": ("destroy_sandbox", lambda h: torn.append(h)),
            "duplicate": ("classify_account", classify_account),
        }
        patches = [
            patch.object(sidecar, "_login_api", lambda: (api, "")),
            patch.object(sidecar, "_login_gate", lambda w: ""),
            patch.object(sidecar.core, "ai_provider_login_error", lambda p: ""),
            patch.object(sidecar, "emit", lambda kind, **f: emits.append(f)),
        ]
        if not save_ok:
            patches.append(patch.object(aiaccounts, "save", return_value=False))
        for p in patches:
            p.start()
        try:
            sidecar.isolated_login("codex", label="t")
        finally:
            for p in reversed(patches):
                p.stop()
        return emits, torn

    def test_failed_save_is_not_reported_added_and_sandbox_kept(self):
        emits, torn = self.run_login(save_ok=False)
        notes = [e.get("note") for e in emits]
        self.assertNotIn("account added", notes)
        self.assertTrue(any("not saved" in (e.get("err") or "") for e in emits), emits)
        self.assertEqual(torn, [], "sandbox destroyed although credential was not saved")

    def test_saved_login_reports_added_and_tears_down(self):
        emits, torn = self.run_login(save_ok=True)
        self.assertIn("account added", [e.get("note") for e in emits])
        self.assertEqual(len(torn), 1)


class RefusalWording(unittest.TestCase):
    def setUp(self):
        aipolicy.reset_for_tests(os.path.join(TMP, "rl", "%s.json" % self.id()))

    def refused(self, key):
        emits = []
        with patch.object(sidecar, "emit", lambda kind, **f: emits.append(f)):
            sidecar._ai_refresh_refused(key, 30)
        return emits[-1]

    def test_error_hold_not_called_polled_recently(self):
        key = aipolicy.key("codex", "a1")
        aipolicy.record_error(key, "codex")
        e = self.refused(key)
        self.assertNotIn("polled recently", e["err"])
        self.assertTrue(e["reason"])

    def test_rate_limit_hold_still_rate_limited(self):
        key = aipolicy.key("codex", "a2")
        aipolicy.record_rate_limited(key, "codex")
        e = self.refused(key)
        self.assertEqual(e["reason"], "rate limited")

    def test_no_hold_says_polled_recently(self):
        e = self.refused(aipolicy.key("codex", "a3"))
        self.assertIn("polled recently", e["err"])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
