#!/usr/bin/env python3
"""Regression harness: an imported account stays in step with its CLI file.

Runs with a bare interpreter: standard library only, no pytest.

    python tests/ai_cli_adopt_regression.py

The bug
-------
An account imported from ~/.claude/.credentials.json and the Claude CLI held
the same login, and both renewed it. A renewal rotates the refresh token, so
the CLI's renewals killed the vault's copy within a day, the vault never read
the file again, and the card said "login expired" for weeks over a CLI login
that was fine.

What is pinned here
-------------------
  * core._adopt_cli_cred takes the file's login when it is newer, records the
    account's identity, and leaves the vault alone when the file is older,
    gutted, or belongs to a different account.
  * core._cli_write_back hands a renewal the widget made back to the file,
    but only while the file still holds the refresh token that renewal spent,
    and only the login subtree.
  * core.ai_account_poll uses the adopted login instead of reporting the
    stale copy as expired.

The network is never touched: refresh and usage are stubbed, and every file
lives in a throwaway directory that HOME and USERPROFILE also point at.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import traceback

REPO_ROOT = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))
SANDBOX = tempfile.mkdtemp(prefix="nw-adopt-")
os.environ["HOME"] = os.environ["USERPROFILE"] = SANDBOX
os.environ["APPDATA"] = os.path.join(SANDBOX, "appdata")
os.environ["AI_RL_FILE"] = os.path.join(SANDBOX, "ai_policy.json")
sys.path.insert(0, REPO_ROOT)

import aiaccounts  # noqa: E402
import core  # noqa: E402

FAKE_ACCESS = "FAKE-ACCESS-TOKEN-DO-NOT-USE"
FAKE_REFRESH = "FAKE-REFRESH-TOKEN-DO-NOT-USE"


def claude_cred(hours, tag):
    return {"claudeAiOauth": {
        "accessToken": "%s-%s" % (FAKE_ACCESS, tag),
        "refreshToken": "%s-%s" % (FAKE_REFRESH, tag),
        "expiresAt": int((time.time() + hours * 3600) * 1000),
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max"}}


class FakeVault:
    """In-memory vault; identity helpers are the real module's."""

    cred_fingerprint = staticmethod(aiaccounts.cred_fingerprint)
    is_durable_fingerprint = staticmethod(aiaccounts.is_durable_fingerprint)
    cred_fingerprint_candidates = staticmethod(
        aiaccounts.cred_fingerprint_candidates)

    def __init__(self, accounts):
        self.accounts = accounts
        self.writes = 0

    def list_accounts(self):
        return self.accounts

    def update_cred(self, aid, cred, fps=None):
        for a in self.accounts:
            if a["id"] == aid:
                a["cred"] = cred
                if fps:
                    a["cred_fp"], a["fps"] = fps[0], list(fps)
                self.writes += 1
                return True
        return False


_n = [0]


def home(account_uuid="uuid-one"):
    """A fresh CLI home: returns the credential path inside it."""
    _n[0] += 1
    h = os.path.join(SANDBOX, "home%d" % _n[0])
    os.makedirs(os.path.join(h, ".claude"))
    if account_uuid:
        with open(os.path.join(h, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"accountUuid": account_uuid}}, f)
    return os.path.join(h, ".claude", ".credentials.json")


def write(path, doc):
    with open(path, "w") as f:
        json.dump(doc, f)


def read(path):
    with open(path) as f:
        return json.load(f)


def acct(aid, cred, path, **extra):
    a = {"id": aid, "provider": "claude", "label": "claude",
         "source": "import", "cred": cred, "cred_path": path}
    a.update(extra)
    return a


class Rig:
    def __init__(self, accounts):
        self.vault = FakeVault(accounts)

    def __enter__(self):
        self.saved = core._vault
        core._vault = self.vault
        return self.vault

    def __exit__(self, *exc):
        core._vault = self.saved


# ── adoption ─────────────────────────────────────────────────────────────────
def t_adopts_newer_cli_login():
    p = home()
    write(p, claude_cred(5, "cli"))
    a = acct("a1", claude_cred(-800, "vault"), p)
    with Rig([a]) as v:
        got = core._adopt_cli_cred(a)
    ok = (got["cred"]["claudeAiOauth"]["refreshToken"].endswith("-cli")
          and v.writes == 1 and a.get("cred_fp") == "claude:acct:uuid-one")
    return ok, "writes %d, stamp %r" % (v.writes, a.get("cred_fp"))


def t_keeps_vault_when_cli_older():
    p = home()
    write(p, claude_cred(1, "cli"))
    a = acct("a1", claude_cred(5, "vault"), p)
    with Rig([a]) as v:
        got = core._adopt_cli_cred(a)
    return got is a and v.writes == 0, "writes %d (want 0)" % v.writes


def t_ignores_logged_out_cli_file():
    p = home()
    write(p, {"claudeAiOauth": {"accessToken": "", "expiresAt": 0}})
    a = acct("a1", claude_cred(-800, "vault"), p)
    with Rig([a]) as v:
        core._adopt_cli_cred(a)
    return v.writes == 0, "writes %d (want 0)" % v.writes


def t_refuses_other_account_by_stamp():
    p = home("uuid-two")
    write(p, claude_cred(5, "cli"))
    a = acct("a1", claude_cred(-800, "vault"), p,
             cred_fp="claude:acct:uuid-one", fps=["claude:acct:uuid-one"])
    with Rig([a]) as v:
        core._adopt_cli_cred(a)
    return v.writes == 0, "writes %d (want 0)" % v.writes


def t_refuses_file_stamped_on_other_row():
    p = home("uuid-two")
    write(p, claude_cred(5, "cli"))
    a = acct("a1", claude_cred(-800, "vault"), p)
    b = acct("a2", claude_cred(-800, "other"), p + ".elsewhere",
             cred_fp="claude:acct:uuid-two", fps=["claude:acct:uuid-two"])
    with Rig([a, b]) as v:
        core._adopt_cli_cred(a)
    return v.writes == 0, "writes %d (want 0)" % v.writes


def t_unstamped_rows_sharing_file_left_alone():
    p = home(None)
    write(p, claude_cred(5, "cli"))
    a = acct("a1", claude_cred(-800, "one"), p)
    b = acct("a2", claude_cred(-800, "two"), p)
    with Rig([a, b]) as v:
        core._adopt_cli_cred(a)
    return v.writes == 0, "writes %d (want 0)" % v.writes


# ── write-back ───────────────────────────────────────────────────────────────
def t_write_back_replaces_login_only():
    p = home()
    old = claude_cred(0.5, "old")
    doc = dict(old, mcpOAuth={"keep": "me"})
    write(p, doc)
    new = claude_cred(8, "new")
    wrote = core._cli_write_back(acct("a1", old, p), old, new)
    got = read(p)
    ok = (wrote and got["claudeAiOauth"]["refreshToken"].endswith("-new")
          and got.get("mcpOAuth") == {"keep": "me"})
    return ok, "wrote %r, keys %r" % (wrote, sorted(got))


def t_write_back_skips_moved_on_file():
    p = home()
    old = claude_cred(0.5, "old")
    write(p, claude_cred(8, "cli-renewed"))
    wrote = core._cli_write_back(acct("a1", old, p), old, claude_cred(8, "new"))
    still = read(p)["claudeAiOauth"]["refreshToken"].endswith("-cli-renewed")
    return not wrote and still, "wrote %r, file kept %r" % (wrote, still)


# ── the poll ─────────────────────────────────────────────────────────────────
def t_poll_reads_usage_with_adopted_login():
    p = home()
    write(p, claude_cred(5, "cli"))
    a = acct("a1", claude_cred(-800, "vault"), p)
    seen = []
    orig = core._prov.fetch_usage, core._prov.refresh_cred
    core._prov.fetch_usage = lambda pid, cred: (
        seen.append(cred["claudeAiOauth"]["refreshToken"]) or
        {"provider": "claude", "session_pct": 12})
    core._prov.refresh_cred = lambda pid, cred: (None, "must not refresh")
    try:
        with Rig([a]):
            row = core.ai_account_poll(a)
    finally:
        core._prov.fetch_usage, core._prov.refresh_cred = orig
    ok = row.get("status") == "ok" and seen and seen[0].endswith("-cli")
    return ok, "status %r error %r" % (row.get("status"), row.get("error"))


def t_dead_hold_lifted_from_cli_file():
    """A held account is never polled, so the hold check must adopt too."""
    import aipolicy
    p = home()
    write(p, claude_cred(5, "cli"))
    a = acct("a1", claude_cred(-800, "vault"), p)
    key = aipolicy.key("claude", "a1")
    aipolicy.record_login_dead(key, "claude")
    try:
        with Rig([a]) as v:
            core.ai_lift_relogged()
        left = aipolicy.hold_left(key)
    finally:
        aipolicy.clear(key)
    return left == 0 and v.writes == 1, \
        "hold left %r, writes %d (want 0, 1)" % (left, v.writes)


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
