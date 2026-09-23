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


# ── refusals to destroy data, kept apart from ordinary write failures ────────
# aisecrets.write_secret() raises rather than land a write on top of bytes it
# could not read, or write a blob that does not decrypt back to its input.
# Those two are not failures to retry; they are refusals, and they are the
# whole reason that module exists. A vault sealed for another user or another
# machine is still recoverable -- restore the profile, sign in as the original
# user, supply the old Windows password -- right up until something overwrites
# it, and there is deliberately no backup copy.
#
# They are re-exported here so callers do not have to import aisecrets, and so
# save() can keep them apart from the transient failures (a full disk, a
# scanner holding the file open, a roaming profile mid-reconnect) that it
# honestly reports as False. Flattening the two together is what made the
# refusal useless in practice: reported as a bare False it reads, upstream, as
# "try again in a moment", the retry can never succeed, and the user is left
# with an account that silently never persists and no message naming why.
#
# The fallback definitions below keep both names importable when aisecrets is
# missing or predates them. Nothing raises them in that case, so they simply
# never match and every path behaves exactly as it did before.
class VaultUnreadableError(Exception):
    """Local stand-in for aisecrets.VaultUnreadableError; see that module."""


class VaultVerificationError(Exception):
    """Local stand-in for aisecrets.VaultVerificationError; see that module."""


if _secrets is not None:
    VaultUnreadableError = getattr(_secrets, "VaultUnreadableError",
                                   VaultUnreadableError)
    VaultVerificationError = getattr(_secrets, "VaultVerificationError",
                                     VaultVerificationError)

# What a caller passes to `except`. A name rather than a tuple rebuilt at each
# call site, so adding a third refusal later reaches every caller at once.
VAULT_REFUSALS = (VaultUnreadableError, VaultVerificationError)


def refusal_reason(exc):
    """A short, displayable reason for a vault refusal, or None.

    Doubles as the classifier: None means "not a refusal", so a caller can ask
    one question instead of importing the exception types and matching on them.

    Deliberately not str(exc). Those messages name the vault path, which is
    right for a traceback and more than a status line in a widget needs, and a
    string built by a module this one only optionally imports is not something
    to forward verbatim to the UI. What the user needs is which of the two
    situations they are in, because the remedies differ: recover access to the
    existing vault, versus report a broken crypto backend. Neither string
    contains anything derived from the vault contents.
    """
    if isinstance(exc, VaultUnreadableError):
        return ("vault locked: this machine could not read the accounts "
                "already stored, so nothing was overwritten")
    if isinstance(exc, VaultVerificationError):
        return "vault write refused: the encrypted copy did not read back"
    return None


# Durable account identity lives in aiproviders: it is the module that already
# knows each vendor's credential shape, and it is the one that can read the
# Claude CLI's own state file to learn WHICH account a credential belongs to.
# Optional at import time for the same reason aisecrets is: a vault that
# refuses to load because a sibling module failed to import would cost the
# user every account row, and the pre-existing expiry-based scheme below is a
# complete, if drifting, fallback. When this is None every entry point in this
# file degrades to exactly the behaviour it had before durable identity
# existed -- no new failure mode, just the old one.
try:
    import aiproviders as _providers
except Exception:
    _providers = None

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

    This used to return exactly {"version", "accounts"} and drop every other
    top-level key on the floor -- harmless for stray junk, but it also ate
    core._mark_migrated()'s "imported_from_cli" stamp on the very next save(),
    which is why that stamp has never actually survived on a live vault (see
    the a3 report). The fix is an allowlist, not a blanket pass-through:
    unknown keys stay dropped (still no way for a corrupt or hostile document
    to smuggle an arbitrary key through the vault), but the handful of keys
    this codebase is known to use are preserved. "forgotten" is NOT one of
    them -- the durable per-account tombstone list is deliberately kept in a
    sibling file (see _tombstones_file()) rather than inside this document,
    precisely so that deleting or resetting the vault cannot also erase the
    record of what the user removed on purpose. imported_from_cli is kept
    here purely so core.py's existing stamp attempt starts working; it is not
    load-bearing for anything in this file.
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
    out = {"version": ver, "accounts": clean}
    if "imported_from_cli" in doc:
        out["imported_from_cli"] = doc["imported_from_cli"]
    return out


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
    provably on disk, False on a transient failure, and raises on a refusal.

    Three outcomes, not two, because two of them are not the same kind of bad:

      * True  -- the document was written and read back.
      * False -- the write did not happen for a reason that may well not
        happen again: a full disk, a scanner holding the file open, a roaming
        profile that has not reconnected. Retrying is sensible.
      * raises VaultUnreadableError / VaultVerificationError -- the write was
        REFUSED to avoid destroying credentials that are still recoverable.
        Retrying cannot help; a human has to act. Collapsing this into False
        is what turned a loud, deliberate refusal into a silent permanent
        failure, so it is deliberately not caught here.

    Callers that genuinely only care whether the vault changed can still write
    `except aiaccounts.VAULT_REFUSALS`; what they can no longer do is mistake a
    refusal for a transient failure by accident.

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
            # here is simply whether it came back -- with one exception.
            #
            # Its two refusals (see VAULT_REFUSALS above) mean the vault was
            # left alone on purpose because overwriting it would have
            # destroyed recoverable credentials. That is not a transient
            # failure and no retry can clear it, so it is re-raised with its
            # type intact instead of being flattened into the same False a
            # busy scanner produces. Everything else keeps the boolean.
            try:
                _secrets.write_secret(path, payload)
            except VAULT_REFUSALS:
                _drop_stale_tmp(tmp)
                raise
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
    # Same stamp the import path writes, so a hand-added row and an imported
    # one are indistinguishable to every duplicate and tombstone check below.
    _stamp_fingerprints(acct, cred_fingerprint_candidates(provider, cred,
                                                          cred_path))
    with _lock:
        doc = load()
        doc["accounts"].append(acct)
        save(doc)
    return acct


def _stamp_fingerprints(entry, cands):
    """Record identity on a row or tombstone about to be written.

    `cred_fp` is the best available form (durable when one could be
    established); `fps` is every form it answers to, so the legacy string an
    older build would have written is still there and an older build reading
    this entry still finds what it expects under the key it expects.

    Deliberately a no-op when there is nothing to record: an entry with no
    recognisable credential shape gains no keys and stays byte-identical to
    what previous builds wrote.

    Nothing written here is a secret -- these are the same non-secret
    fingerprint strings already surfaced by list_forgotten().
    """
    if not isinstance(entry, dict) or not cands:
        return entry
    entry["cred_fp"] = cands[0]
    entry["fps"] = list(cands)
    return entry


def forget_account(account_id):
    """Remove an account from the vault AND make it durably un-re-importable.

    This is what "delete" has always needed to mean and never did: the old
    remove_account() (kept below as a compatibility alias, so no caller has
    to change) only ever deleted the vault row. Nothing stopped the next
    import_from_cli() from adding the exact same login straight back the
    moment its CLI credential file was still sitting on disk -- see the a4
    report, section 4. This function closes that gap by recording a
    tombstone for the account being removed before it reports success.

    The tombstone is written to a file beside the vault (see
    _tombstones_file()), never inside the vault document itself, so it
    survives the one scenario that matters most: the vault being wiped,
    reset, or reinstalled out from under the user. If the tombstone lived in
    doc["forgotten"] instead, deleting accounts.json -- which is exactly what
    an uninstall/reinstall or a corrupted-vault recovery does -- would
    silently un-delete every account the user ever removed. See the report
    for the full reasoning; core._mark_migrated's in-vault stamp is the
    cautionary tale this design deliberately avoids repeating.

    Returns True only once the vault row is confirmed gone AND the tombstone
    is confirmed written; False on any failure of either half. A tombstone
    that silently failed to write would look like a successful delete right
    up until the next CLI import brought the account back.
    """
    with _lock:
        doc = load()
        target = None
        kept = []
        for a in doc["accounts"]:
            if a.get("id") == account_id and target is None:
                target = a
                continue
            kept.append(a)
        if target is None:
            return False
        doc["accounts"] = kept
        if not save(doc):
            return False
        # The vault write is authoritative and already confirmed; a failure
        # to persist the tombstone is reported rather than swallowed, since a
        # caller that ignores it deserves to know the delete is not durable.
        return _add_tombstone(target)


def remove_account(account_id):
    """Compatibility alias -- delete now always durably forgets.

    Every existing call site (sidecar.ai_account_remove -> this function)
    gets tombstone protection with no change on its end. New code should
    call forget_account() directly; this name is kept only so nothing else
    in the tree has to be touched to get the fix.
    """
    return forget_account(account_id)


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
    harmless. Nothing is ranked or discarded here, and that is deliberate
    even though import_from_cli() now does discard: the aging judgement
    needs to SEE every copy before it can say anything useful about any of
    them. A list that had already dropped the unreachable candidate could
    not tell "your login is out of reach" from "your login is dead", which
    is the distinction the whole exercise exists to preserve. So this
    function keeps returning everything and the caller decides.
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


def _legacy_fingerprint(provider, cred):
    """The expiry-based fingerprint this module shipped before durable ids.

    Kept, unchanged to the character, for two reasons. It is the fallback
    when aiproviders is unavailable, and -- far more important -- it is the
    exact string already written into vault rows and into the tombstone
    sidecar on every machine running an older build. Altering its shape by
    one character would orphan every one of those, which presents as the
    user's original complaint (a known account seen as new) plus a second
    one (an account he deliberately deleted coming back).

      claude: subscriptionType + expiresAt. expiresAt is rewritten on every
              token issue, so this is a timestamp wearing an identity's
              clothes -- the defect durable identity exists to fix.
      codex:  account_id, which was always a real stable identifier.
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


def cred_fingerprint(provider, cred, cred_path=None):
    """Stable, non-secret identity for a credential. Best form available.

    Deliberately NOT the token: tokens rotate on every refresh, so
    fingerprinting one would re-import the same account as a new row every
    time it renewed, and would carry a secret through comparison code and
    log lines.

    For claude this now prefers the account's real identifier -- read by
    aiproviders from the CLI's own local state file, no network -- and falls
    back to the legacy subscriptionType+expiresAt pair only when no identity
    can be established. That fallback is the thing that used to drift on
    every token issue, which is why callers that need to know what they got
    ask is_durable_fingerprint() rather than assuming.

    `cred_path` is optional and additive: the credential file's location is
    what lets aiproviders find the matching CLI state file. Omitting it is
    never wrong, it just means claude degrades to the legacy form -- so the
    existing two-argument callers in sidecar.py, core.py and ailogin.py keep
    working with no change and no behaviour surprise.

    Returns None when the shape is unrecognisable; callers treat that as
    "cannot dedupe" and skip the file rather than importing a mystery.
    """
    if _providers is not None:
        try:
            fp = _providers.cred_fingerprint(provider, cred, cred_path)
            if fp:
                return fp
        except Exception:
            pass
    return _legacy_fingerprint(provider, cred)


def is_durable_fingerprint(fp):
    """True when this fingerprint survives a token refresh.

    Answers from the stored string alone, so a caller holding only a value
    read back out of a vault row or a tombstone entry -- with no credential
    in hand -- can still ask.
    """
    if _providers is not None:
        try:
            return bool(_providers.is_durable_fingerprint(fp))
        except Exception:
            pass
    # Without the provider module only codex's account_id is durable; the
    # legacy claude form never was.
    return isinstance(fp, str) and fp.startswith("codex:")


def cred_fingerprint_candidates(provider, cred, cred_path=None):
    """Every fingerprint form this credential answers to, best first.

    This is the back-compatibility mechanism and the reason nothing in this
    file needs a migration pass. A claude credential whose account is known
    yields both ["claude:acct:<uuid>", "claude:<tier>:<expiresAt>"], so it
    matches a row or tombstone written by any build, before or after durable
    identity existed.

    The list is always a SUPERSET of what the old single-string scheme
    produced. That is the property that makes this change safe: anything
    that matched before still matches, so no existing row and no existing
    tombstone can be orphaned by it.
    """
    out = []
    if _providers is not None:
        try:
            for fp in _providers.cred_fingerprint_candidates(
                    provider, cred, cred_path) or []:
                if fp and fp not in out:
                    out.append(fp)
        except Exception:
            out = []
    legacy = _legacy_fingerprint(provider, cred)
    if legacy and legacy not in out:
        out.append(legacy)
    return out


def _stored_fingerprints(entry):
    """Fingerprints an existing vault row or tombstone entry was written with.

    Read back rather than recomputed, because the whole point is to honour
    what an OLDER build recorded. `fps` is the list newer writes leave; `fp`
    is the single string every build has always written. Both are consulted,
    so a row from any era keeps answering to its own recorded identity even
    if its credential file has since vanished and nothing can be recomputed
    from it at all.
    """
    out = []
    if not isinstance(entry, dict):
        return out
    for fp in (entry.get("fps") or []):
        if isinstance(fp, str) and fp and fp not in out:
            out.append(fp)
    one = entry.get("fp") or entry.get("cred_fp")
    if isinstance(one, str) and one and one not in out:
        out.append(one)
    return out


def _account_fingerprints(account):
    """Every fingerprint an existing account row answers to.

    The union of two sources, and it needs both:

      * recomputed from the row's live credential plus its cred_path, which
        is what picks up the durable id for a row written before durable
        identity existed -- no rewrite of the vault required;
      * whatever the row itself recorded, which is what keeps a row matching
        after its credential file has been deleted, moved, or logged out.
    """
    if not isinstance(account, dict):
        return []
    out = cred_fingerprint_candidates(account.get("provider"),
                                      account.get("cred"),
                                      account.get("cred_path"))
    for fp in _stored_fingerprints(account):
        if fp not in out:
            out.append(fp)
    return out


# ── credential aging, borrowed rather than re-derived ─────────────────────
# The policy that decides whether a credential copy is still renewable lives
# in aicredsources: it owns CRED_MAX_STALE, it knows what "past renewal"
# means in each vendor's file shape, and it already separates stale from
# unreadable from out-of-reach. None of that is re-implemented here. Two
# copies of one rule drift, and this codebase has the scar: the polling path
# (core._pick_cred) learned to age credentials out years before the
# discovery path did, and the discovery path simply never got the lesson.
#
# The import is deferred to call time, which is not a style preference.
# aicredsources imports THIS module for its Claude and Codex path knowledge,
# so a module-level import here would close the loop and the loser would be
# whichever module the process happened to load second: if aiaccounts wins
# that race, aicredsources receives a half-built module with no
# cli_cred_paths() on it yet, silently loses both CLI vendors, and the
# damage surfaces nowhere near its cause. By the time anything calls
# import_from_cli() both modules are fully constructed.
_credsources_cache = []


def _cred_sources():
    """aicredsources, or None when it is unavailable. Never raises.

    Optional for the same reason aisecrets and aiproviders are optional
    above: a vault that refused to import accounts because a sibling module
    failed to load would cost the user every login, which is a far worse
    outcome than the one this module is guarding against.
    """
    if not _credsources_cache:
        try:
            import aicredsources as _cs
        except Exception:
            _cs = None
        _credsources_cache.append(_cs)
    return _credsources_cache[0]


# Fallback verdict names, used only when aicredsources could not be imported
# at all. They are never compared against anything in that case, so their
# only job is to keep the code below total.
_CAND_STALE = "stale"


def cli_cred_states(provider):
    """Every candidate path for one provider, mapped to its verdict.

    A thin adapter over aicredsources.cli_cred_candidates(): that function is
    the public, value-free classification API, and it walks the same
    candidate list this module hands it, so the two views cannot disagree
    about which files exist.

    Keys are normcase'd because the two modules build their path strings
    independently and Windows casing differences would otherwise turn a
    verdict into a silent lookup miss -- which fails OPEN, back to the old
    import-everything behaviour, so it has to be prevented rather than
    tolerated.

    Returns {} when the classification is unavailable. A caller must read
    that as "no judgement was made", never as "everything is fine".
    """
    cs = _cred_sources()
    if cs is None:
        return {}
    out = {}
    try:
        for rec in cs.cli_cred_candidates(provider) or []:
            path = rec.get("path")
            if isinstance(path, str) and path:
                out[os.path.normcase(path)] = rec
    except Exception:
        return {}
    return out


def import_from_cli(report=False):
    """Register every CLI login found that is not registered already.

    Returns the list of newly added accounts (empty when everything on disk
    was already known). Existing rows are left completely alone: re-importing
    must never clobber a credential the widget has since refreshed past what
    the CLI has on disk.

    With report=True the return value is (new, skipped) instead, where
    skipped lists the credential copies that were found, read, and then
    deliberately NOT imported, each with the reason aicredsources gave. The
    default stays a bare list because core._migrate_from_cli, tools/aiacct.py
    and the repo's own suites all treat the result as one, and changing that
    shape underneath them would be a second bug in service of fixing the
    first.

    Why a stale copy must be refused rather than imported and left to fail
    later: this machine keeps every CLI login twice, once in the Windows
    profile and once in a WSL home, and a logged-out CLI does not delete its
    credential file -- it guts it, leaving a perfectly readable document with
    an expiry of zero and no refresh token. Importing that produces an
    account row that can never work, and the first poll against it comes back
    400 and is shown to the user as "login expired" for a login that is
    alive and well in the other copy. The refusal is therefore not tidiness;
    it is the difference between the user being told to restore a file and
    the user being sent off to redo a login that was never broken.

    Why skipping it silently would be just as wrong: an account the user can
    see in his CLI and cannot see in the widget, with no explanation, is
    indistinguishable from the widget being broken. So the reason travels
    out with the result, and it is aicredsources' own R_STALE wording --
    never the expired-login wording, because those two demand opposite
    actions.
    """
    new = []
    skipped = []
    with _lock:
        doc = load()
        # Keyed by provider so a claude fingerprint can never be compared
        # against a codex one, and holding EVERY form each existing row
        # answers to rather than a single string. Matching on the full
        # candidate set is what finally makes this check work for claude: the
        # legacy fingerprint contains expiresAt, which the CLI rewrites on
        # every silent token refresh, so the same account never equalled its
        # own stored value and got added again -- the reported bug.
        known = {}
        for a in doc["accounts"]:
            pid = a.get("provider")
            if not pid:
                continue
            bucket = known.setdefault(pid, set())
            for fp in _account_fingerprints(a):
                bucket.add(fp)
        for provider in ("claude", "codex"):
            # Verdicts for every copy, computed once per provider rather than
            # per path: the classification walks the same candidate list and
            # asking it repeatedly would re-stat the whole set for each file.
            states = cli_cred_states(provider)
            stale_state = getattr(_cred_sources(), "CAND_STALE", _CAND_STALE)
            for path in cli_cred_paths(provider):
                cred = _read_json(path)
                if not cred:
                    continue
                verdict = states.get(os.path.normcase(path)) or {}
                if verdict.get("state") == stale_state:
                    # Past renewal. Refused BEFORE the dedupe bookkeeping
                    # below, and the order is load bearing: a stale copy that
                    # reached bucket.update() would register its own
                    # fingerprints as "known", and the live copy of the very
                    # same account -- which on this machine is listed second,
                    # behind the Windows profile -- would then be discarded as
                    # a duplicate of the corpse. The account would exist in
                    # the vault holding the one credential that cannot be
                    # refreshed.
                    skipped.append({
                        "provider": provider,
                        "path": path,
                        "state": verdict.get("state"),
                        "reason": verdict.get("reason") or (
                            "%s is past the refresh-token lifetime, so it "
                            "cannot be renewed and only a real re-login "
                            "revives it" % path),
                    })
                    continue
                # Everything else is admitted, including a copy whose age
                # could not be determined. "I do not recognise this shape"
                # must never graduate into "this login is dead": the day a
                # vendor changes its credential format, the honest failure is
                # importing an account we cannot age, not emptying the user's
                # vault.
                cands = cred_fingerprint_candidates(provider, cred, path)
                if not cands:
                    continue
                bucket = known.setdefault(provider, set())
                if bucket.intersection(cands):
                    continue
                if is_tombstoned(provider, cred, path):
                    # The user removed this account on purpose (or the same
                    # file path was tombstoned) -- see forget_account(). A
                    # credential file still sitting on disk is not consent
                    # to re-add it; the user opts back in via
                    # allow_reimport(), not by us re-discovering the file.
                    # tombstone_match() has the reason if a caller wants to
                    # distinguish "this exact account" from "this path".
                    continue
                bucket.update(cands)
                acct = {
                    "id": uuid.uuid4().hex,
                    "provider": provider,
                    "label": _import_label(provider, cred, doc["accounts"] + new),
                    "added_at": _now_iso(),
                    "source": "import",
                    "cred": cred,
                    "cred_path": path,
                }
                # Record the identity this row was admitted under. Not load
                # bearing -- _account_fingerprints() recomputes it from the
                # credential anyway -- but it is what keeps the row matching
                # once its credential file is gone, and it is a non-secret
                # string, so writing it costs nothing and buys durability.
                _stamp_fingerprints(acct, cands)
                doc["accounts"].append(acct)
                new.append(acct)
        if new:
            save(doc)
    return (new, skipped) if report else new


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


# ── durable "do not re-import" tombstones ─────────────────────────────────
# A per-account record of every deliberate removal, so import_from_cli() (and
# any future caller -- core._import_sweep, the manual re-add flow) can refuse
# to resurrect what the user deleted. See forget_account() above for why this
# is a sibling file rather than a key inside the vault document: the vault is
# exactly what an uninstall, reset, or corruption-recovery path clears, and
# that is precisely the moment this record has to keep working.
#
# Public API for other modules (core.py, sidecar.py, the UI):
#   is_tombstoned(provider, cred, cred_path=None) -> bool
#       Call before adding any account sourced from a CLI credential file.
#   tombstone_match(provider, cred, cred_path=None) -> dict | None
#       The same decision with its reason: "durable-fp" (the account's own
#       identifier -- certain), "legacy-fp" (the pre-durable expiry pair),
#       or "cred-path" (only the file location matched, so this may be a
#       different login that moved into the same file).
#   list_forgotten() -> [{"provider", "fp", "cred_path", "removed_at"}, ...]
#       Safe to surface in the UI as "previously removed" rows -- no secrets.
#   allow_reimport(provider, cred_path=None, fp=None) -> int
#       Undo. Clears matching tombstones (or every tombstone for that
#       provider if both cred_path and fp are omitted) so the next
#       import_from_cli() is free to add the account again. Returns how many
#       entries were cleared.


def _tombstones_file():
    """Path of the tombstone store, always beside the vault, never in it.

    Built from accounts_file() so AI_ACCOUNTS_FILE (the required override for
    every test and any alternate-vault-location setup) redirects this too --
    a test that only patches the vault path must not still write into the
    real %APPDATA% tombstone file.
    """
    return accounts_file() + ".forgotten"


def _load_tombstones():
    """Every tombstone on disk. Never raises; unreadable/corrupt -> empty.

    Same posture as load(): a widget that refuses to start because this
    sidecar file is damaged is a worse outcome than briefly forgetting which
    accounts were forgotten. Entries missing both match keys are dropped on
    load -- they can never match anything in is_tombstoned() and are not
    worth carrying forward.
    """
    path = _tombstones_file()
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return []
    if not isinstance(doc, dict):
        return []
    items = doc.get("forgotten")
    if not isinstance(items, list):
        return []
    return [
        t for t in items
        if isinstance(t, dict) and t.get("provider") and (t.get("fp") or t.get("cred_path"))
    ]


def _save_tombstones(items):
    """Atomic write of the tombstone list. Mirrors save()'s discipline.

    No secrets ever land in this file -- entries are provider name,
    non-secret fingerprint string, normalised path, and a timestamp -- so the
    0600 mode here is belt-and-braces consistency with the vault rather than
    a hard requirement.
    """
    path = _tombstones_file()
    tmp = path + ".tmp"
    _drop_stale_tmp(tmp)
    try:
        payload = json.dumps({"version": VERSION, "forgotten": items}, indent=2).encode("utf-8")
    except Exception:
        return False
    try:
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
    except Exception:
        return False
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
            pass
    except Exception:
        _drop_stale_tmp(tmp)
        return False
    return True


def _add_tombstone(account):
    """Record one removed account. Called only from forget_account().

    Skips accounts with neither a fingerprint nor a cred_path: those are
    manually-added rows with nothing a CLI import could ever match, so there
    is nothing useful to tombstone and no reason to grow the file forever
    with dead entries.
    """
    provider = account.get("provider")
    if not provider:
        return True
    cred_path = account.get("cred_path")
    cands = _account_fingerprints(account)
    fp = cands[0] if cands else None
    if not fp and not cred_path:
        return True
    entry = {
        "provider": provider,
        # The single-string key every build has always written, now carrying
        # the DURABLE form when one exists -- so this tombstone keeps
        # blocking after the CLI's next silent token refresh instead of
        # going stale the way the expiry-based value did.
        "fp": fp,
        "cred_path": os.path.normcase(cred_path) if cred_path else None,
        "removed_at": _now_iso(),
    }
    # Both forms, so an older build reading this file (which only looks at
    # "fp") and a newer one both recognise the account.
    _stamp_fingerprints(entry, cands)
    entry["fp"] = fp
    with _lock:
        items = _load_tombstones()
        items.append(entry)
        return _save_tombstones(items)


def tombstone_match(provider, cred, cred_path=None):
    """Why (or whether) this credential is blocked. None when it is not.

    Returns {"reason", "fp", "cred_path", "durable", "removed_at"} for the
    best matching tombstone, best meaning: a durable fingerprint match beats
    a legacy fingerprint match beats a path match. The whole list is scanned
    rather than returning the first hit, because the ranking is the useful
    part -- "we know this is the same account" and "something else once
    lived at this path" deserve different words in a UI and different
    confidence in a log line.

    Three match keys, in descending order of trust:

      * a DURABLE fingerprint -- the account's own identifier. Survives
        every token refresh, so it keeps matching forever. This is the key
        that finally works for claude, and it is why the path fallback is
        now a backstop rather than the load-bearing member it used to be.
      * a LEGACY fingerprint -- subscriptionType + expiresAt. Still matched,
        because tombstones written by older builds contain exactly this and
        dropping it would un-block every account removed before this change.
        It only matches while the token behind it has not rotated.
      * cred_path (normalised) -- retained deliberately. Not every
        credential can be identified: a CLI with no state file, a gutted
        logged-out copy, an older CLI build. For those the path is still the
        only stable thing there is, and losing that protection would
        resurrect exactly the accounts the user deleted.

    The path key keeps its known false-positive direction: a genuinely
    different login that later occupies the same file is blocked until the
    user calls allow_reimport(). That trade-off is unchanged and still
    conservative in the user's favour -- a false "still forgotten" costs one
    click, a silent resurrection costs trust that delete means delete. What
    HAS changed is that a durable match now reports itself as one, so the
    caller can tell a confident identity match from a cautious path guess.

    Contains no secrets: provider, non-secret fingerprint, normalised path.
    """
    if not provider:
        return None
    cands = cred_fingerprint_candidates(provider, cred, cred_path) \
        if cred is not None else []
    cand_set = set(cands)
    norm_path = os.path.normcase(cred_path) if cred_path else None
    best = None
    best_rank = -1
    for t in _load_tombstones():
        if t.get("provider") != provider:
            continue
        stored = _stored_fingerprints(t)
        hit = None
        for fp in stored:
            if fp in cand_set:
                # Prefer a durable hit even if a legacy one was seen first.
                if hit is None or (is_durable_fingerprint(fp)
                                   and not is_durable_fingerprint(hit)):
                    hit = fp
        if hit is not None:
            durable = is_durable_fingerprint(hit)
            rank = 3 if durable else 2
            reason = "durable-fp" if durable else "legacy-fp"
        elif norm_path and t.get("cred_path") and \
                t.get("cred_path") == norm_path:
            durable = False
            rank = 1
            reason = "cred-path"
        else:
            continue
        if rank > best_rank:
            best_rank = rank
            best = {"reason": reason, "fp": hit,
                    "cred_path": t.get("cred_path"), "durable": durable,
                    "removed_at": t.get("removed_at")}
        if best_rank == 3:
            break
    return best


def is_tombstoned(provider, cred, cred_path=None):
    """True if this credential must not be silently (re-)added.

    The boolean every existing caller (import_from_cli below, core._is_
    tombstoned, the UI) already uses. tombstone_match() is the same decision
    with the reason attached, for a caller that wants to say WHY rather than
    just refuse.
    """
    return tombstone_match(provider, cred, cred_path) is not None


def list_forgotten():
    """Every tombstone, safe to surface in a UI ("previously removed").

    Contains no secrets: provider name, the already-non-secret fingerprint
    string, a normalised file path, and a timestamp.
    """
    return list(_load_tombstones())


def allow_reimport(provider, cred_path=None, fp=None):
    """Undo. Clears tombstones so import_from_cli() may add the account again.

    This is the recovery path for requirement 4: a mistaken "forget" is not
    permanent. An entry is cleared when its provider matches and either any
    fingerprint it recorded (durable or legacy) or its cred_path matches what
    is passed here -- the same keys tombstone_match() blocks on, so "undo"
    reliably undoes exactly what "forget" blocked.

    Calling with only `provider` (both cred_path and fp omitted) clears every
    tombstone for that provider -- a deliberate blunt "let me start over"
    escape hatch, for a user who cannot tell which of several greyed rows in
    list_forgotten() corresponds to the account they want back.

    Intended caller: core.py / sidecar.py, wired to a UI action next to a
    "previously removed" row (see list_forgotten()). Returns the number of
    tombstone entries removed, so the caller can tell "nothing matched" (0)
    from "cleared".
    """
    if not provider:
        return 0
    norm_path = os.path.normcase(cred_path) if cred_path else None
    with _lock:
        items = _load_tombstones()
        keep = []
        removed = 0
        for t in items:
            if t.get("provider") != provider:
                keep.append(t)
                continue
            clear_all = fp is None and norm_path is None
            match = clear_all
            # Against every form the entry records, not just its "fp" key:
            # a tombstone written after durable identity carries both, and a
            # caller may hold either one. Undo has to clear exactly what
            # tombstone_match() blocks on, or a UI "let this back in" button
            # reports success and the account stays out.
            if fp is not None and fp in _stored_fingerprints(t):
                match = True
            if norm_path is not None and t.get("cred_path") == norm_path:
                match = True
            if match:
                removed += 1
            else:
                keep.append(t)
        if removed:
            _save_tombstones(keep)
        return removed
