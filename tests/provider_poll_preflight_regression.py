#!/usr/bin/env python3
"""Offline AST-isolated core checks. No app import, stores or network."""
import ast
import base64
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import aiproviders as providers

def jwt(exp):
    return "fixture." + base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=") + ".sig"

def load():
    tree = ast.parse((ROOT / "core.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {"ai_account_poll", "_needs_login", "_codex_token"}]
    ns = dict(AI_REFRESH_LEAD=3600, time=time, json=json, base64=base64,
              _dt=__import__("datetime"), _prov=providers,
              NOT_CONFIGURED=(), NOT_CONFIGURED_CODEX="not configured",
              CODEX_CRED_PATHS=[], CODEX_CLIENT="fixture", CODEX_TOKEN="fixture", CODEX_UA="fixture",
              _TOKEN_429="refresh failed (429)", AI_UNSAVED={}, AI_UNSAVED_DEFAULT="not saved",
              _unsaved_lock=threading.Lock(), AI_LATCH={}, _GEO_VENDOR={},
              _AI_FETCH_FLAGS=("offline", "rate_limited", "blocked", "geo_blocked"),
              ai_vault_ready=lambda: True, _provider_is_live=lambda p: True,
              _vault_store_cred=lambda *a: True,
              _acct_row=lambda acct, cred, status, usage, error, needs_login=False:
                  dict(status=status, usage=usage, error=error, needs_login=needs_login))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "core.py", "exec"), ns)
    ns.setdefault("_adopt_cli_cred", lambda acct: acct)  # CLI adoption has its own test
    return ns

class Poll(unittest.TestCase):
    def poll(self, why, exp=None, usage=None):
        ns = load()
        with patch.object(providers, "is_unrecoverable", return_value=False), patch.object(providers, "cred_expiry", return_value=exp), patch.object(providers, "refresh_cred", return_value=(None, why)), patch.object(providers, "fetch_usage", return_value=usage or {"session_pct": 12}) as fetch:
            row = ns["ai_account_poll"]({"id": "fixture", "provider": "copilot", "cred": {}})
        return row, fetch.call_count, ns

    def test_non_expiring_providers(self):
        for pid in ("copilot", "windsurf", "devin", "cline"):
            with self.subTest(pid=pid), patch.object(providers, "is_unrecoverable", return_value=False), patch.object(providers, "fetch_usage", return_value={"session_pct": 12}) as fetch:
                row = load()["ai_account_poll"]({"id": "fixture", "provider": pid, "cred": {}})
                self.assertEqual(row["status"], "ok")
                fetch.assert_called_once()

    def test_unknown_expiry_attempts_usage(self):
        for why in ("too soon (earliest refresh later)", "refresh endpoint not verified", "refresh failed (500)"):
            with self.subTest(why=why):
                row, calls, _ = self.poll(why)
                self.assertEqual((row["status"], calls), ("ok", 1))

    def test_plain_login_expired_actionable(self):
        ns = load()
        ns["_prov"] = None
        self.assertTrue(ns["_needs_login"]("cursor", {}, "login expired"))

    def test_unknown_expiry_safety(self):
        for why, flag in (("offline", "offline"), ("refresh failed (429)", "rate_limited"), ("login expired", "needs_login")):
            with self.subTest(why=why):
                row, calls, _ = self.poll(why)
                self.assertEqual(calls, 0)
                self.assertTrue(row.get(flag), row)

    def test_known_expired_stops(self):
        row, calls, _ = self.poll("refresh failed (500)", exp=1)
        self.assertEqual((row["status"], calls), ("error", 0))

    def test_usage_geo_tripwire(self):
        row, calls, ns = self.poll("token does not expire", usage={"error": "geo blocked", "geo_blocked": True})
        self.assertEqual(calls, 1)
        self.assertTrue(row["geo_blocked"])
        self.assertTrue(ns["AI_LATCH"]["blocked"])

class Legacy(unittest.TestCase):
    def token(self, access, identity, response=(500, None, "provider failure"), earliest=None, shared=True, saved=True):
        ns = load()
        cred = {"tokens": {"access_token": access, "id_token": identity, "refresh_token": "fixture", "account_id": "fixture-account"}}
        if earliest:
            cred["tokens"]["earliest_refresh_at"] = earliest
        calls, writes = [], []
        ns["_pick_cred"] = lambda *a, **k: ("fixture-path", cred, None)
        ns["_write_json_atomic"] = lambda path, doc: writes.append(doc) or saved
        def http(*a, **k):
            calls.append("refresh")
            return response
        ns["_http_json"] = http
        if not shared:
            ns["_prov"] = None
        with patch.object(providers, "_http_json", side_effect=http):
            result = ns["_codex_token"]()
        return result, calls, writes

    def test_access_not_identity(self):
        for shared in (True, False):
            with self.subTest(shared=shared):
                access = jwt(time.time() + 10000)
                result, calls, _ = self.token(access, jwt(1), shared=shared)
                self.assertEqual(result, (access, "fixture-account", None))
                self.assertEqual(calls, [])

    def test_expired_access_refreshes(self):
        result, calls, _ = self.token(jwt(1), jwt(time.time() + 10000), response=(200, {"access_token": "new"}, ""))
        self.assertEqual(result[0], "new")
        self.assertEqual(calls, ["refresh"])

    def test_live_access_survives_early_failure(self):
        for shared in (True, False):
            with self.subTest(shared=shared):
                access = jwt(time.time() + 1800)
                result, calls, _ = self.token(access, jwt(1), shared=shared)
                self.assertEqual(result, (access, "fixture-account", None))
                self.assertEqual(calls, ["refresh"])

    def test_earliest_refresh_guard(self):
        access = jwt(time.time() + 1800)
        result, calls, _ = self.token(access, jwt(1), earliest=time.time() + 900)
        self.assertEqual(result, (access, "fixture-account", None))
        self.assertEqual(calls, [])

    def test_earliest_refresh_saved(self):
        earliest = time.time() + 900
        result, _, writes = self.token(jwt(1), jwt(1), response=(200, {"access_token": "new", "earliest_refresh_at": earliest}, ""))
        self.assertEqual(result[0], "new")
        self.assertEqual(writes[0]["tokens"].get("earliest_refresh_at"), earliest)

    def test_bare_400_not_dead(self):
        result, _, _ = self.token(jwt(1), jwt(1), response=(400, None, "invalid_request"))
        self.assertNotIn("LOGIN_EXPIRED", result[2])

    def test_write_failure_not_success(self):
        result, _, _ = self.token(jwt(1), jwt(1), response=(200, {"access_token": "new"}, ""), saved=False)
        self.assertIsNone(result[0])
        self.assertIn("sav", result[2])

if __name__ == "__main__":
    unittest.main(verbosity=2)
