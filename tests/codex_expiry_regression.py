#!/usr/bin/env python3
"""Offline regression: identity tokens do not authorize usage requests."""
import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aiproviders


def jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode()
    return "synthetic." + payload.rstrip("=") + ".signature"


class CodexExpiry(unittest.TestCase):
    def test_expired_identity_does_not_expire_live_access(self):
        cred = {"tokens": {"id_token": jwt(100), "access_token": jwt(200)}}
        self.assertEqual(aiproviders.cred_expiry("codex", cred), 200)

    def test_live_identity_does_not_extend_expired_access(self):
        cred = {"tokens": {"id_token": jwt(200), "access_token": jwt(100)}}
        self.assertEqual(aiproviders.cred_expiry("codex", cred), 100)

    def test_identity_alone_does_not_prove_access_validity(self):
        cred = {"tokens": {"id_token": jwt(200), "access_token": "opaque"}}
        self.assertIsNone(aiproviders.cred_expiry("codex", cred))


if __name__ == "__main__":
    unittest.main()
