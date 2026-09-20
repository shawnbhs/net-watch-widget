"""Multi-account credential vault for the AI usage rows.

The widget used to read exactly two credential files, both hardcoded: one
Claude login and one Codex login. That is fine until a user has a personal
Claude account and a work one, or two vendors' CLIs installed side by side --
at which point the only way to see the other account's quota is to log the
first one out. This module is the registry that removes that limit: accounts
are named, stored once, and addressed by a stable id, so the UI can list them
and poll whichever one was clicked.

Why a separate file rather than the CLIs' own credential files:

  * A CLI stores one login. Ours has to store several, including several for
    the same provider, which no CLI credential format has a slot for.
  * The CLI files are owned by the CLI. Writing rotated refresh tokens back
    into them is already done (see core._write_json_atomic) but doing it for
    accounts the CLI is not currently logged into would fight the tool.
  * The CLI copies live wherever the CLI was installed -- for WSL installs
    that is behind the wsl.localhost share, which vanishes whenever the distro
    wedges. A vault in %APPDATA% is readable whether or not WSL is up. That
    unreadability is not a theoretical concern: it is exactly the failure that
    made an earlier version report a healthy login as expired.

Durability rules this file obeys, because it holds live logins:

  * Every write is atomic (temp file, 0600, flush, fsync, os.replace). A
    half-written vault logs the user out of every account at once.
  * No .bak sidecar. os.replace is already atomic, so a backup copy buys
    nothing and costs a permanent second copy of long-lived refresh tokens at
    rest -- a mistake this codebase has made before.
  * A corrupt or truncated vault yields an empty store, never an exception.
    Losing the whole widget is worse than losing the account list, and the
    account list can be rebuilt by importing from the CLIs again.
  * Nothing here logs, prints or raises a token value. Ids, providers,
    labels and expiry timestamps are safe to surface; secrets are not.
"""

import datetime as _dt
import json
import os
import threading
import uuid

# Encryption at rest is optional at import time on purpose. aisecrets wraps
# the vault document with DPAPI so a copy of the file lifted off the machine
# is inert, but a widget that refuses to start because that one module failed
# to import would cost the user every account list and every usage row for the
# sake of a storage property. When the import fails we fall back to the
# plaintext document this file has always written, which is exactly what an
# older build on the same machine already has on disk.
try:
    import aisecrets as _secrets
except Exception:
    _secrets = None

VERSION = 1

# One lock for the whole vault. Reads are cheap and writes are rare, so a
# single mutex costs nothing and removes any chance of two callers (the poll
# thread refreshing a token, the UI adding an account) racing on os.replace.
_lock = threading.RLock()


def _expand(path):
    """Expand both '~' and %ENVVAR% so either platform's style works.

    Mirrors core._expand deliberately: paths reach this module from the same
    .env file that feeds core, and two different expansion rules for one
    setting would be a bug waiting to happen.
    """
    return os.path.expanduser(os.path.expandvars(path))


def _default_dir():
    """%APPDATA%\\net-watch-ui on Windows, ~/.config/net-watch-ui elsewhere.

    APPDATA is roaming, which is the right choice here: the vault is small,
    it is per-user configuration, and a user who roams a profile between
    machines almost certainly wants their account list to come along.
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(_expand(appdata), "net-watch-ui")
    return os.path.join(os.path.expanduser("~"), ".config", "net-watch-ui")


def accounts_file():
    """Absolute path of the vault.

    AI_ACCOUNTS_FILE overrides it outright. Tests must set that rather than
    poke at the default, and the override is also how a user keeps the vault
    on an encrypted volume instead of in the roaming profile.
    """
    override = os.environ.get("AI_ACCOUNTS_FILE")
    if override:
        return _expand(override)
    return os.path.join(_default_dir(), "accounts.json")


def _empty_doc():
    return {"version": VERSION, "accounts": []}


def _coerce(doc):
    """Force any parsed JSON into the documented shape.

    Version is written from day one so a later format change can migrate
    rather than guess. An unknown (future) version is read best-effort rather
    than discarded: a newer build having touched the file is not a reason to
    throw away the user's logins.
    """
    if not isinstance(doc, dict):
        return _empty_doc()
    accounts = doc.get("accounts")
    if not isinstance(accounts, list):
        accounts = []
    clean = [a for a in accounts if isinstance(a, dict) and a.get("id")]
    ver = doc.get("version")
    if not isinstance(ver, int):
        ver = VERSION
    return {"version": ver, "accounts": clean}


def load():
    """Whole vault document. Never raises.

    Any failure at all -- missing file, bad permissions, truncated JSON, a
    directory where the file should be, a protected blob this machine cannot
    unwrap -- yields an empty store. The caller gets a usable widget with no
    accounts instead of a traceback.

    Reads go through aisecrets because the document on disk is now a protected
    blob rather than bare JSON. Migration needs no special case here: that
    module hands a legacy unwrapped document back verbatim, so a vault written
    by an older build loads exactly as it always did and is upgraded to a
    protected blob by the next save() -- which, on the credential path, is the
    very next token rotation. The plaintext branch below is only reached when
    aisecrets could not be imported at all.
    """
    path = accounts_file()
    with _lock:
        if _secrets is not None:
            try:
                raw = _secrets.read_secret(path)
            except Exception:
                # read_secret documents that it never raises, but a vault that
                # throws on read would take the widget down at logon, so the
                # guarantee is enforced here as well as promised there.
                raw = None
            if raw is None:
                return _empty_doc()
            try:
                return _coerce(json.loads(raw.decode("utf-8")))
            except Exception:
                return _empty_doc()
        try:
            with open(path, "r", encoding="utf-8") as f:
                return _coerce(json.load(f))
        except Exception:
            return _empty_doc()


def _readback_ok(doc):
    """Re-read the vault and confirm it really holds what save() just wrote.

    core._mark_migrated already reasons this way about a boolean migration
    flag: a write that is assumed to have stuck and did not is the worst
    outcome available. The credential path deserves more of that care, not
    less. Both providers invalidate the old refresh token the instant they
    issue a new one, so a save() that returns True on a write that did not
    land tells the caller it is safe to go on using a credential that exists
    nowhere but in memory -- and the account is gone at the next process start.

    The comparison is on ids and credential objects because those are the only
    fields whose loss is unrecoverable. Nothing is logged or raised from here:
    the comparison operands are secrets.
    """
    try:
        got = load()
    except Exception:
        return False
    if got.get("version") != doc.get("version"):
        return False
    stored = got.get("accounts") or []
    wanted = doc.get("accounts") or []
    if len(stored) != len(wanted):
        return False
    for a, b in zip(stored, wanted):
        if a.get("id") != b.get("id") or a.get("cred") != b.get("cred"):
            return False
    return True


def save(doc):
    """Atomically replace the vault. Returns True only when the document is
    provably on disk, False on every failure.

    This used to return None unconditionally, which made a full disk, an
    antivirus lock on the file or an unavailable roaming profile completely
    indistinguishable from a good write. That is not a cosmetic defect on this
    path: update_cred() is called immediately after a token rotation, at which
    point the provider has already killed the old refresh token, so a write
    that silently did nothing leaves the vault holding a dead credential and
    the live one only in memory. The account is then unrecoverable at the next
    process start. Every exit below now reports honestly, and success is only
    claimed after the file has been read back.

    The temp file is created with os.open and an explicit 0600 mode rather
    than opened and chmod'ed afterwards, so the secret is never briefly
    world-readable between creation and the chmod. Any temp left over from an
    earlier interrupted write is removed first: on Windows a scanner can hold
    that file open long enough for the cleanup below to fail, and what is left
    behind is a complete second copy of every live credential sitting beside
    the vault indefinitely.
    """
    doc = _coerce(doc)
    path = accounts_file()
    with _lock:
        try:
            d = os.path.dirname(path)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
        except Exception:
            return False
        tmp = path + ".tmp"
        _drop_stale_tmp(tmp)
        try:
            payload = json.dumps(doc, indent=2).encode("utf-8")
        except Exception:
            return False

        if _secrets is not None:
            # write_secret performs the same temp/fsync/os.replace dance and
            # raises rather than returning on failure, so the honest answer
            # here is simply whether it came back.
            try:
                _secrets.write_secret(path, payload)
            except Exception:
                _drop_stale_tmp(tmp)
                return False
            return _readback_ok(doc)

        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                f = os.fdopen(fd, "wb")
            except Exception:
                os.close(fd)
                raise
            with f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                # Windows honours only the read-only bit here; failing to
                # tighten the mode is not a reason to lose the write.
                pass
        except Exception:
            # Leave no half-written temp behind holding a live token.
            _drop_stale_tmp(tmp)
            return False
        return _readback_ok(doc)


def _drop_stale_tmp(tmp):
    """Remove a leftover vault temp file, ignoring the case where it is gone.

    Called both before and after a write. Before, because an interrupted write
    leaves a file that holds every live credential in the vault and nothing
    else would ever clean it up; after, for the same reason on the failure
    path. A temp that cannot be removed -- a scanner still has it open -- is
    not worth failing the write over, but it is worth the second attempt.
    """
    try:
        os.remove(tmp)
    except OSError:
        pass


def list_accounts():
    """Raw stored account dicts, in registration order.

    Returned as stored, secrets included -- this is an in-process API, not
    something to hand to the renderer. The sidecar is expected to project a
    redacted view before anything crosses the stdio boundary.
    """
    return load()["accounts"]


def get_account(account_id):
    for a in list_accounts():
        if a.get("id") == account_id:
            return a
    return None


def _now_iso():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def add_account(provider, label, cred, source="manual", cred_path=None):
    """Register an account and return the stored dict, id included.

    The id is a uuid4 hex rather than provider+label, because labels are user
    text and get renamed; anything holding a reference (a pinned UI row, a
    poll schedule) must survive a rename.
    """
    acct = {
        "id": uuid.uuid4().hex,
        "provider": provider,
        "label": label or provider,
        "added_at": _now_iso(),
        "source": source if source in ("import", "manual") else "manual",
        "cred": cred,
    }
    if cred_path:
        acct["cred_path"] = cred_path
    with _lock:
        doc = load()
        doc["accounts"].append(acct)
        save(doc)
    return acct


def remove_account(account_id):
    with _lock:
        doc = load()
        before = len(doc["accounts"])
        doc["accounts"] = [a for a in doc["accounts"] if a.get("id") != account_id]
        if len(doc["accounts"]) == before:
            return False
        # The account was found, so the only remaining question is whether the
        # removal reached the disk; reporting True on a write that failed would
        # tell the UI to drop a row the vault still holds.
        return save(doc)


def update_cred(account_id, cred):
    """Write a refreshed credential back.

    This is the hot path after a token rotation, and the reason save() has to
    be atomic: both providers invalidate the old refresh token the moment they
    hand out a new one, so a write that only half lands destroys the login for
    good. The whole credential object is replaced, not merged, because the
    provider adapters return the complete rotated object by contract.

    Returns True only when the rotated credential is actually on disk. A False
    here means the caller is holding the only live copy of that account's
    login and must not go on using it as though it were safe -- see
    core._vault_store_cred, which retries once and then treats the account as
    failed rather than carrying on.
    """
    with _lock:
        doc = load()
        for a in doc["accounts"]:
            if a.get("id") == account_id:
                a["cred"] = cred
                a["cred_updated_at"] = _now_iso()
                return save(doc)
        return False


def rename_account(account_id, label):
    with _lock:
        doc = load()
        for a in doc["accounts"]:
            if a.get("id") == account_id:
                a["label"] = label or a.get("provider") or "account"
                return save(doc)
        return False


# ── importing what the CLIs already have ──────────────────────────────────────
# Nothing here is machine specific: locations come from the same semicolon
# separated .env variables core.py reads (CLAUDE_CRED_PATHS / CODEX_CRED_PATHS),
# plus the standard per-user default, plus -- on Windows only -- a best effort
# walk of the wsl.localhost share, because on this class of setup the CLI is
# very often installed inside a distro and never on the Windows PATH.

_CLI_DEFAULTS = {
    "claude": os.path.join(".claude", ".credentials.json"),
    "codex": os.path.join(".codex", "auth.json"),
}

_ENV_KEY = {"claude": "CLAUDE_CRED_PATHS", "codex": "CODEX_CRED_PATHS"}


def _env_paths(key):
    raw = os.environ.get(key, "") or ""
    return [_expand(p.strip()) for p in raw.split(";") if p.strip()]


def _wsl_homes():
    """Home directories visible through \\\\wsl.localhost, best effort.

    Enumerated rather than hardcoded: the distro name and the Linux username
    are both machine specific and must never appear in this repo. Every step
    is wrapped, because the share raising OSError the moment WSL is not
    running is the normal case, not an error worth reporting.
    """
    if os.name != "nt":
        return []
    root = r"\\wsl.localhost"
    out = []
    try:
        distros = os.listdir(root)
    except Exception:
        return []
    for distro in distros[:8]:
        home = os.path.join(root, distro, "home")
        try:
            users = os.listdir(home)
        except Exception:
            continue
        for u in users[:16]:
            out.append(os.path.join(home, u))
    return out


def cli_cred_paths(provider):
    """Candidate credential files for one provider, best first.

    Ordering matches core.cred_paths: an explicit .env entry outranks the
    default, duplicates are dropped so listing the default explicitly is
    harmless. Unlike core._pick_cred nothing is ranked or discarded here --
    import wants every distinct login it can find, not the freshest one.
    """
    tail = _CLI_DEFAULTS.get(provider)
    if not tail:
        return []
    cands = _env_paths(_ENV_KEY.get(provider, ""))
    cands.append(os.path.join(os.path.expanduser("~"), tail))
    for home in _wsl_homes():
        cands.append(os.path.join(home, tail))
    seen = {}
    for p in cands:
        seen.setdefault(os.path.normcase(p), p)
    return list(seen.values())


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cred_fingerprint(provider, cred):
    """Stable, non-secret identity for a credential.

    Deliberately NOT the token. Tokens rotate on every refresh, so
    fingerprinting on one would re-import the same account as a new row every
    time it renewed, and it would also mean carrying a secret through
    comparison code and log lines.

      claude: subscriptionType + expiresAt. Claude's credential file carries
              no account id at all; the subscription tier plus the exact
              expiry millisecond is what distinguishes two copies of two
              different logins, while two copies of the SAME login (the
              Windows profile and the WSL home) share both and collapse into
              one -- which is the behaviour we want.
      codex:  account_id, which is a real stable identifier.

    Returns None when the shape is unrecognisable; callers treat that as
    "cannot dedupe" and skip the file rather than importing a mystery.
    """
    if not isinstance(cred, dict):
        return None
    if provider == "claude":
        o = cred.get("claudeAiOauth")
        if not isinstance(o, dict):
            return None
        return "claude:%s:%s" % (o.get("subscriptionType") or "?",
                                 o.get("expiresAt") or 0)
    if provider == "codex":
        t = cred.get("tokens")
        if not isinstance(t, dict):
            return None
        acct = t.get("account_id")
        if not acct:
            return None
        return "codex:%s" % acct
    return None


def import_from_cli():
    """Register every CLI login found that is not registered already.

    Returns the list of newly added accounts (empty when everything on disk
    was already known). Existing rows are left completely alone: re-importing
    must never clobber a credential the widget has since refreshed past what
    the CLI has on disk.
    """
    new = []
    with _lock:
        doc = load()
        known = set()
        for a in doc["accounts"]:
            fp = cred_fingerprint(a.get("provider"), a.get("cred"))
            if fp:
                known.add(fp)
        for provider in ("claude", "codex"):
            for path in cli_cred_paths(provider):
                cred = _read_json(path)
                if not cred:
                    continue
                fp = cred_fingerprint(provider, cred)
                if not fp or fp in known:
                    continue
                known.add(fp)
                acct = {
                    "id": uuid.uuid4().hex,
                    "provider": provider,
                    "label": _import_label(provider, cred, doc["accounts"] + new),
                    "added_at": _now_iso(),
                    "source": "import",
                    "cred": cred,
                    "cred_path": path,
                }
                doc["accounts"].append(acct)
                new.append(acct)
        if new:
            save(doc)
    return new


def _import_label(provider, cred, existing):
    """A human-ish default name, unique within the vault.

    The credential files carry no display name (Claude has none at all, Codex
    only an opaque account id), so the honest default is the provider slug
    plus a counter. The user renames it to "work" from the UI; nothing here
    pretends to know which account is which.
    """
    base = provider
    used = {a.get("label") for a in existing}
    if base not in used:
        return base
    n = 2
    while "%s %d" % (base, n) in used:
        n += 1
    return "%s %d" % (base, n)
