"""Read-only discovery of vendor logins that already exist on this machine.

The widget can only show a quota for an account whose credential it can find.
aiaccounts.import_from_cli() knows exactly two vendors, Claude and Codex, and
both of them are easy: a plain JSON file in a dot-directory under the user's
home. The other vendors are not that kind. Cursor and Windsurf hide their
token inside the editor's SQLite state store, Devin ships a TOML file, Kimi
splits its login across two files, GitHub Copilot has migrated from a readable
JSON file to an encrypted database, and Cline keeps nothing on disk we are
allowed to read at all. Without a layer that knows all of that, the user would
have to hunt down and paste a token by hand for every vendor, which is exactly
the chore the widget exists to remove.

This module is that layer and nothing more. It answers three questions --
where would this vendor's credential be, is it there, and can we read it --
and it answers the third one with a specific reason when the answer is no.

What this module will never do, and why each rule is here:

  * It never writes, moves, renames or deletes anything. Every path below
    belongs to another application that the user depends on. A bug that
    truncated Cursor's state.vscdb would log the user out of their editor,
    which is far worse than this feature simply not working. Reading is the
    only operation in this file.
  * Every SQLite database is opened through the read-only URI form
    (file:...?mode=ro) with PRAGMA query_only on top, and the connection is
    closed in a finally. Those databases belong to editors that may be running
    right now; taking a writer lock on one would stall the user's editor.
  * No token value is ever printed, logged, put in a reason string or placed
    in an exception message. Reasons carry paths, key names, sizes and
    timestamps -- things that are safe to show a user and safe to paste into
    a bug report on a public repository.
  * Nothing here makes a network request, and nothing here runs a subprocess
    unless a caller explicitly asks for it (see copilot_token_via_gh).
  * Every public function is total. It returns a result for any input,
    including an unknown provider slug, and never raises. A discovery layer
    that throws would take down the first-run import for every vendor because
    one vendor's file was odd.

Standard library only, matching aiproviders.py: the sidecar's one third-party
dependency is psutil and it stays that way.

── Confidence in the paths encoded below ────────────────────────────────────

A future reader must be able to tell a documented path from an inferred one,
so each entry says which it is. "documented" means the vendor publishes it,
"observed" means it was read off real installations and is widely corroborated
but not in vendor documentation, "inferred" means it follows a pattern the
vendor uses elsewhere and has NOT been confirmed -- treat an inferred path as
a candidate to probe, never as a fact to report.

  cursor      observed   %APPDATA%\\Cursor\\User\\globalStorage\\state.vscdb,
                         SQLite table ItemTable, key cursorAuth/accessToken.
                         This is the standard VS Code global-storage layout,
                         which Cursor inherits as a VS Code fork, plus a
                         vendor-specific key name.
  copilot     observed   %LOCALAPPDATA%\\github-copilot\\apps.json (older
                         installs: hosts.json). Newer installs instead have
                         auth.db, table oauth_tokens, column token_ciphertext,
                         which is encrypted and deliberately left alone.
  windsurf    observed   %APPDATA%\\Windsurf\\User\\globalStorage\\state.vscdb,
                         ItemTable keys windsurfAuthStatus,
                         windsurf.settings.cachedPlanInfo and codeium.windsurf.
                         Same VS Code lineage as Cursor.
  devin       documented %APPDATA%\\devin\\credentials.toml with keys
                         windsurf_api_key and api_server_url. The only vendor
                         here with an officially published Windows path.
  devin       inferred   %APPDATA%\\Devin\\User\\globalStorage\\state.vscdb,
                         ItemTable key windsurfAuthStatus -- the desktop app's
                         own state store, a fallback pattern borrowed from
                         Windsurf. Unconfirmed.
  kimi        observed   ~/.kimi-code/credentials/kimi-code.json plus
                         ~/.kimi-code/device_id, the latter being required as
                         a device header on the usage request.
  cline       none       Cline stores its token in VS Code's ENCRYPTED secret
                         storage. There is no file to read, and this module
                         refuses to invent one. The only honest route is an
                         API key the user generates and exports himself.
  claude      documented ~/.claude/.credentials.json, delegated to aiaccounts.
  codex       documented ~/.codex/auth.json, delegated to aiaccounts.
  glm, replit, antigravity, railway
              unknown    No credential location has been established. They
                         are listed so a scan reports them honestly rather
                         than omitting them and implying they were checked.
"""

import base64
import datetime as _dt
import json
import os
import time

# SQLite is in the standard library on every build we care about, but a
# stripped or embedded interpreter can ship without it. Four vendors are
# SQLite-backed, and the right behaviour there is to report those four as
# unreadable with that exact reason rather than to fail the import and take
# the other eight down with them.
try:
    import sqlite3 as _sqlite3
except Exception:
    _sqlite3 = None

# tomllib landed in the standard library in 3.11. Devin's credentials.toml is
# the only TOML we read, so an older interpreter loses exactly that one vendor
# and is told so plainly. Hand-writing a TOML parser to cover that case would
# be a much larger risk than the vendor being temporarily undiscoverable.
try:
    import tomllib as _tomllib
except Exception:
    _tomllib = None

# aiaccounts already owns the Claude and Codex path knowledge, including the
# .env overrides and the wsl.localhost walk. Duplicating it here would create
# two places to fix when a path changes, so we call into it and accept that a
# failed import simply means those two report as unknown.
try:
    import aiaccounts as _accounts
except Exception:
    _accounts = None


# ── vocabulary ───────────────────────────────────────────────────────────────
# The kind of store, not the vendor. The UI uses it to decide what it can say:
# an encrypted store is worth a different sentence than a missing file.
KIND_JSON = "json"
KIND_TOML = "toml"
KIND_SQLITE_ITEMTABLE = "sqlite-itemtable"
KIND_SQLITE_ENCRYPTED = "sqlite-encrypted"
KIND_ENV = "env"
KIND_CLI = "cli-subprocess"

# Reason prefixes. A generic "failed" teaches the user nothing; each of these
# implies a different next action, which is the whole point of separating
# them: not found means log in, encrypted means we cannot help, unreadable
# means look at permissions or a running editor, malformed means the vendor
# changed their format and this module needs updating.
R_NOT_FOUND = "not found"
R_ENCRYPTED = "present but encrypted"
R_UNREADABLE = "present but unreadable"
R_MALFORMED = "present but malformed"
R_NO_SOURCE = "no discoverable credential source"
R_UNKNOWN_PROVIDER = "unknown provider"
# A file that is present and parseable but whose credential is older than
# the refresh-token lifetime. It is deliberately NOT folded into
# R_UNREADABLE: those two demand opposite actions from the user. Stale
# means the login in THIS file is genuinely finished and only a real
# re-login revives it; unreadable means the file we wanted was out of
# reach and we still do not know whether the login is alive.
R_STALE = "present but stale"

# Kept in sync with aiproviders.PROVIDERS by hand rather than by import, so a
# scan still enumerates every slug if that module is unavailable. The import
# below upgrades to the real list when it works.
_SLUGS = ["claude", "codex", "cursor", "copilot", "windsurf", "devin",
          "replit", "kimi", "glm", "cline", "antigravity", "railway"]

# How old the cached Windsurf quota document may be before this module calls
# it stale. Six hours is chosen because that document is only rewritten while
# the Windsurf editor is actually running, and the quota it reports only moves
# while the editor is being used: a document written within the current working
# session can still be trusted as a reading of this period's usage, whereas one
# written before it cannot, since any usage from another machine or another
# session in between is invisible here. The number is exported rather than
# buried so a caller with a different tolerance can apply its own rule to
# age_seconds; what this module owns is the FACT of staleness against a stated
# threshold, not the presentation policy that follows from it.
WINDSURF_PLAN_STALE_AFTER = 6 * 3600

# How far in the past a CLI credential's own expiry may sit before this module
# refuses to hand it back as the answer.
#
# This exists because of a failure this project actually shipped. The same
# Claude or Codex login lives in two places at once on this machine -- the
# Windows user profile and a WSL home -- and aiaccounts.cli_cred_paths()
# returns both. When a CLI logs out it does not delete its credential file, it
# guts it: expiresAt drops to 0 and the refresh token disappears, leaving a
# perfectly readable, perfectly parseable, perfectly dead document behind. A
# picker that walks the candidates and takes the first readable one -- or even
# the one with the latest expiry -- is safe only while every candidate is
# readable. The moment the live copy becomes unreachable (a stopped WSL
# distro, an unmounted share, a permissions change) the dead copy is the only
# survivor, is silently promoted to "the credential", and the refresh that
# follows fails with a 400 that gets shown to the user as an expired login --
# for a login that was never broken. The real fault was an unreadable file.
#
# So the candidates are aged out, not merely ranked: a credential older than
# the refresh token's own lifetime can never be revived and must never be
# returned as if it could. 16 days matches aiproviders.CRED_MAX_STALE and
# core.AI_CRED_MAX_STALE (Claude's measured refresh-token lifetime is ~15.8
# days, and Codex's is not shorter). The value is re-read from aiproviders at
# call time when that module is importable, so the three cannot drift apart.
CRED_MAX_STALE = 16 * 86400


def _max_stale():
    """The refresh-token lifetime to age candidates against.

    aiproviders owns this number for the whole widget; the local constant is
    the fallback for the case where that module is unavailable, which is the
    same posture this file already takes for sqlite3 and tomllib. A bad or
    hostile value there must not disable the aging check, so anything that is
    not a positive finite number falls back rather than being trusted.
    """
    try:
        import aiproviders as _p
        v = float(getattr(_p, "CRED_MAX_STALE", CRED_MAX_STALE))
        if v > 0 and v == v and v != float("inf"):
            return v
    except Exception:
        pass
    return float(CRED_MAX_STALE)


# The environment variables a user can export to supply a Cline key by hand.
# Cline is the one vendor with no readable store at all, so this list is the
# entire discovery surface for it.
_CLINE_ENV = ("CLINE_API_KEY", "CLINE_TOKEN")

_CLINE_HELP = ("Cline keeps its token in VS Code's encrypted secret storage, "
               "which has no readable file. Generate an API key in the Cline "
               "extension (Settings -> API provider) and export it as "
               "CLINE_API_KEY.")


def provider_slugs():
    """Every slug a scan will report on. Never raises."""
    try:
        import aiproviders as _p
        ids = [p["id"] for p in _p.PROVIDERS]
        return ids or list(_SLUGS)
    except Exception:
        return list(_SLUGS)


# ── path resolution ──────────────────────────────────────────────────────────
# Everything is resolved from environment variables at call time, never at
# import time and never from a literal. Two reasons: this repository is public
# and must contain no personal path, and the self-test below works by pointing
# APPDATA and friends at a temporary directory, which only works if nothing
# was cached during import.

def _expand(path):
    """Expand '~' and %ENVVAR%, mirroring aiaccounts._expand deliberately."""
    try:
        return os.path.expanduser(os.path.expandvars(path))
    except Exception:
        return path


def _home():
    try:
        return os.path.expanduser("~")
    except Exception:
        return ""


def _appdata():
    """Roaming application data, or the closest equivalent elsewhere.

    Windows is the only platform whose vendor paths we actually know, but
    returning a plausible directory on Linux and macOS keeps every function
    total instead of forcing a None check into each caller. A path that does
    not exist simply reports as not found, which is the truth.
    """
    v = os.environ.get("APPDATA")
    if v:
        return _expand(v)
    return os.path.join(_home(), ".config")


def _localappdata():
    v = os.environ.get("LOCALAPPDATA")
    if v:
        return _expand(v)
    return os.path.join(_home(), ".local", "share")


def _exists(path):
    try:
        return bool(path) and os.path.exists(path)
    except Exception:
        return False


def _size(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return None


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except Exception:
        return None


def _iso(ts):
    if not ts:
        return None
    try:
        return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat()
    except Exception:
        return None


def _loc(path, kind, confidence, key=None, note=None):
    """One candidate location, described the same way for every vendor.

    'key' is the ItemTable key or TOML/JSON field that holds the secret. It is
    a key NAME, never a value, so it is safe to display and safe to log.
    """
    rec = {
        "path": path,
        "kind": kind,
        "exists": _exists(path) if kind != KIND_ENV else bool(os.environ.get(path)),
        "confidence": confidence,
    }
    if key:
        rec["key"] = key
    if note:
        rec["note"] = note
    if rec["exists"] and kind != KIND_ENV:
        rec["size"] = _size(path)
        rec["mtime"] = _mtime(path)
        rec["mtime_iso"] = _iso(rec["mtime"])
    return rec


# ── SQLite, strictly read-only ───────────────────────────────────────────────

def _ro_uri(path):
    """A file: URI for sqlite3's uri=True mode, opened read-only.

    The URI form is the only way to get a genuinely read-only handle out of
    the sqlite3 module -- connecting to a bare path and promising not to write
    still opens the file for writing, creates it if missing, and can leave a
    -journal beside the vendor's database. mode=ro fixes all three.

    immutable=1 would additionally skip locking entirely, which is tempting
    given these files belong to running editors, but it tells SQLite the file
    cannot change while open. It can: the editor may be writing to it right
    now, and a stale or torn read would be indistinguishable from a valid one.
    A shared read lock held for the few milliseconds of one SELECT is the
    honest trade, and it does not block the editor's writes.
    """
    try:
        from pathlib import Path
        return Path(path).absolute().as_uri() + "?mode=ro"
    except Exception:
        # as_uri() is picky about UNC and about non-absolute input. A manual
        # fallback is better than losing the vendor entirely; percent-encoding
        # is skipped because the only characters at risk here are spaces and
        # sqlite tolerates them in this position.
        return "file:" + str(path).replace("\\", "/") + "?mode=ro"


def _itemtable(path, keys):
    """Read the named ItemTable keys out of a VS Code style state.vscdb.

    Returns (mapping, reason). The mapping holds only the keys that were
    present; a key that is absent is simply missing from it, which the callers
    distinguish from a database they could not open at all.
    """
    if _sqlite3 is None:
        return None, ("%s: this Python has no sqlite3 module, so SQLite-backed "
                      "vendors cannot be read (%s)" % (R_UNREADABLE, path))
    if not _exists(path):
        return None, "%s: %s" % (R_NOT_FOUND, path)
    con = None
    try:
        # timeout is short on purpose. If another process genuinely holds an
        # exclusive lock we want to give up quickly and report it, not sit
        # there for the default five seconds while the widget's poll stalls.
        con = _sqlite3.connect(_ro_uri(path), uri=True, timeout=1.0,
                               isolation_level=None)
        con.execute("PRAGMA query_only = 1")
        placeholders = ",".join("?" for _ in keys)
        rows = con.execute(
            "SELECT key, value FROM ItemTable WHERE key IN (%s)" % placeholders,
            tuple(keys)).fetchall()
    except Exception as e:
        # Type name only. SQLite error strings sometimes echo row content, and
        # the row content here is a bearer token.
        return None, "%s: %s (sqlite %s)" % (R_UNREADABLE, path,
                                             type(e).__name__)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    out = {}
    for k, v in rows:
        if isinstance(v, (bytes, bytearray)):
            try:
                v = bytes(v).decode("utf-8", "replace")
            except Exception:
                continue
        out[k] = v
    return out, None


def _read_json_file(path):
    """(document, reason). Distinguishes missing from unreadable from bad JSON."""
    if not _exists(path):
        return None, "%s: %s" % (R_NOT_FOUND, path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        return None, "%s: %s (%s)" % (R_UNREADABLE, path, type(e).__name__)
    try:
        return json.loads(raw), None
    except Exception:
        return None, ("%s: %s is not valid JSON (%s bytes)"
                      % (R_MALFORMED, path, len(raw)))


def _json_blob(text, where, key):
    """Parse a JSON document that was stored as an ItemTable value."""
    try:
        return json.loads(text), None
    except Exception:
        return None, ("%s: key %s in %s does not hold valid JSON"
                      % (R_MALFORMED, key, where))


# ── vendor location tables ───────────────────────────────────────────────────

def _cursor_locations():
    p = os.path.join(_appdata(), "Cursor", "User", "globalStorage", "state.vscdb")
    return [_loc(p, KIND_SQLITE_ITEMTABLE, "observed",
                 key="cursorAuth/accessToken",
                 note="VS Code global storage inherited by the Cursor fork")]


def _copilot_locations():
    base = os.path.join(_localappdata(), "github-copilot")
    return [
        _loc(os.path.join(base, "apps.json"), KIND_JSON, "observed",
             key="oauth_token",
             note="current readable layout; keys are host identifiers"),
        _loc(os.path.join(base, "hosts.json"), KIND_JSON, "observed",
             key="oauth_token",
             note="older installs used this filename"),
        _loc(os.path.join(base, "auth.db"), KIND_SQLITE_ENCRYPTED, "observed",
             key="oauth_tokens.token_ciphertext",
             note="newer installs; the column is encrypted and is not read"),
        _loc("GH_TOKEN", KIND_ENV, "observed",
             note="a token exported by hand is used if present"),
        _loc("gh auth token", KIND_CLI, "observed",
             note="opt-in only; never run during a scan"),
    ]


def _windsurf_locations():
    p = os.path.join(_appdata(), "Windsurf", "User", "globalStorage", "state.vscdb")
    return [
        _loc(p, KIND_SQLITE_ITEMTABLE, "observed", key="windsurfAuthStatus",
             note="JSON document carrying apiKey"),
        _loc(p, KIND_SQLITE_ITEMTABLE, "observed",
             key="windsurf.settings.cachedPlanInfo",
             note="cached quota; lets a row render with no network request"),
        _loc(p, KIND_SQLITE_ITEMTABLE, "observed", key="codeium.windsurf",
             note="account email"),
    ]


def _devin_locations():
    return [
        _loc(os.path.join(_appdata(), "devin", "credentials.toml"), KIND_TOML,
             "documented", key="windsurf_api_key",
             note="the only officially published Windows path in this file"),
        _loc(os.path.join(_appdata(), "Devin", "User", "globalStorage",
                          "state.vscdb"),
             KIND_SQLITE_ITEMTABLE, "inferred", key="windsurfAuthStatus",
             note="desktop app state store; pattern borrowed from Windsurf, "
                  "not confirmed"),
    ]


def _kimi_locations():
    base = os.path.join(_home(), ".kimi-code")
    return [
        _loc(os.path.join(base, "credentials", "kimi-code.json"), KIND_JSON,
             "observed", note="login document"),
        _loc(os.path.join(base, "device_id"), KIND_JSON, "observed",
             note="plain text; sent as a device header on the usage request"),
    ]


def _cline_locations():
    return [_loc(name, KIND_ENV, "documented", note=_CLINE_HELP)
            for name in _CLINE_ENV]


def _cli_locations(provider):
    """Claude and Codex, delegated to the module that already owns them."""
    if _accounts is None:
        return []
    try:
        paths = _accounts.cli_cred_paths(provider) or []
    except Exception:
        return []
    return [_loc(p, KIND_JSON, "documented", note="CLI credential file")
            for p in paths]


def cred_locations(provider):
    """Every candidate credential location for one slug, best first.

    Each record carries the path (or, for an environment source, the variable
    name), the kind of store it is, whether it exists right now, and how
    confident we are in the path. Existing files additionally carry size and
    mtime, which are the two facts a user needs to tell a live login from a
    leftover. Never raises; an unknown slug yields an empty list.
    """
    # Everything below compares, hashes or formats the slug, and all three of
    # those operations are attacker-controlled once the argument is an
    # arbitrary object: a list is unhashable, a dict lookup on it raises, and a
    # class with a hostile __eq__ or __repr__ turns a comparison or a log line
    # into an exception. Rejecting anything that is not a string up front is
    # cheaper and more honest than catching the fallout afterwards.
    if not isinstance(provider, str):
        return []
    try:
        if provider in ("claude", "codex"):
            return _cli_locations(provider)
        builder = {
            "cursor": _cursor_locations,
            "copilot": _copilot_locations,
            "windsurf": _windsurf_locations,
            "devin": _devin_locations,
            "kimi": _kimi_locations,
            "cline": _cline_locations,
        }.get(provider)
        return builder() if builder else []
    except Exception:
        return []


# ── vendor readers ───────────────────────────────────────────────────────────
# Each reader returns (credential, reason): exactly one of the two is None.
# The credential shape follows the house convention set by Claude and Codex --
# one top-level key named for the vendor, holding the fields an adapter needs.
# None of these vendors has an adapter in aiproviders yet (all twelve non-CLI
# slugs are status "planned"), so these shapes are the contract the adapter
# will be written against rather than one it already imposes.

def _b64_json(segment):
    """Decode one base64url JWT segment into a dict. None when it is not one."""
    try:
        seg = segment + "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(seg).decode("utf-8", "replace"))
    except Exception:
        return None


def _cursor_user_id(jwt):
    """The user id Cursor's usage endpoint wants, out of the token's sub claim.

    The claim looks like "<provider>|<id>"; the REST call wants the part after
    the bar. This is a parse of a token we already hold, not a verification --
    nothing is trusted on the strength of it, and the signature is irrelevant
    because the token is about to be sent back to the issuer anyway.

    Returns None rather than guessing when the claim is absent or unsplit, so
    a caller can say "token found, user id unknown" instead of sending a
    malformed request.
    """
    try:
        parts = (jwt or "").split(".")
        if len(parts) < 2:
            return None
        payload = _b64_json(parts[1]) or {}
        sub = payload.get("sub")
        if not isinstance(sub, str) or "|" not in sub:
            return None
        return sub.split("|")[1] or None
    except Exception:
        return None


def _read_cursor():
    locs = _cursor_locations()
    path = locs[0]["path"]
    key = "cursorAuth/accessToken"
    vals, why = _itemtable(path, [key])
    if vals is None:
        return None, why
    token = vals.get(key)
    if not token:
        return None, ("%s: %s has no ItemTable row for key %s -- log in inside "
                      "the Cursor app" % (R_NOT_FOUND, path, key))
    return {"cursorAuth": {"accessToken": token,
                           "userId": _cursor_user_id(token)}}, None


def _read_copilot(allow_subprocess=False):
    """apps.json, then hosts.json, then the encrypted database, then GH_TOKEN.

    The order matters: the encrypted database is checked only after the
    readable files have been ruled out, because an install that has both --
    which happens mid-migration -- should be read from the file rather than
    reported as encrypted.
    """
    base = os.path.join(_localappdata(), "github-copilot")
    tried = []
    for name in ("apps.json", "hosts.json"):
        path = os.path.join(base, name)
        tried.append(path)
        if not _exists(path):
            continue
        doc, why = _read_json_file(path)
        if doc is None:
            return None, why
        if not isinstance(doc, dict):
            return None, "%s: %s is not a JSON object" % (R_MALFORMED, path)
        # Keys are host identifiers such as "github.com:Iv1.xxxx". Prefer a
        # github.com entry; an enterprise host is accepted as a fallback
        # because a user on GHES has no github.com row at all.
        best = None
        for hk, hv in doc.items():
            if not isinstance(hv, dict) or not hv.get("oauth_token"):
                continue
            if str(hk).startswith("github.com"):
                best = (hk, hv)
                break
            if best is None:
                best = (hk, hv)
        if best is None:
            return None, ("%s: %s has no entry carrying an oauth_token field"
                          % (R_MALFORMED, path))
        hk, hv = best
        cred = {"copilotAuth": {"oauth_token": hv["oauth_token"], "host": hk}}
        if hv.get("user"):
            cred["copilotAuth"]["user"] = hv["user"]
        return cred, None

    authdb = os.path.join(base, "auth.db")
    if _exists(authdb):
        # Deliberately not decrypted, and deliberately not silently skipped.
        # The user's Copilot login genuinely IS on this machine; saying "not
        # found" would send them off to re-run a login that already worked.
        # The key material lives in the OS keychain under GitHub's own scheme,
        # and reverse-engineering another product's at-rest encryption is not
        # something this widget should be doing.
        return None, (
            "%s: %s holds the Copilot login in table oauth_tokens, column "
            "token_ciphertext, which newer Copilot builds encrypt. This module "
            "does not decrypt another application's secrets. Supply a token "
            "instead: export GH_TOKEN, or opt in to reading it from 'gh auth "
            "token'." % (R_ENCRYPTED, authdb))

    env_tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if env_tok:
        return {"copilotAuth": {"oauth_token": env_tok, "host": "env"}}, None

    if allow_subprocess:
        tok, why = copilot_token_via_gh()
        if tok:
            return {"copilotAuth": {"oauth_token": tok, "host": "gh-cli"}}, None
        return None, why

    return None, ("%s: no %s in %s, no auth.db, and GH_TOKEN is unset -- run "
                  "'gh auth login' or export GH_TOKEN"
                  % (R_NOT_FOUND, " or ".join(os.path.basename(p) for p in tried),
                     base))


def copilot_token_via_gh(timeout=10):
    """Ask the GitHub CLI for a token. Explicit opt-in, never automatic.

    This is the one function in the file that leaves the process, which is why
    it is not reachable from cred_locations() or scan_all(). Spawning another
    vendor's binary during a background scan is a surprise: it can prompt, it
    can hit the network, and on a machine with a misconfigured gh it can hang.
    A caller that wants it must ask for it by name.

    Returns (token, reason). The token is returned, never logged.
    """
    try:
        import shutil
        import subprocess
    except Exception as e:
        return None, "%s: cannot spawn gh (%s)" % (R_UNREADABLE, type(e).__name__)
    exe = None
    try:
        exe = shutil.which("gh")
    except Exception:
        exe = None
    if not exe:
        return None, "%s: the GitHub CLI ('gh') is not on PATH" % R_NOT_FOUND
    try:
        out = subprocess.run([exe, "auth", "token"], capture_output=True,
                             text=True, timeout=timeout)
    except Exception as e:
        return None, "%s: 'gh auth token' failed (%s)" % (R_UNREADABLE,
                                                          type(e).__name__)
    if out.returncode != 0:
        # stderr is not echoed: gh is generally well behaved, but a third
        # party's error text is not something to forward into our logs when
        # the same command also prints secrets on success.
        return None, ("%s: 'gh auth token' exited %s -- run 'gh auth login'"
                      % (R_NOT_FOUND, out.returncode))
    tok = (out.stdout or "").strip()
    if not tok:
        return None, "%s: 'gh auth token' printed nothing" % R_NOT_FOUND
    return tok, None


def _read_windsurf():
    path = _windsurf_locations()[0]["path"]
    k_auth = "windsurfAuthStatus"
    k_plan = "windsurf.settings.cachedPlanInfo"
    k_acct = "codeium.windsurf"
    vals, why = _itemtable(path, [k_auth, k_plan, k_acct])
    if vals is None:
        return None, why
    raw = vals.get(k_auth)
    if not raw:
        return None, ("%s: %s has no ItemTable row for key %s -- log in inside "
                      "the Windsurf app" % (R_NOT_FOUND, path, k_auth))
    doc, why = _json_blob(raw, path, k_auth)
    if doc is None:
        return None, why
    api_key = doc.get("apiKey") if isinstance(doc, dict) else None
    if not api_key:
        return None, ("%s: key %s in %s carries no apiKey field"
                      % (R_MALFORMED, k_auth, path))
    cred = {"windsurfAuth": {"apiKey": api_key}}
    acct = vals.get(k_acct)
    if acct:
        # Sometimes a bare string, sometimes a JSON object holding the email.
        obj, _ = _json_blob(acct, path, k_acct)
        if isinstance(obj, dict):
            email = obj.get("email") or obj.get("userEmail")
        else:
            email = acct if isinstance(acct, str) else None
        if email:
            cred["windsurfAuth"]["email"] = email
    plan = vals.get(k_plan)
    if plan:
        obj, _ = _json_blob(plan, path, k_plan)
        if obj is not None:
            # Carried along with its staleness stamp, never without one. See
            # windsurf_cached_plan() for why that stamp is mandatory.
            cred["windsurfAuth"]["cachedPlanInfo"] = obj
            cred["windsurfAuth"]["cachedPlanInfoAt"] = _mtime(path)
    return cred, None


def windsurf_cached_plan():
    """The cached Windsurf quota document plus when it was last written.

    This one is worth a dedicated function because it is the only quota in the
    whole widget that costs no network request at all: the Windsurf editor
    writes its plan info into its own state store, so the number is simply
    sitting there.

    The catch, and the reason the timestamp is returned alongside rather than
    optionally: it is only as fresh as the last time the Windsurf editor ran.
    On a machine where Windsurf was last opened three weeks ago this document
    will cheerfully report a quota from three weeks ago, and displaying that
    as a current reading would be exactly the class of confident-but-wrong
    number aiproviders.py refuses to ship. A caller must look at age_seconds
    and either label the row as stale or decline to show it.

    The timestamp is the state database's mtime, not a field inside the
    document, because the document has no self-reported write time. That makes
    it an upper bound on freshness: the database is touched by plenty of
    unrelated editor settings, so the plan info can be older than the mtime
    but never newer. Erring towards "looks fresher than it is" is the wrong
    direction, so treat age_seconds as a floor.

    The result therefore carries a machine-readable 'stale' boolean next to
    age_seconds, evaluated against WINDSURF_PLAN_STALE_AFTER. It is a boolean
    and not a rendering decision on purpose: leaving every caller to re-derive
    staleness from the age guarantees that one of them eventually forgets and
    paints a three-week-old quota as a current figure, which is precisely the
    confident-but-wrong number this project refuses to ship. A caller that
    disagrees with the threshold still has age_seconds and the exported
    constant and can decide for itself; a caller that has not thought about it
    at all now gets the safe answer by default. When the mtime is unavailable
    the age is unknown, and an unknown age is reported as stale rather than
    fresh, because the failure direction that hides a bad number is preferable
    to the one that presents it.

    Returns a dict, always, with an 'ok' flag and a 'reason' when not ok.
    """
    try:
        path = _windsurf_locations()[0]["path"]
        key = "windsurf.settings.cachedPlanInfo"
        vals, why = _itemtable(path, [key])
        if vals is None:
            return {"ok": False, "reason": why, "path": path, "key": key}
        raw = vals.get(key)
        if not raw:
            return {"ok": False, "path": path, "key": key,
                    "reason": ("%s: %s has no ItemTable row for key %s -- open "
                               "the Windsurf app once to populate it"
                               % (R_NOT_FOUND, path, key))}
        doc, why = _json_blob(raw, path, key)
        if doc is None:
            return {"ok": False, "reason": why, "path": path, "key": key}
        ts = _mtime(path)
        age = (time.time() - ts) if ts else None
        return {
            "ok": True,
            "path": path,
            "key": key,
            "plan": doc,
            "written_at": ts,
            "written_at_iso": _iso(ts),
            "age_seconds": age,
            "stale": (True if age is None else age > WINDSURF_PLAN_STALE_AFTER),
            "stale_after_seconds": WINDSURF_PLAN_STALE_AFTER,
            "reason": None,
        }
    except Exception as e:
        return {"ok": False, "stale": True, "age_seconds": None,
                "reason": "%s: windsurf cached plan (%s)"
                % (R_UNREADABLE, type(e).__name__)}


def _read_devin():
    toml_path = os.path.join(_appdata(), "devin", "credentials.toml")
    if _exists(toml_path):
        if _tomllib is None:
            return None, ("%s: %s needs the tomllib module, which this Python "
                          "build does not provide (Python 3.11+ has it)"
                          % (R_UNREADABLE, toml_path))
        try:
            with open(toml_path, "rb") as f:
                doc = _tomllib.load(f)
        except Exception as e:
            return None, "%s: %s (%s)" % (R_MALFORMED, toml_path,
                                          type(e).__name__)
        key = doc.get("windsurf_api_key") if isinstance(doc, dict) else None
        if not key:
            return None, ("%s: %s has no windsurf_api_key key"
                          % (R_MALFORMED, toml_path))
        cred = {"devinAuth": {"api_key": key}}
        if doc.get("api_server_url"):
            cred["devinAuth"]["api_server_url"] = doc["api_server_url"]
        return cred, None

    # Fallback: the desktop app's own state store. Marked inferred in the
    # table above, so it is probed but never presented as authoritative.
    db = os.path.join(_appdata(), "Devin", "User", "globalStorage", "state.vscdb")
    k = "windsurfAuthStatus"
    if _exists(db):
        vals, why = _itemtable(db, [k])
        if vals is None:
            return None, why
        raw = vals.get(k)
        if raw:
            doc, why = _json_blob(raw, db, k)
            if doc is None:
                return None, why
            if isinstance(doc, dict) and doc.get("apiKey"):
                return {"devinAuth": {"api_key": doc["apiKey"]}}, None
            return None, ("%s: key %s in %s carries no apiKey field"
                          % (R_MALFORMED, k, db))
    return None, ("%s: neither %s nor %s exists -- log in with the Devin app "
                  "or CLI" % (R_NOT_FOUND, toml_path, db))


def _read_kimi():
    base = os.path.join(_home(), ".kimi-code")
    cred_path = os.path.join(base, "credentials", "kimi-code.json")
    doc, why = _read_json_file(cred_path)
    if doc is None:
        return None, why
    if not isinstance(doc, dict):
        return None, "%s: %s is not a JSON object" % (R_MALFORMED, cred_path)
    cred = {"kimiAuth": {"credentials": doc}}
    # The device id is a separate plain-text file and the usage request wants
    # it as a header. Its absence is reported inside the credential rather
    # than failing the read: the login itself is still usable for anything
    # that does not need the header, and saying which half is missing is more
    # useful than refusing both.
    dev_path = os.path.join(base, "device_id")
    try:
        with open(dev_path, "r", encoding="utf-8") as f:
            dev = f.read().strip()
    except Exception:
        dev = None
    if dev:
        cred["kimiAuth"]["device_id"] = dev
    else:
        cred["kimiAuth"]["device_id_missing"] = dev_path
    return cred, None


def _read_cline():
    for name in _CLINE_ENV:
        v = os.environ.get(name)
        if v:
            return {"clineAuth": {"apiKey": v, "source_env": name}}, None
    return None, "%s: %s Checked: %s." % (R_NO_SOURCE, _CLINE_HELP,
                                          ", ".join(_CLINE_ENV))


def _cli_refresh_key_present(provider, doc):
    """Does this document still carry a refresh token KEY? Boolean only.

    Never touches the value. A logged-out CLI credential is not deleted, it is
    gutted: the refresh token key disappears and the expiry drops to zero. The
    presence of the key is therefore the single most reliable "is there
    anything left to renew here" signal, and it is readable without ever
    looking at a secret.
    """
    try:
        if provider == "claude":
            o = doc.get("claudeAiOauth")
            return isinstance(o, dict) and bool(o.get("refreshToken"))
        if provider == "codex":
            t = doc.get("tokens")
            return isinstance(t, dict) and bool(t.get("refresh_token"))
    except Exception:
        return False
    return False


def _cli_cred_expiry(provider, doc):
    """This credential's own expiry as UNIX seconds, or None when unknown.

    aiproviders.cred_expiry owns this per-vendor knowledge for the whole
    widget, so it is asked first and only, so the two cannot drift. When that
    module is unavailable the fallback reads the two plain, non-secret fields
    this file is allowed to know about: Claude's epoch-MILLISECOND expiresAt
    and Codex's last_refresh stamp. Both are timestamps, not tokens.

    None means "cannot tell", and the caller must never promote that to
    "dead": an unrecognised shape is not an expired login.
    """
    try:
        import aiproviders as _p
        v = _p.cred_expiry(provider, doc)
        if v is not None:
            return float(v)
    except Exception:
        pass
    try:
        if provider == "claude":
            o = doc.get("claudeAiOauth")
            if isinstance(o, dict):
                v = o.get("expiresAt")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    # Zero is not "unknown" here, it is the gutted-file marker
                    # a logged-out CLI leaves behind, and 1970 is correctly
                    # older than any refresh-token lifetime.
                    return float(v) / 1000.0
        elif provider == "codex":
            v = doc.get("last_refresh")
            if isinstance(v, str) and v:
                s = v.replace("Z", "+00:00")
                return _dt.datetime.fromisoformat(s).timestamp()
    except Exception:
        pass
    return None


# Per-candidate verdicts. These are the vocabulary the whole stale-promotion
# fix is built on, and they are deliberately four states rather than the two
# ("worked" / "did not work") that let the original bug through.
CAND_LIVE = "live"            # readable, parsed, and young enough to renew
CAND_STALE = "stale"          # readable and parsed, but past renewal
CAND_UNKNOWN_AGE = "unknown-age"   # readable, but the shape hides the expiry
CAND_UNREADABLE = "unreadable"     # present but locked or malformed
CAND_UNREACHABLE = "unreachable"   # we could not even get far enough to look
CAND_ABSENT = "absent"             # we looked there and there is nothing


def _cli_path_reachable(path):
    """Could we actually get far enough to see whether this file is there?

    This distinction is load-bearing, and missing it is how the original bug
    reappears in a new disguise. os.path.exists() on a credential inside a
    stopped WSL distro or an unmounted share returns False -- exactly the same
    answer it gives for a machine where the user simply never logged in there.
    Treating both as "not logged in" lets the surviving stale copy become the
    only candidate all over again.

    The tell is the ancestry. A credential lives at <home>/<vendor-dir>/<file>,
    so if either the vendor directory or the home above it is present, we truly
    did look and the file truly is not there. If NEITHER is present, the whole
    location is out of reach and the honest answer is "cannot tell", not "no".

    Purely local: existence checks on directories, no network, no reads.
    """
    try:
        d = os.path.dirname(path)
        return bool(_exists(d) or _exists(os.path.dirname(d)))
    except Exception:
        return False


def _cli_candidate(provider, path, now=None):
    """Classify one CLI credential file. Never returns a value from inside it.

    The returned record carries the path, the verdict, the age in seconds and
    a boolean for whether a refresh-token key is present. Every one of those
    is safe to print, log and paste into a public bug report.
    """
    rec = {"path": path, "state": CAND_UNREADABLE, "age_seconds": None,
           "expires_at": None, "expires_at_iso": None,
           "has_refresh_key": False, "reason": None}
    doc, why = _read_json_file(path)
    if doc is None:
        rec["reason"] = why
        if not _exists(path):
            rec["state"] = (CAND_ABSENT if _cli_path_reachable(path)
                            else CAND_UNREACHABLE)
            if rec["state"] == CAND_UNREACHABLE:
                rec["reason"] = (
                    "%s: %s is out of reach -- neither it nor the directory "
                    "above it can be seen, which is what a stopped WSL distro, "
                    "an unmounted share or a revoked permission looks like. "
                    "Whether a login exists there is unknown, not absent."
                    % (R_UNREADABLE, path))
        return rec, None
    if not isinstance(doc, dict):
        rec["reason"] = "%s: %s is not a JSON object" % (R_MALFORMED, path)
        return rec, None
    rec["has_refresh_key"] = _cli_refresh_key_present(provider, doc)
    exp = _cli_cred_expiry(provider, doc)
    rec["expires_at"] = exp
    rec["expires_at_iso"] = _iso(exp)
    now = time.time() if now is None else now
    limit = _max_stale()
    if exp is None:
        # Unknown age. Not stale -- "I cannot read the expiry out of this
        # shape" must never graduate into "your login is dead" -- but not
        # trusted over a candidate whose freshness is actually established,
        # which is why the caller ranks it below CAND_LIVE.
        rec["state"] = CAND_UNKNOWN_AGE
    else:
        rec["age_seconds"] = now - exp
        if rec["age_seconds"] > limit or not rec["has_refresh_key"]:
            # Two independent ways to be past saving, and both occur on this
            # machine: an expiry further in the past than the refresh token's
            # own lifetime, and a gutted file whose refresh key is simply gone.
            rec["state"] = CAND_STALE
            rec["reason"] = (
                "%s: %s expired %.1f days ago%s, which is beyond the "
                "refresh-token lifetime of %.1f days -- this copy cannot be "
                "renewed and only a real re-login revives it"
                % (R_STALE, path, (rec["age_seconds"] or 0) / 86400.0,
                   "" if rec["has_refresh_key"] else " and carries no "
                   "refresh-token key",
                   limit / 86400.0))
        else:
            rec["state"] = CAND_LIVE
    return rec, doc


def cli_cred_candidates(provider):
    """Every CLI credential candidate for one slug, classified, value-free.

    Exported because the distinction it draws is exactly what a diagnostics
    view has to show the user: which copies of this login exist, which of them
    could be read at all, and which of the readable ones are past renewal.
    Collapsing that into a single "found / not found" is what produced the
    false alarm documented on _read_cli below. Never raises.
    """
    if not isinstance(provider, str) or provider not in ("claude", "codex"):
        return []
    out = []
    try:
        for rec in cred_locations(provider):
            cand, _doc = _cli_candidate(provider, rec["path"])
            out.append(cand)
    except Exception:
        return out
    return out


def _read_cli(provider):
    """Claude and Codex: the youngest still-renewable credential file wins.

    Path knowledge and JSON parsing both come from aiaccounts so the two
    modules cannot drift apart. This exists only so scan_all() can cover all
    twelve slugs uniformly; the real import path stays aiaccounts.import_from_cli.

    The selection rule is the interesting part, and it is written against a
    failure this project actually shipped. The same login exists twice on this
    machine -- once in the Windows profile, once in a WSL home -- and one of
    those copies is an abandoned leftover whose expiry is zero and whose
    refresh token is gone. "First readable file wins" is correct exactly as
    long as every copy is readable. Stop the WSL distro and the live copy
    becomes unreachable, the dead leftover is the only survivor, it wins by
    default, and the refresh attempt that follows fails with a 400 that the
    user is shown as "login expired" -- for a login that is perfectly healthy.
    An unreadable fresh file must never silently promote a stale one.

    So candidates are AGED OUT rather than merely ranked, and the failure the
    caller is told about names the real fault:

      * a live candidate exists            -> it is returned, freshest first.
      * every candidate is readable and
        past the refresh-token lifetime    -> R_STALE. This one genuinely does
                                              mean re-login.
      * some candidate could not be read
        and the only survivors are stale   -> R_UNREADABLE, naming the path we
                                              could not reach. This one means
                                              "fix the share / start the
                                              distro", NOT "log in again", and
                                              keeping the two apart is the
                                              entire point of this function.
    """
    if _accounts is None:
        return None, ("%s: the aiaccounts module could not be imported, so the "
                      "%s CLI paths are unknown" % (R_UNREADABLE, provider))
    paths = cred_locations(provider)
    if not paths:
        return None, "%s: no candidate path for %s" % (R_NOT_FOUND, provider)

    live = []       # (expiry_or_-inf, index, path, doc)
    unknown = []    # readable, age not determinable
    stale = []      # readable, past renewal
    blocked = []    # present but could not be read
    missing = []    # simply not there
    for i, rec in enumerate(paths):
        path = rec["path"]
        cand, doc = _cli_candidate(provider, path)
        if cand["state"] == CAND_LIVE:
            # Ties are broken by candidate order, which cred_locations
            # already orders best-first, so an explicit .env path still wins
            # over the default when two copies share an expiry.
            live.append((cand["expires_at"] or 0.0, -i, path, doc))
        elif cand["state"] == CAND_STALE:
            stale.append(cand)
        elif cand["state"] == CAND_UNKNOWN_AGE:
            unknown.append((i, path, doc))
        elif cand["state"] == CAND_ABSENT:
            missing.append(cand)
        else:
            # Present-but-unreadable and out-of-reach are pooled on purpose.
            # They differ in cause but not in consequence: in both, a copy of
            # this login that we cannot judge exists somewhere in the candidate
            # list, so no verdict drawn from the remaining copies is complete.
            blocked.append(cand)

    if live:
        live.sort(key=lambda t: (t[0], t[1]))
        return live[-1][3], None

    # No candidate whose freshness could be established. A readable file of an
    # unrecognised shape is still preferable to nothing -- refusing it would
    # break every future credential format on the day the vendor changes it --
    # but it is only reached once no live copy exists, so it can never shadow
    # one.
    if unknown:
        return unknown[0][2], None

    if blocked:
        # The real fault. Reported as unreadable even though a stale copy is
        # sitting right there, because handing that copy back is precisely the
        # silent promotion this function exists to prevent, and because the
        # user's next action is to restore access to the path below, not to
        # redo a login that may well be fine.
        note = ""
        if stale:
            note = (" A stale copy of this login is readable at %s, and it is "
                    "deliberately NOT used: refreshing from it would fail and "
                    "be reported as an expired login, which is not what is "
                    "wrong here." % stale[0]["path"])
        return None, ("%s: %s could not be read, so the freshest %s credential "
                      "on this machine may simply be out of reach. This is not "
                      "an expired login.%s"
                      % (R_UNREADABLE, blocked[0]["path"], provider, note))

    if stale:
        # Every copy was readable and every copy is past saving. This is the
        # one case where "log in again" is the honest advice.
        return None, stale[0]["reason"]

    return None, (missing[0]["reason"] if missing else
                  ("%s: no readable credential file for %s"
                   % (R_NOT_FOUND, provider)))


_READERS = {
    "cursor": _read_cursor,
    "windsurf": _read_windsurf,
    "devin": _read_devin,
    "kimi": _read_kimi,
    "cline": _read_cline,
}


def read_cred(provider, allow_subprocess=False):
    """(credential, reason) for one slug. Exactly one of the two is None.

    The reason is always specific enough to act on: which path was checked,
    which key was missing, whether the store was encrypted rather than absent.
    "failed" would tell the user nothing and would make every vendor's
    troubleshooting identical, which is the opposite of useful.

    allow_subprocess only affects Copilot, and only enables the 'gh auth token'
    fallback. It defaults to False so a background scan never spawns anything.

    Never raises, for any input, including an unknown slug.
    """
    # The slug is validated as a string before anything is done with it, for
    # the same reason as in cred_locations(): comparison, hashing and string
    # formatting are all able to raise when the value is an arbitrary object,
    # so the only safe move is to never let such a value reach them. The
    # rejection message names the TYPE and never the value, because formatting
    # the value is itself one of the operations that can raise.
    if not isinstance(provider, str):
        return None, "%s: expected a provider slug string, got %s" % (
            R_UNKNOWN_PROVIDER, type(provider).__name__)
    try:
        if provider in ("claude", "codex"):
            return _read_cli(provider)
        if provider == "copilot":
            return _read_copilot(allow_subprocess=allow_subprocess)
        fn = _READERS.get(provider)
        if fn is not None:
            return fn()
        if provider in provider_slugs():
            return None, ("%s: no credential location has been established for "
                          "'%s' yet, so there is nothing to discover. Add the "
                          "account by hand once its token location is known."
                          % (R_NO_SOURCE, provider))
        return None, "%s: '%s'" % (R_UNKNOWN_PROVIDER, provider)
    except Exception as e:
        # The total-function guarantee is enforced here as well as promised in
        # each reader, because a discovery layer that throws takes down the
        # whole first-run import over one odd file.
        #
        # The reason names the reader's TYPE, never the provider value itself.
        # Interpolating an untrusted value into the message hands control of
        # this error path to whatever __repr__ that value happens to define,
        # and a __repr__ that raises would turn the guarantee above into a
        # lie exactly when it matters most.
        return None, "%s: %s reader raised %s" % (
            R_UNREADABLE, type(provider).__name__, type(e).__name__)


def _cli_source(slug, cands, locs):
    """Which path actually produced the answer, best-effort and value-free.

    A stale candidate is never named as the source even when it is the only
    file that exists, because "source" is read as "this is where your login
    came from" and a leftover is not where it came from.
    """
    try:
        for state in (CAND_LIVE, CAND_UNKNOWN_AGE):
            for c in cands or []:
                if c.get("state") == state:
                    return c.get("path")
        return next((l["path"] for l in locs if l.get("exists")), None)
    except Exception:
        return None


def describe(slug):
    """A human-readable, credential-free summary of one slug's discovery story.

    This exists so a diagnostics view can explain a single vendor without
    calling read_cred() and then having to remember to strip the credential
    out of the answer. Nothing it returns is ever token material: it reports
    where this module looks, whether those places exist, how big they are and
    when they were last written, plus the reason string read_cred() would give
    -- and the reason strings are already written to name paths and keys
    rather than values. Building the safe view as its own function is what
    makes "no secret reaches the renderer" a property of the code instead of a
    rule each caller has to follow.

    Total for any input: an unrecognised or non-string slug is described as
    unknown rather than raising, and the description names the argument's type
    rather than the argument, since formatting the value could itself raise.
    """
    try:
        if not isinstance(slug, str):
            return {"provider": None, "known": False, "found": False,
                    "locations": [], "reason": "%s: expected a provider slug "
                    "string, got %s" % (R_UNKNOWN_PROVIDER,
                                        type(slug).__name__)}
        known = slug in provider_slugs()
        locs = cred_locations(slug)
        cands = cli_cred_candidates(slug)
        cred, why = read_cred(slug)
        return {
            "provider": slug,
            "known": known,
            "found": cred is not None,
            "reason": why,
            # The locations already come back free of any value; they carry
            # paths, kinds, sizes and mtimes only, which is exactly the set of
            # facts that tells a user whether a login is live or left over.
            "locations": locs,
            # For the two CLI vendors the first EXISTING path is not
            # necessarily the one that answered: a dead leftover can exist
            # beside the live copy, and naming it here would misattribute the
            # result in exactly the diagnostics view a user opens when
            # something looks wrong. The chosen candidate is reported instead,
            # with the per-candidate verdicts alongside it.
            "candidates": cands,
            "source": _cli_source(slug, cands, locs),
        }
    except Exception as e:
        return {"provider": None, "known": False, "found": False,
                "locations": [],
                "reason": "%s: describe (%s)" % (R_UNREADABLE,
                                                 type(e).__name__)}


def scan_all(include_creds=True):
    """Everything discoverable on this machine, for first-run import and for
    the diagnostics view.

    include_creds=True returns the credential objects themselves, which is
    what an import needs. They are secrets: this is an in-process API in the
    same sense as aiaccounts.list_accounts, and nothing here should cross the
    stdio boundary to the renderer unredacted. Pass include_creds=False for
    anything that gets displayed or logged -- the result is then free of token
    material by construction rather than by the caller remembering to strip it.

    Never raises. A vendor whose reader misbehaves is reported as unreadable
    and the scan continues.
    """
    # The flag is reduced to a real bool once, up front. Evaluating an
    # arbitrary object's truthiness inside the loop would hand a hostile
    # __bool__ the ability to abort a scan halfway through, and a scan that
    # dies partway is indistinguishable to the caller from a machine with no
    # logins on it -- the worst possible failure for a discovery layer.
    try:
        include_creds = bool(include_creds)
    except Exception:
        include_creds = False
    out = {"generated_at": _iso(time.time()), "providers": {}}
    for slug in provider_slugs():
        try:
            locs = cred_locations(slug)
        except Exception:
            locs = []
        cred, why = read_cred(slug)
        rec = {
            "provider": slug,
            "found": cred is not None,
            "reason": why,
            "locations": locs,
            "source": None,
        }
        # Which location actually produced the answer is the single most
        # useful diagnostic line, so it is recorded rather than left for the
        # reader to infer from the list.
        cands = cli_cred_candidates(slug)
        if cands:
            # Value-free: paths, verdicts, ages and a has-refresh-key boolean.
            # Carried even in the redacted scan because it is exactly what
            # distinguishes "the fresh copy was unreachable" from "this login
            # is over", and a diagnostics view that cannot tell those apart is
            # how the false alarm happened in the first place.
            rec["candidates"] = cands
        chosen = _cli_source(slug, cands, locs)
        for loc in locs:
            if loc.get("exists") and (chosen is None or loc["path"] == chosen):
                rec["source"] = loc["path"]
                rec["source_kind"] = loc["kind"]
                break
        if cred is not None and include_creds:
            rec["cred"] = cred
        out["providers"][slug] = rec
    out["found"] = sorted(s for s, r in out["providers"].items() if r["found"])
    out["missing"] = sorted(s for s, r in out["providers"].items()
                            if not r["found"])
    return out


# ── self-test ────────────────────────────────────────────────────────────────
# Run this file directly. It builds fake stores in a temporary directory with
# the real table, key and field names, points the environment at them, and
# proves each reader pulls the right field out. Fixtures rather than the real
# machine, because a test that only passes on a developer's laptop tests
# nothing -- and because the real stores must never be written to.
#
# It then runs a genuine read-only scan of this machine, which is safe by
# construction and genuinely informative. That section prints paths, existence,
# key names and sizes only. No value from any store is ever printed.

def _selftest():
    import shutil
    import tempfile

    root = tempfile.mkdtemp(prefix="aicredsrc-")
    ok = []
    fail = []

    def check(name, cond, detail=""):
        (ok if cond else fail).append(name)
        print("  %-42s %s%s" % (name, "PASS" if cond else "FAIL",
                                (" " + detail) if detail else ""))

    saved = {k: os.environ.get(k) for k in
             ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME",
              "GH_TOKEN", "GITHUB_TOKEN", "CLINE_API_KEY", "CLINE_TOKEN")}
    try:
        appdata = os.path.join(root, "Roaming")
        localapp = os.path.join(root, "Local")
        home = os.path.join(root, "home")
        for d in (appdata, localapp, home):
            os.makedirs(d, exist_ok=True)
        os.environ["APPDATA"] = appdata
        os.environ["LOCALAPPDATA"] = localapp
        os.environ["USERPROFILE"] = home
        os.environ["HOME"] = home
        for k in ("GH_TOKEN", "GITHUB_TOKEN", "CLINE_API_KEY", "CLINE_TOKEN"):
            os.environ.pop(k, None)

        # A JWT whose sub claim is "auth0|user-abc123": index 1 after the bar
        # is the user id Cursor's usage endpoint wants.
        def b64(obj):
            return base64.urlsafe_b64encode(
                json.dumps(obj).encode()).decode().rstrip("=")
        jwt = "%s.%s.%s" % (b64({"alg": "HS256"}),
                            b64({"sub": "auth0|user-abc123"}), "sig")

        def make_itemtable(path, rows):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            con = _sqlite3.connect(path)
            con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
            con.executemany("INSERT INTO ItemTable VALUES (?, ?)", rows)
            con.commit()
            con.close()

        print("\n=== fixtures ===")

        # Cursor
        cur_db = os.path.join(appdata, "Cursor", "User", "globalStorage",
                              "state.vscdb")
        make_itemtable(cur_db, [("cursorAuth/accessToken", jwt),
                                ("noise/other", "x")])
        cred, why = read_cred("cursor")
        check("cursor: token read from ItemTable", bool(cred), why or "")
        check("cursor: accessToken matches fixture",
              bool(cred) and cred["cursorAuth"]["accessToken"] == jwt)
        check("cursor: userId derived from sub claim",
              bool(cred) and cred["cursorAuth"]["userId"] == "user-abc123")

        # Copilot: readable apps.json
        cop_dir = os.path.join(localapp, "github-copilot")
        os.makedirs(cop_dir, exist_ok=True)
        with open(os.path.join(cop_dir, "apps.json"), "w", encoding="utf-8") as f:
            json.dump({"github.com:Iv1.deadbeef":
                       {"user": "octocat", "oauth_token": "gho_FIXTURE"}}, f)
        cred, why = read_cred("copilot")
        check("copilot: oauth_token read from apps.json", bool(cred), why or "")
        check("copilot: oauth_token matches fixture",
              bool(cred) and cred["copilotAuth"]["oauth_token"] == "gho_FIXTURE")
        check("copilot: host key preserved",
              bool(cred) and cred["copilotAuth"]["host"].startswith("github.com"))

        # Copilot: encrypted auth.db only
        os.remove(os.path.join(cop_dir, "apps.json"))
        authdb = os.path.join(cop_dir, "auth.db")
        con = _sqlite3.connect(authdb)
        con.execute("CREATE TABLE oauth_tokens (host TEXT, token_ciphertext BLOB)")
        con.execute("INSERT INTO oauth_tokens VALUES ('github.com', X'00ff00ff')")
        con.commit()
        con.close()
        cred, why = read_cred("copilot")
        check("copilot: encrypted db is not decrypted", cred is None)
        check("copilot: reported as encrypted, not missing",
              bool(why) and why.startswith(R_ENCRYPTED))
        check("copilot: reason names table and column",
              bool(why) and "oauth_tokens" in why and "token_ciphertext" in why)
        check("copilot: reason leaks no ciphertext",
              bool(why) and "00ff" not in why.lower())
        shutil.rmtree(cop_dir)
        cred, why = read_cred("copilot")
        check("copilot: absent install reports not found",
              cred is None and bool(why) and why.startswith(R_NOT_FOUND))

        # Windsurf
        ws_db = os.path.join(appdata, "Windsurf", "User", "globalStorage",
                             "state.vscdb")
        plan_doc = {"planName": "Pro", "promptsUsed": 123, "promptsLimit": 500}
        make_itemtable(ws_db, [
            ("windsurfAuthStatus", json.dumps({"apiKey": "ws_FIXTURE"})),
            ("windsurf.settings.cachedPlanInfo", json.dumps(plan_doc)),
            ("codeium.windsurf", json.dumps({"email": "dev@example.invalid"})),
        ])
        cred, why = read_cred("windsurf")
        check("windsurf: apiKey read", bool(cred), why or "")
        check("windsurf: apiKey matches fixture",
              bool(cred) and cred["windsurfAuth"]["apiKey"] == "ws_FIXTURE")
        check("windsurf: email extracted",
              bool(cred) and cred["windsurfAuth"].get("email")
              == "dev@example.invalid")
        plan = windsurf_cached_plan()
        check("windsurf: cached plan read", plan.get("ok") is True,
              plan.get("reason") or "")
        check("windsurf: cached plan content matches",
              plan.get("plan") == plan_doc)
        check("windsurf: cached plan carries a write timestamp",
              isinstance(plan.get("written_at"), float)
              and bool(plan.get("written_at_iso")))
        check("windsurf: cached plan reports its age",
              isinstance(plan.get("age_seconds"), float)
              and plan["age_seconds"] >= 0)

        # Devin
        dev_dir = os.path.join(appdata, "devin")
        os.makedirs(dev_dir, exist_ok=True)
        with open(os.path.join(dev_dir, "credentials.toml"), "w",
                  encoding="utf-8") as f:
            f.write('windsurf_api_key = "devin_FIXTURE"\n'
                    'api_server_url = "https://api.devin.example.invalid"\n')
        cred, why = read_cred("devin")
        check("devin: windsurf_api_key read from TOML", bool(cred), why or "")
        check("devin: api_key matches fixture",
              bool(cred) and cred["devinAuth"]["api_key"] == "devin_FIXTURE")
        check("devin: api_server_url carried through",
              bool(cred) and cred["devinAuth"].get("api_server_url", "")
              .endswith("invalid"))

        # Kimi
        kimi_dir = os.path.join(home, ".kimi-code")
        os.makedirs(os.path.join(kimi_dir, "credentials"), exist_ok=True)
        with open(os.path.join(kimi_dir, "credentials", "kimi-code.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"access_token": "kimi_FIXTURE", "refresh_token": "r"}, f)
        with open(os.path.join(kimi_dir, "device_id"), "w",
                  encoding="utf-8") as f:
            f.write("device-fixture-0001\n")
        cred, why = read_cred("kimi")
        check("kimi: credentials json read", bool(cred), why or "")
        check("kimi: access_token preserved",
              bool(cred)
              and cred["kimiAuth"]["credentials"]["access_token"] == "kimi_FIXTURE")
        check("kimi: device_id read and stripped",
              bool(cred) and cred["kimiAuth"]["device_id"] == "device-fixture-0001")

        # Cline: absent, then supplied by hand
        cred, why = read_cred("cline")
        check("cline: absent key explained honestly",
              cred is None and bool(why) and "CLINE_API_KEY" in why
              and "encrypted secret storage" in why)
        os.environ["CLINE_API_KEY"] = "cline_FIXTURE"
        cred, why = read_cred("cline")
        check("cline: env key picked up",
              bool(cred) and cred["clineAuth"]["apiKey"] == "cline_FIXTURE")
        os.environ.pop("CLINE_API_KEY", None)

        # Totality and malformed input
        cred, why = read_cred("no-such-vendor")
        check("unknown slug returns a reason, not an exception",
              cred is None and bool(why) and why.startswith(R_UNKNOWN_PROVIDER))
        cred, why = read_cred(None)
        check("None slug is total", cred is None and bool(why))
        check("cred_locations on unknown slug is empty",
              cred_locations("no-such-vendor") == [])
        bad = os.path.join(appdata, "Cursor", "User", "globalStorage",
                           "state.vscdb")
        with open(bad, "wb") as f:
            f.write(b"this is not a sqlite database at all")
        cred, why = read_cred("cursor")
        check("corrupt sqlite reports unreadable, not a crash",
              cred is None and bool(why) and why.startswith(R_UNREADABLE))

        # Read-only guarantee: no journal, no new file, no mtime change.
        # The corrupt fixture above left non-SQLite bytes at this path, and
        # sqlite3 refuses to create a table inside a file it cannot recognise,
        # so the fixture has to be removed before a real database is rebuilt
        # in its place.
        os.remove(bad)
        make_itemtable(bad, [("cursorAuth/accessToken", jwt)])
        before = (os.path.getmtime(bad), os.path.getsize(bad))
        listing_before = sorted(os.listdir(os.path.dirname(bad)))
        for _ in range(3):
            read_cred("cursor")
        after = (os.path.getmtime(bad), os.path.getsize(bad))
        check("sqlite read leaves mtime and size untouched", before == after)
        check("sqlite read leaves no journal or wal beside the db",
              sorted(os.listdir(os.path.dirname(bad))) == listing_before)

        # Aging out a stale CLI credential. Built directly against
        # _cli_candidate so the fixture never has to impersonate a real home
        # directory, and asserted on verdicts and booleans only -- no value
        # from either document is read or compared.
        cli_dir = os.path.join(root, "cli")
        os.makedirs(cli_dir, exist_ok=True)
        fresh_p = os.path.join(cli_dir, "fresh.json")
        dead_p = os.path.join(cli_dir, "dead.json")
        with open(fresh_p, "w", encoding="utf-8") as f:
            json.dump({"claudeAiOauth": {
                "accessToken": "FIXTURE", "refreshToken": "FIXTURE",
                "expiresAt": int((time.time() + 3600) * 1000)}}, f)
        with open(dead_p, "w", encoding="utf-8") as f:
            # The exact shape a logged-out Claude CLI leaves behind: the file
            # survives, the expiry is zero and the refresh key is gone.
            json.dump({"claudeAiOauth": {"accessToken": "",
                                         "expiresAt": 0}}, f)
        c_fresh, _ = _cli_candidate("claude", fresh_p)
        c_dead, _ = _cli_candidate("claude", dead_p)
        c_gone, _ = _cli_candidate("claude", os.path.join(cli_dir, "nope.json"))
        c_offline, _ = _cli_candidate(
            "claude", os.path.join(root, "no-such-home", ".claude", "c.json"))
        check("cli: a current credential classifies live",
              c_fresh["state"] == CAND_LIVE, c_fresh.get("reason") or "")
        check("cli: a gutted credential classifies stale",
              c_dead["state"] == CAND_STALE)
        check("cli: stale verdict notes the missing refresh key",
              c_dead["has_refresh_key"] is False)
        check("cli: stale reason is not the expired-login wording",
              (c_dead["reason"] or "").startswith(R_STALE)
              and "re-login" in (c_dead["reason"] or ""))
        check("cli: an absent file in a reachable dir reads as absent",
              c_gone["state"] == CAND_ABSENT)
        check("cli: an out-of-reach home is unreachable, not absent",
              c_offline["state"] == CAND_UNREACHABLE)
        check("cli: neither absence nor unreachability is ever stale",
              CAND_STALE not in (c_gone["state"], c_offline["state"]))
        check("cli: stale and unreadable are different reason prefixes",
              R_STALE != R_UNREADABLE)

        # A scan must cover every slug and must be redactable.
        scan = scan_all(include_creds=False)
        check("scan covers every provider slug",
              set(scan["providers"]) == set(provider_slugs()))
        check("scan with include_creds=False carries no cred objects",
              all("cred" not in r for r in scan["providers"].values()))
        blob = json.dumps(scan)
        check("redacted scan contains no fixture secret",
              all(s not in blob for s in ("gho_FIXTURE", "ws_FIXTURE",
                                          "devin_FIXTURE", "kimi_FIXTURE",
                                          jwt)))
        scan_full = scan_all(include_creds=True)
        check("scan with include_creds=True finds the fixture vendors",
              {"cursor", "windsurf", "devin", "kimi"}.issubset(
                  set(scan_full["found"])))

        print("\n  %d passed, %d failed" % (len(ok), len(fail)))
        if fail:
            print("  FAILED: " + ", ".join(fail))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            shutil.rmtree(root, ignore_errors=True)
        except Exception:
            pass
    return not fail


def _report_real_machine():
    """Read-only inventory of this machine. Paths, existence, sizes only."""
    print("\n=== real machine, read-only scan ===")
    scan = scan_all(include_creds=False)
    print("  generated_at: %s" % scan["generated_at"])
    for slug in provider_slugs():
        rec = scan["providers"][slug]
        print("\n  [%s] %s" % ("FOUND " if rec["found"] else "absent", slug))
        if not rec["found"]:
            # Reasons are built from paths and key names only, which is what
            # makes it safe to print one verbatim on a public repository.
            print("      reason: %s" % rec["reason"])
        for loc in rec["locations"]:
            bits = ["kind=%s" % loc["kind"], "confidence=%s" % loc["confidence"]]
            if loc.get("key"):
                bits.append("key=%s" % loc["key"])
            if loc.get("size") is not None:
                bits.append("size=%s" % loc["size"])
            if loc.get("mtime_iso"):
                bits.append("mtime=%s" % loc["mtime_iso"])
            print("      %-7s %s" % ("exists" if loc["exists"] else "missing",
                                     loc["path"]))
            print("              %s" % " ".join(bits))
    print("\n  found:   %s" % (", ".join(scan["found"]) or "(none)"))
    print("  missing: %s" % (", ".join(scan["missing"]) or "(none)"))
    plan = windsurf_cached_plan()
    if plan.get("ok"):
        age = plan.get("age_seconds") or 0
        print("  windsurf cached plan: written %s (%.1f hours ago), %d top-level "
              "fields" % (plan.get("written_at_iso"), age / 3600.0,
                          len(plan.get("plan") or {})))
    else:
        print("  windsurf cached plan: %s" % plan.get("reason"))


if __name__ == "__main__":
    good = _selftest()
    _report_real_machine()
    raise SystemExit(0 if good else 1)
