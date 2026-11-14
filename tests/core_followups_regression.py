#!/usr/bin/env python3
"""Offline AST-isolated checks for R02 #1/#3 and R03 F1. No app import or network."""
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
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name in {"ai_account_poll", "_needs_login", "_codex_token"}]
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


class SharedCodexPath(unittest.TestCase):
    def token(self, refresh_result):
        ns = load()
        cred = {"tokens": {"access_token": jwt(1), "refresh_token": "fixture", "account_id": "acct"}}
        writes = []
        ns["_pick_cred"] = lambda *a, **k: ("fixture-path", cred, None)
        ns["_write_json_atomic"] = lambda path, doc: writes.append(doc) or True
        ns["_http_json"] = lambda *a, **k: self.fail("legacy inline path used")
        with patch.object(providers, "refresh_cred", return_value=refresh_result):
            return ns["_codex_token"](), writes

    def test_login_expired_keeps_ui_contract(self):
        result, _ = self.token((None, "login expired"))
        self.assertEqual(result, (None, None, "LOGIN_EXPIRED:codex"))

    def test_offline_passes_through(self):
        result, _ = self.token((None, "offline"))
        self.assertEqual(result[2], "offline")

    def test_refresh_stamps_last_refresh(self):
        new = {"tokens": {"access_token": "new", "refresh_token": "r2", "account_id": "acct"}}
        before = time.time() - 1
        result, writes = self.token((new, None))
        self.assertEqual(result[0], "new")
        stamp = writes[0].get("last_refresh")
        self.assertIsNotNone(stamp)
        ts = __import__("datetime").datetime.fromisoformat(stamp).timestamp()
        self.assertGreaterEqual(ts, before)


class UnknownExpiry451(unittest.TestCase):
    def test_451_holds_before_usage(self):
        ns = load()
        with patch.object(providers, "is_unrecoverable", return_value=False), \
             patch.object(providers, "cred_expiry", return_value=None), \
             patch.object(providers, "refresh_cred", return_value=(None, "refresh failed (451)")), \
             patch.object(providers, "fetch_usage", return_value={"session_pct": 12}) as fetch:
            row = ns["ai_account_poll"]({"id": "fixture", "provider": "copilot", "cred": {}})
        self.assertEqual((row["status"], fetch.call_count), ("error", 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
