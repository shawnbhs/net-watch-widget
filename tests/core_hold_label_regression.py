#!/usr/bin/env python3
"""Offline AST-isolated checks: held-row labels follow the real hold kind, and
legacy Codex picks the credential copy with the latest access-token expiry.

No core import (it needs winreg), no credential stores, no network. Policy
state lives in a throwaway temp file; credential copies are synthetic.
"""
import ast
import base64
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix="nw_hold_label_")
os.environ["AI_RL_FILE"] = os.path.join(TMP, "rl.json")
sys.path.insert(0, str(ROOT))
import aipolicy  # noqa: E402
import aiproviders as providers  # noqa: E402


def _no_network(*a, **k):
    raise AssertionError("network attempted in offline test")


providers._http_json = _no_network           # belt and braces: never leave
providers.refresh_cred = lambda *a, **k: (None, "refresh blocked in test")

NAMES = {"ai_poll_legacy", "ai_poll_accounts_due", "_codex_token", "_pick_cred",
         "_read_json", "_newest_cli_cred", "_cli_cred_paths"}


def jwt(exp):
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode()
    return "fixture." + body.rstrip("=") + ".sig"


def load(**extra):
    tree = ast.parse((ROOT / "core.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and (n.name in NAMES or n.name.startswith("_ai_"))
             or isinstance(n, ast.Assign) and any(
                 getattr(t, "id", "").startswith("_AI_HOLD") for t in n.targets)]
    fetched = []
    ns = dict(aipolicy=aipolicy, _prov=providers, time=time, json=json,
              base64=base64, os=os, threading=threading,
              _dt=__import__("datetime"),
              AI_REFRESH_LEAD=3600, AI_ACCT_SPACING=0, AI_CRED_MAX_STALE=16 * 86400,
              AI_WAITING="waiting for next poll", _TOKEN_429="refresh failed (429)",
              NOT_CONFIGURED=(), NOT_CONFIGURED_CODEX="not configured",
              CODEX_CRED_PATHS=[], CLAUDE_CRED_PATHS=[],
              _AI_LAST_ROWS={}, _AI_LAST_GOOD={}, _AI_DEAD_FP={},
              _AI_ACTIVE_KEYS=None, _cred_fingerprint=lambda c: "fp",
              fetch_claude_usage=lambda: fetched.append("claude") or {"err": "x"},
              fetch_gpt_usage=lambda: fetched.append("codex") or {"err": "x"},
              ai_vault_ready=lambda: True, _provider_is_live=lambda p: True,
              ai_account_poll=lambda a, lead=0: fetched.append(a["id"]) or {},
              _acct_row=lambda acct, cred, status, usage=None, error=None,
              needs_login=False: dict(status=status, usage=usage, error=error,
                                      needs_login=needs_login))
    ns.update(extra)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "core.py", "exec"), ns)
    ns.setdefault("_adopt_cli_cred", lambda acct: acct)  # CLI adoption has its own test
    ns["fetched"] = fetched
    return ns


def fresh_policy():
    aipolicy.reset_for_tests(tempfile.mktemp(dir=TMP, suffix=".json"))


class HoldLabel(unittest.TestCase):
    """A restarted widget has no last row, so the placeholder is all it shows."""

    def setUp(self):
        fresh_policy()

    def legacy(self, record, key=None):
        record(key or aipolicy.key("codex", None), "codex")
        aipolicy.record_error(aipolicy.key("claude", None), "claude")
        ns = load()
        out = ns["ai_poll_legacy"]()[1]
        self.assertEqual(ns["fetched"], [])
        return out

    def vault(self, record, other=False):
        accts = [{"id": "a1", "provider": "codex", "label": "A"},
                 {"id": "a2", "provider": "codex", "label": "B"}]
        record(aipolicy.key("codex", "a2" if other else "a1"), "codex")
        if not other:
            aipolicy.record_error(aipolicy.key("codex", "a2"), "codex")
        ns = load(_vault=SimpleNamespace(list_accounts=lambda: accts))
        rows, requested = ns["ai_poll_accounts_due"]()
        self.assertFalse(requested)
        return rows[0]

    def test_legacy_error_hold_not_rate_limited(self):
        out = self.legacy(aipolicy.record_error)
        self.assertNotIn("rate limited", out["err"])
        self.assertIn("error", out["err"])
        self.assertFalse(out.get("rate_limited"))
        self.assertGreater(out["held"], 0)

    def test_legacy_rate_hold_says_rate_limited(self):
        out = self.legacy(aipolicy.record_rate_limited)
        self.assertEqual(out["err"], "rate limited")
        self.assertTrue(out.get("rate_limited"))

    def test_legacy_block_hold_says_blocked(self):
        out = self.legacy(aipolicy.record_blocked)
        self.assertIn("blocked", out["err"])
        self.assertFalse(out.get("rate_limited"))

    def test_legacy_login_dead_keeps_sign_in(self):
        out = self.legacy(aipolicy.record_login_dead)
        self.assertEqual(out["err"], "LOGIN_EXPIRED:codex")

    def test_vault_error_hold_not_rate_limited(self):
        row = self.vault(aipolicy.record_error)
        self.assertNotIn("rate limited", row["error"])
        self.assertIn("error", row["error"])

    def test_vault_rate_hold_says_rate_limited(self):
        self.assertEqual(self.vault(aipolicy.record_rate_limited)["error"],
                         "rate limited")

    def test_vault_block_hold_says_blocked(self):
        self.assertIn("blocked", self.vault(aipolicy.record_blocked)["error"])

    def test_vault_provider_wide_block_from_other_key(self):
        row = self.vault(aipolicy.record_blocked, other=True)
        self.assertIn("blocked", row["error"])

    def test_vault_provider_wide_rate_from_other_key(self):
        row = self.vault(aipolicy.record_rate_limited, other=True)
        self.assertEqual(row["error"], "rate limited")

    def test_vault_login_dead_keeps_sign_in(self):
        row = self.vault(aipolicy.record_login_dead)
        self.assertEqual(row["error"], "LOGIN_EXPIRED:codex")
        self.assertTrue(row["needs_login"])


class CodexCopy(unittest.TestCase):
    """A larger stale copy must not beat a smaller live one."""

    def write(self, name, doc):
        path = os.path.join(TMP, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return path

    def test_live_copy_beats_bigger_stale_copy(self):
        live = jwt(time.time() + 50000)
        stale = {"tokens": {"access_token": jwt(time.time() - 100),
                            "id_token": jwt(time.time() + 99999) + "x" * 4000,
                            "refresh_token": "fixture", "account_id": "stale"}}
        fresh = {"tokens": {"access_token": live, "refresh_token": "fixture",
                            "account_id": "live"}}
        a, b = self.write("stale.json", stale), self.write("live.json", fresh)
        for paths in ([a, b], [b, a]):
            with self.subTest(order=[os.path.basename(p) for p in paths]):
                ns = load(CODEX_CRED_PATHS=paths,
                          _write_json_atomic=lambda *x: self.fail("wrote"),
                          _http_json=lambda *x, **k: self.fail("network"))
                self.assertEqual(ns["_codex_token"](), (live, "live", None))

    def test_no_copy_is_not_configured(self):
        ns = load(CODEX_CRED_PATHS=[os.path.join(TMP, "missing.json")])
        self.assertEqual(ns["_codex_token"](), (None, None, "not configured"))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
