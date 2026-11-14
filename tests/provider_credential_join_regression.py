#!/usr/bin/env python3
"""Offline reader-shape joins and known-live access regression (stdlib only)."""
import base64
import copy
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch
import urllib.request

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aiproviders as providers

NOW = 2000000000
FAKE = "FAKE-ACCESS-DO-NOT-USE"


def jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode())
    return "synthetic." + payload.decode().rstrip("=") + ".signature"


def credential(pid, exp, access=True, refresh=False):
    if pid == "claude":
        inner = {"expiresAt": exp * 1000}
        if access:
            inner["accessToken"] = FAKE
        if refresh:
            inner["refreshToken"] = "FAKE-REFRESH-DO-NOT-USE"
        return {"claudeAiOauth": inner}
    inner = {"id_token": jwt(NOW - 100)}
    if access:
        inner["access_token"] = jwt(exp)
    if refresh:
        inner["refresh_token"] = "FAKE-REFRESH-DO-NOT-USE"
    return {"tokens": inner}


class CredentialJoins(unittest.TestCase):
    def setUp(self):
        for target in ("urllib.request.urlopen", "urllib.request.OpenerDirector.open",
                       "socket.create_connection", "socket.socket.connect"):
            blocker = patch(target, side_effect=AssertionError("network forbidden"))
            blocker.start()
            self.addCleanup(blocker.stop)
        clock = patch.object(providers.time, "time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def test_network_blocker_active(self):
        with self.assertRaisesRegex(AssertionError, "network forbidden"):
            socket.create_connection(("127.0.0.1", 1))

    def assert_usage(self, pid, shapes, response, token, prefix):
        before = copy.deepcopy(shapes)
        for cred in shapes:
            with self.subTest(shape=list(cred)):
                with patch.object(providers, "_http_json", return_value=(200, response, "")) as http:
                    out = providers.fetch_usage(pid, cred)
                    self.assertNotIn("error", out)
                    self.assertEqual(out["session_pct" if pid == "kimi" else "month_pct"], 25.0)
                    http.assert_called_once()
                    self.assertEqual(http.call_args.args[1]["Authorization"], prefix + token)
        self.assertEqual(shapes, before, "adapter mutated credential")

    def test_copilot_import_wrapper_and_legacy_shapes(self):
        inner = {"oauth_token": FAKE, "host": "synthetic"}
        self.assert_usage("copilot", [inner, {"copilotAuth": inner},
                          {"copilot": inner}, {"github": inner}],
                          {"quota_snapshots": {"premium_interactions": {
                              "entitlement": 100, "remaining": 75}}}, FAKE, "token ")

    def test_kimi_import_wrapper_and_legacy_shapes(self):
        inner = {"access_token": FAKE, "expires_at": NOW + 7200}
        shapes = [inner, {"kimi": inner}, {"kimiAuth": inner},
                  {"kimiAuth": {"credentials": inner, "device_id": "FAKE-DEVICE"}}]
        self.assert_usage("kimi", shapes, {"usages": {"five_hour": {"usedRatio": 0.25}}},
                          FAKE, "Bearer ")
        for cred in shapes:
            with self.subTest(shape=list(cred)):
                self.assertEqual(providers.cred_expiry("kimi", cred), NOW + 7200)

    def test_kimi_nested_jwt_expiry(self):
        cred = {"kimiAuth": {"credentials": {"access_token": jwt(NOW + 7200)}}}
        self.assertEqual(providers.cred_expiry("kimi", cred), NOW + 7200)

    def test_malformed_wrappers_do_not_send(self):
        for pid, creds in (("copilot", [None, {"copilotAuth": None}, {"copilotAuth": {}}]),
                           ("kimi", [None, {"kimiAuth": {"credentials": None}},
                                     {"kimiAuth": {"credentials": {}}}])):
            for cred in creds:
                with self.subTest(pid=pid, cred=cred):
                    with patch.object(providers, "_http_json") as http:
                        self.assertEqual(providers.fetch_usage(pid, cred)["error"], "no credential")
                        http.assert_not_called()

    def test_future_access_without_refresh_not_dead(self):
        for pid in ("claude", "codex"):
            with self.subTest(pid=pid):
                self.assertFalse(providers.is_unrecoverable(pid, credential(pid, NOW + 7200)))

    def test_future_access_near_expiry_without_refresh_not_dead(self):
        for pid in ("claude", "codex"):
            with self.subTest(pid=pid):
                self.assertFalse(providers.is_unrecoverable(pid, credential(pid, NOW + 30)))

    def test_expired_access_without_refresh_still_dead(self):
        for pid in ("claude", "codex"):
            for exp in (NOW, NOW - 1, NOW - providers.CRED_MAX_STALE - 1):
                with self.subTest(pid=pid, exp=exp):
                    self.assertTrue(providers.is_unrecoverable(pid, credential(pid, exp)))

    def test_future_metadata_without_access_does_not_revive_shell(self):
        for pid in ("claude", "codex"):
            with self.subTest(pid=pid):
                self.assertTrue(providers.is_unrecoverable(pid, credential(pid, NOW + 7200, access=False)))

    def test_refreshable_and_gutted_semantics_unchanged(self):
        for pid in ("claude", "codex"):
            with self.subTest(pid=pid):
                self.assertFalse(providers.is_unrecoverable(pid, credential(pid, NOW - 60, refresh=True)))
                self.assertTrue(providers.is_unrecoverable(pid, credential(pid, 0)))
                self.assertTrue(providers.is_unrecoverable(pid, credential(
                    pid, NOW - providers.CRED_MAX_STALE - 1, refresh=True)))
                self.assertFalse(providers.is_unrecoverable(pid, {"unrecognized": True}))

    def test_refresh_refusal_contract_unchanged(self):
        with patch.object(providers, "_http_json") as http:
            for pid in ("claude", "codex"):
                self.assertEqual(providers.refresh_cred(pid, credential(pid, NOW + 7200)),
                                 (None, "login expired"))
            self.assertEqual(providers.refresh_cred("copilot", {"copilotAuth": {"oauth_token": FAKE}}),
                             (None, "token does not expire"))
            self.assertEqual(providers.refresh_cred("kimi", {"kimiAuth": {"credentials": {"access_token": FAKE}}}),
                             (None, "refresh endpoint not verified"))
            http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
