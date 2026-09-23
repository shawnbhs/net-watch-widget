"""Encryption at rest for the credential vault.

The vault in aiaccounts.py holds live OAuth access and refresh tokens for paid
AI subscriptions. Until now it sat on disk as plain JSON under the roaming
profile, protected by nothing but filesystem permissions. On Windows that is
thinner cover than it sounds: every process running as the signed-in user can
read the user's own files, and a great many things run as the signed-in user.
A refresh token is long-lived, so a single read is worth a lot more to an
attacker than a session cookie would be. This module is the layer that wraps
the vault document before it is written and unwraps it after it is read.

Why DPAPI, and why through ctypes
---------------------------------

Windows exposes CryptProtectData and CryptUnprotectData in crypt32.dll. They
encrypt a blob with a key derived from the logged-on user's credentials and
managed entirely by the operating system. That last part is the reason this is
the correct primitive here rather than, say, a passphrase-derived AES key:

  * No key material has to exist in the repository, in the .env file, or in
    any config the user might paste into a bug report. The repository is
    public, so any scheme that needed a stored key would be a scheme with a
    published key.
  * No passphrase prompt. The widget starts unattended at logon and must be
    able to read its own vault with nobody sitting in front of it. A
    passphrase-based design would defeat the entire feature, because the
    widget has nobody to ask.
  * ctypes is in the standard library, so this costs no new dependency. The
    sidecar's only third-party dependency is psutil and that stays true.

The threat model, stated honestly
---------------------------------

DPAPI's user scope protects against exactly two things, and it is worth being
precise because overstating the guarantee is worse than offering none -- it
would encourage storing something here that deserves stronger protection:

  * Another user account on the same machine. A different user, including one
    with a different administrator profile, cannot decrypt this user's blob
    through the normal API, because the key is tied to their credentials, not
    to the box.
  * A stolen copy of the file. Lift accounts.json onto a USB stick or into a
    backup that leaves the machine and it is inert. Restoring it under a
    different user or on a different machine will not decrypt it.

It does NOT protect against:

  * Malware or any other code already running as this same user. Such code
    can simply call CryptUnprotectData itself, exactly as this module does,
    and get the plaintext back. DPAPI's user scope is a boundary between
    users, not a boundary inside one user's session.
  * An attacker who has the user's Windows password, since that is what the
    protection is ultimately derived from.
  * Anything at all once the plaintext is in this process's memory.

So the honest summary is: this raises the bar from "any process that can open
a file" to "code running as this user", and it makes an exfiltrated file
useless. That is a real and worthwhile improvement, and it is not the same
thing as a secret being safe.

Graceful degradation is a hard requirement
------------------------------------------

The widget also runs where DPAPI does not exist (a non-Windows host, a bare
CI container) or where the call can fail for reasons outside our control. A
hard failure there would mean the widget cannot read its own vault, which
logs the user out of every registered account at once. That is far worse than
the storage being plaintext, so when encryption is unavailable this module
still stores the data -- inside a clearly marked plaintext envelope.

The one rule that makes that acceptable is that the downgrade is never
silent. available() says no, backend_name() names the fallback out loud, and
is_protected() returns False for the resulting blob, so a diagnostic can tell
the user the truth. A UI that implied encryption while quietly writing
plaintext would be a genuine betrayal of the user's expectation, and is the
single failure mode this module is most careful to avoid.

Blob format
-----------

Every blob written here begins with a seven byte header:

    b"NWSEC" | format_version (1 byte) | scheme (1 byte) | payload...

The magic makes the file self-describing, the version byte is present from day
one so a future change of scheme can still recognise today's blobs instead of
mistaking them for corruption, and the scheme byte says how the payload was
produced. A file that carries no header at all is treated as a legacy
unwrapped document and returned unchanged, which is how the pre-encryption
plaintext vault keeps working across the upgrade.

Empty is not the same as unreadable
-----------------------------------

The read path reports three different unhappy outcomes and refuses to blur
them together, because a writer's correct response to each is different and
one of those responses is destructive:

  * The vault is absent or holds no bytes. Nothing to lose; a write may
    proceed and create it.
  * The vault exists and holds bytes that this process could not turn back
    into a document -- sealed for another user or another machine, truncated,
    corrupted, an unknown scheme, a version from a future build. Those bytes
    are the only copy of the user's credentials. A write here is irreversible
    deletion, and it must not happen by accident.
  * The bytes could not be fetched at all (a lock, a permission denial). The
    vault may be perfectly fine; nothing here can prove it either way, so it
    is treated with the same caution as unreadable.

The original shape of this module returned None for all three, which meant a
vault this machine cannot decrypt was indistinguishable from a fresh install:
the caller started empty, the next save wrote that empty document over the
ciphertext, and the credentials were gone with no backup and no recovery path
short of a manual re-login per account. read_secret_ex() exists to keep the
three apart, and write_secret() refuses -- loudly, by raising -- rather than
land a write on top of bytes it could not read. read_secret() keeps its old
collapse-to-None signature so existing callers are unaffected.
"""

import ctypes
import os
import sys

# Bumped only if the envelope itself changes shape. The scheme byte, not this,
# distinguishes DPAPI payloads from fallback payloads, so adding a third
# backend later does not require a version bump and does not invalidate any
# blob already on disk.
FORMAT_VERSION = 1

MAGIC = b"NWSEC"
HEADER_LEN = len(MAGIC) + 2

# Scheme byte values. These are on-disk constants: never reuse a number for a
# different meaning, because a blob written by an older build will still be
# sitting in a roaming profile somewhere.
SCHEME_DPAPI_USER = 1
SCHEME_PLAINTEXT = 2

BACKEND_DPAPI = "dpapi-user"
BACKEND_PLAINTEXT = "plaintext-fallback"

# Read outcomes, kept apart by read_secret_ex(). These are part of the module's
# API surface rather than internal detail because the whole point is that a
# caller can branch on them; a status a caller cannot see is a status that
# cannot protect anything.
STATUS_OK = "ok"
STATUS_ABSENT = "absent"
STATUS_UNREADABLE = "unreadable"
STATUS_ERROR = "error"


class VaultUnreadableError(Exception):
    """Raised instead of overwriting a vault whose contents could not be read.

    Failing loudly is the entire value of this exception. A silent success on
    this path converts a recoverable situation -- ciphertext the user could
    still open by restoring their profile, signing in as the original user, or
    supplying the old Windows password -- into permanent loss, since there is
    deliberately no backup copy of the vault and the credentials inside it
    cannot be regenerated without a manual re-login per account. A stall the
    user can act on is strictly better than a data-destroying success.
    """


class VaultVerificationError(Exception):
    """Raised when a freshly wrapped blob does not decrypt back to its input.

    Checked in memory, before anything touches the target file, so a blob that
    could never be reopened is never allowed to become the only copy.
    """

# Setting this environment variable to a truthy value forces the fallback path
# even on a working Windows host. It exists for the self-test and for anyone
# reproducing a report from a machine where DPAPI misbehaves; it is read once
# at import so a running widget cannot be flipped underneath itself.
DISABLE_ENV = "NET_WATCH_SECRETS_PLAINTEXT"

# Secondary entropy. This is not a secret and is not treated as one -- it is a
# fixed, published domain separator, so a blob produced by this widget cannot
# be handed to some other DPAPI consumer running as the same user and decrypted
# by accident. Changing this string would strand every existing blob, so it is
# frozen.
_ENTROPY = b"net-watch-widget/ai-credential-vault/v1"

_CRYPTPROTECT_UI_FORBIDDEN = 0x01
# CRYPTPROTECT_LOCAL_MACHINE (0x04) is deliberately never passed. Machine
# scope would let any account on the box decrypt the vault, which is precisely
# the boundary this module exists to draw.


class _DataBlob(ctypes.Structure):
    """The DATA_BLOB struct both crypt32 entry points take and return."""

    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _load_dpapi():
    """Resolve the crypt32/kernel32 entry points, or return None.

    Everything that can go wrong at load time -- wrong platform, a stripped
    system image, a ctypes restriction -- is funnelled into a None return so
    that the import of this module can never be the thing that stops the
    widget from starting.
    """
    if sys.platform != "win32":
        return None
    if os.environ.get(DISABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob_p = ctypes.POINTER(_DataBlob)
        void_p = ctypes.c_void_p
        for fn in (crypt32.CryptProtectData, crypt32.CryptUnprotectData):
            fn.restype = ctypes.c_int
            fn.argtypes = [blob_p, ctypes.c_wchar_p, blob_p, void_p, void_p,
                           ctypes.c_uint32, blob_p]
        kernel32.LocalFree.restype = void_p
        kernel32.LocalFree.argtypes = [void_p]
        return (crypt32, kernel32)
    except Exception:
        return None


_DPAPI = _load_dpapi()


def _in_blob(data):
    """Wrap bytes we own in a DATA_BLOB for the call's input side."""
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _take_out_blob(out):
    """Copy an output DATA_BLOB into Python bytes and free what DPAPI gave us.

    crypt32 allocates the output with LocalAlloc and hands us ownership. The
    widget calls through here on every poll of every account, so skipping the
    LocalFree would be a slow leak that only shows up after the machine has
    been left running for a week -- the exact class of bug that is miserable
    to diagnose later. The free happens in a finally block so it also runs if
    the copy raises.
    """
    try:
        if not out.pbData or out.cbData == 0:
            return b""
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        if out.pbData:
            _DPAPI[1].LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))
            out.pbData = ctypes.POINTER(ctypes.c_char)()
            out.cbData = 0


def available():
    """True when real encryption is usable in this process on this platform.

    This is a live probe, not just a platform check: the DLL resolving is not
    the same as the call succeeding, and a caller deciding what to tell the
    user deserves the stronger answer. The probe encrypts a tiny fixed
    constant, never anything from the vault.
    """
    if _DPAPI is None:
        return False
    try:
        return _dpapi_protect(b"probe") is not None
    except Exception:
        return False


def backend_name():
    """Short name of the active backend, for a diagnostic line.

    Returned rather than logged so the caller decides where it goes. The
    fallback name says "plaintext" in as many words, because the whole point
    of surfacing this is that the user can tell which of the two situations
    they are in.
    """
    return BACKEND_DPAPI if available() else BACKEND_PLAINTEXT


def _dpapi_protect(data):
    """Encrypt with the user scope, or return None if the call fails."""
    if _DPAPI is None:
        return None
    crypt32 = _DPAPI[0]
    blob_in, _keep_in = _in_blob(data)
    ent_in, _keep_ent = _in_blob(_ENTROPY)
    out = _DataBlob()
    try:
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, ctypes.byref(ent_in), None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
        )
    except Exception:
        return None
    if not ok:
        return None
    return _take_out_blob(out)


def _dpapi_unprotect(payload):
    """Decrypt a user-scope payload, or return None if it cannot be read.

    Returning None rather than raising is deliberate and load-bearing: a blob
    from another user, another machine, or a truncated file all land here, and
    the caller's correct response to every one of them is the same -- treat
    the vault as empty rather than crash at startup. Note that no exception
    text is constructed from the payload, so nothing derived from a secret can
    end up in a traceback.
    """
    if _DPAPI is None:
        return None
    crypt32 = _DPAPI[0]
    blob_in, _keep_in = _in_blob(payload)
    ent_in, _keep_ent = _in_blob(_ENTROPY)
    out = _DataBlob()
    try:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, ctypes.byref(ent_in), None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
        )
    except Exception:
        return None
    if not ok:
        return None
    return _take_out_blob(out)


def _parse_header(blob):
    """Return the scheme byte and payload, or (None, None) if unwrapped.

    (None, None) is not an error: it is how a legacy document announces
    itself, since the pre-encryption vault was a bare JSON file with no header
    at all.
    """
    if not isinstance(blob, (bytes, bytearray)):
        return (None, None)
    blob = bytes(blob)
    if len(blob) < HEADER_LEN or not blob.startswith(MAGIC):
        return (None, None)
    version = blob[len(MAGIC)]
    scheme = blob[len(MAGIC) + 1]
    if version != FORMAT_VERSION:
        # A blob from a future build. Refusing it is the only honest answer --
        # guessing at a format we do not know would risk handing back garbage
        # that looks like data.
        return (-1, b"")
    return (scheme, blob[HEADER_LEN:])


def _wrap(scheme, payload):
    return MAGIC + bytes([FORMAT_VERSION, scheme]) + payload


def protect(data_bytes):
    """Wrap bytes for storage: encrypted when possible, enveloped when not.

    Always returns a headered blob, so the writer never has to know which of
    the two paths it got. When DPAPI is unavailable the payload is the
    original bytes under the plaintext scheme byte, which is what keeps the
    widget working on a non-Windows host at the cost, clearly recorded in the
    blob itself, of no confidentiality.
    """
    if not isinstance(data_bytes, (bytes, bytearray)):
        raise TypeError("protect() takes bytes")
    data_bytes = bytes(data_bytes)
    sealed = _dpapi_protect(data_bytes)
    if sealed is None:
        return _wrap(SCHEME_PLAINTEXT, data_bytes)
    return _wrap(SCHEME_DPAPI_USER, sealed)


def unprotect(blob_bytes):
    """Unwrap a blob from any of the three shapes, or return None.

    The three shapes are a DPAPI blob, a plaintext-fallback blob, and a legacy
    unwrapped document written before this module existed. The last one is
    returned byte for byte unchanged, which is what makes the upgrade
    seamless: the user's existing accounts load on first run and get
    encrypted the next time anything writes the vault.
    """
    if blob_bytes is None:
        return None
    if not isinstance(blob_bytes, (bytes, bytearray)):
        return None
    blob_bytes = bytes(blob_bytes)
    scheme, payload = _parse_header(blob_bytes)
    if scheme is None:
        return blob_bytes
    if scheme == SCHEME_PLAINTEXT:
        return payload
    if scheme == SCHEME_DPAPI_USER:
        return _dpapi_unprotect(payload)
    # Unknown scheme, or a future format version. Nothing to do but report the
    # vault as unreadable and let the caller carry on with an empty store.
    return None


def is_protected(blob_bytes):
    """True only when the blob is genuinely encrypted.

    Both the plaintext envelope and a legacy unwrapped document return False,
    because from the user's point of view those are the same situation: the
    tokens are readable by anything that can open the file. blob_kind() is
    there when a diagnostic wants to tell the two apart.
    """
    scheme, _payload = _parse_header(blob_bytes)
    return scheme == SCHEME_DPAPI_USER


def blob_kind(blob_bytes):
    """Name which of the shapes a blob is, for diagnostics.

    One of "dpapi-user", "plaintext-fallback", "legacy-unwrapped" or
    "unknown". is_protected() answers the yes/no question a caller needs to
    gate behaviour on; this answers the question a user needs to understand
    what is on their disk.
    """
    scheme, _payload = _parse_header(blob_bytes)
    if scheme is None:
        return "legacy-unwrapped"
    if scheme == SCHEME_DPAPI_USER:
        return BACKEND_DPAPI
    if scheme == SCHEME_PLAINTEXT:
        return BACKEND_PLAINTEXT
    return "unknown"


def write_secret(path, data_bytes, allow_unreadable_overwrite=False):
    """Atomically write a protected blob to path.

    The sequence is the same one aiaccounts.py uses for the plaintext vault,
    for the same reasons: create a temp file in the target directory with 0600
    from the very first byte (never create it world-readable and tighten it
    afterwards, which leaves a window), write, flush, fsync so the bytes are
    really on the platter before the rename, then os.replace onto the target.
    os.replace is atomic, so a reader either sees the whole old file or the
    whole new one.

    There is deliberately no .bak sidecar. An earlier version of this codebase
    left a permanent backup copy of a credential file next to the original,
    which bought nothing -- os.replace is already atomic -- and cost a second
    copy of long-lived refresh tokens at rest, one that nothing ever cleaned
    up. On any failure the temp file is removed, so the directory is left
    exactly as it was found.

    Two refusals guard the replace, and both raise rather than return, because
    a caller that treats "written" as a boolean must not be able to mistake
    either of them for success:

      * If the file already at path could not be read -- it exists, it holds
        bytes, and they did not unwrap -- the write is refused with
        VaultUnreadableError. Those bytes are the only copy of whatever is
        inside them, there is no backup by design, and replacing them is not
        an operation anyone can undo. allow_unreadable_overwrite=True is the
        deliberate escape hatch for a caller that has established the user
        really does mean to discard the old vault; it is never the default.
      * If the freshly wrapped blob does not decrypt back to the input, the
        write is refused with VaultVerificationError. Checked in memory before
        the temp file is even created, so a readable vault is never replaced by
        a blob that cannot be reopened.

    Both checks run before anything is created on disk, which preserves the
    property that an interrupted write leaves the original byte for byte
    intact.
    """
    _existing, status = read_secret_ex(path)
    if status in (STATUS_UNREADABLE, STATUS_ERROR) and not allow_unreadable_overwrite:
        # The message names the path and the status only. Nothing derived from
        # the blob or from the data being written goes into it, so this cannot
        # leak credential material into a log or a traceback.
        raise VaultUnreadableError(
            "refusing to overwrite a vault whose current contents could not be "
            "read (status=%s): %s" % (status, path)
        )
    blob = protect(data_bytes)
    if unprotect(blob) != bytes(data_bytes):
        raise VaultVerificationError(
            "refusing to write a blob that does not decrypt back to its input: %s" % path
        )
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            # os.fdopen took ownership of the descriptor on success; if it
            # raised before that, close it here so a repeated write cannot
            # exhaust the process's descriptors.
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows honours only the read-only bit through chmod, so failing to
        # tighten the mode here is expected and is not a reason to lose a
        # write that already landed. The real cross-user protection on that
        # platform is the encryption, not the mode bits.
        pass
    return path


def read_secret_ex(path):
    """Read and unprotect a file, returning (data, status).

    status is one of STATUS_ABSENT, STATUS_UNREADABLE, STATUS_ERROR or
    STATUS_OK, and data holds bytes only in the STATUS_OK case. Like
    read_secret this never raises: a vault that throws on read would take the
    widget down at logon over a file the user can re-populate.

    STATUS_ABSENT covers both a missing file and a zero-length one, because a
    file with no bytes in it holds no credentials and is safe to replace.

    A blob that unwraps to zero bytes is STATUS_OK with b"", not ABSENT: the
    decryption genuinely succeeded, and the caller deciding whether a write is
    safe needs to know that the key worked.
    """
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except (FileNotFoundError, NotADirectoryError):
        return (None, STATUS_ABSENT)
    except OSError:
        # The file is there in some form but its bytes cannot be fetched -- a
        # scanner lock, a permission denial, a directory in its place. Nothing
        # here can prove it is disposable, so it is not reported as absent.
        return (None, STATUS_ERROR)
    if not blob:
        return (None, STATUS_ABSENT)
    try:
        data = unprotect(blob)
    except Exception:
        return (None, STATUS_UNREADABLE)
    if data is None:
        return (None, STATUS_UNREADABLE)
    return (data, STATUS_OK)


def read_secret(path):
    """Read and unprotect a file, returning bytes, or None.

    None covers every unhappy outcome -- no file yet, unreadable file, corrupt
    or foreign blob. This signature is kept exactly as it was because callers
    depend on it, but it is now the lossy view: anything that needs to tell an
    absent vault from one it could not open must call read_secret_ex(), and
    anything that is about to WRITE must, because on this path the difference
    is the difference between creating a vault and destroying one.
    """
    data, _status = read_secret_ex(path)
    return data


def readback_ok(path, expected_bytes):
    """True only when path now reads back as exactly expected_bytes.

    Offered here so a caller verifying its own write does not have to re-derive
    the read statuses. Stronger than comparing parsed fields in two ways: it is
    byte exact, and it returns False when the file that comes back is
    unreadable rather than treating an unopenable vault as a successful write.

    Nothing is logged or raised from here; both operands are secrets.
    """
    try:
        expected = bytes(expected_bytes)
    except Exception:
        return False
    data, status = read_secret_ex(path)
    return status == STATUS_OK and data == expected


def describe():
    """One short dict for the diagnostic panel. Contains no secret material."""
    ok = available()
    return {
        "backend": BACKEND_DPAPI if ok else BACKEND_PLAINTEXT,
        "encrypted": ok,
        "format_version": FORMAT_VERSION,
        "note": (
            "DPAPI user scope: another user on this machine, or a copy of the "
            "file taken elsewhere, cannot decrypt it. Code running as this "
            "user can."
        ) if ok else (
            "Encryption unavailable on this platform: credentials are stored "
            "in plaintext inside a marked envelope."
        ),
    }


if __name__ == "__main__":
    # Self-test. Everything touching the filesystem happens inside a fresh
    # temporary directory, so this can never go near a real vault. The
    # fallback half re-imports the module in a child process with the disable
    # switch set, because the backend is resolved once at import time.
    import json
    import shutil
    import subprocess
    import tempfile

    fails = []

    def check(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + (("  " + detail) if detail else ""))
        if not cond:
            fails.append(name)

    print("backend: %s  available=%s" % (backend_name(), available()))
    print("describe: %s" % json.dumps(describe(), sort_keys=True))

    print("\n[1] round trip")
    secret = json.dumps({"version": 1, "accounts": [{"id": "a1", "refresh": "TOKEN-SENTINEL"}]}).encode()
    blob = protect(secret)
    check("unprotect(protect(x)) == x", unprotect(blob) == secret)
    check("header magic present", blob.startswith(MAGIC))
    check("version byte is %d" % FORMAT_VERSION, blob[len(MAGIC)] == FORMAT_VERSION)
    check("blob_kind == %s" % blob_kind(blob), blob_kind(blob) in (BACKEND_DPAPI, BACKEND_PLAINTEXT))

    print("\n[2] ciphertext does not contain the plaintext")
    if available():
        check("sentinel absent from blob", b"TOKEN-SENTINEL" not in blob)
        check("is_protected true", is_protected(blob) is True)
        check("blob longer than input (real DPAPI blob)", len(blob) > len(secret))
    else:
        check("skipped: no DPAPI on this host", True, "(fallback host)")

    print("\n[3] legacy unwrapped JSON reads back unchanged")
    legacy = b'{"version": 1, "accounts": []}'
    check("unprotect(legacy) is identical", unprotect(legacy) == legacy)
    check("is_protected(legacy) is False", is_protected(legacy) is False)
    check("blob_kind(legacy) == legacy-unwrapped", blob_kind(legacy) == "legacy-unwrapped")

    print("\n[4] corrupt / foreign blobs return None, never raise")
    truncated = blob[: HEADER_LEN + 4]
    # Flip a byte deep inside the ciphertext rather than near its front. A
    # DPAPI blob begins with descriptor fields that the API will tolerate
    # being altered -- it reports success and still returns the plaintext --
    # so a bit flip at a low offset is not a valid corruption probe. The
    # integrity check covers the encrypted body, which is what this targets.
    flipped = bytearray(blob)
    if len(flipped) > HEADER_LEN + 16:
        flipped[len(flipped) - 12] ^= 0xFF
    cases = [
        ("truncated blob", bytes(truncated)),
        ("bit-flipped blob", bytes(flipped)),
        ("unknown scheme byte", MAGIC + bytes([FORMAT_VERSION, 99]) + b"xxxx"),
        ("future format version", MAGIC + bytes([FORMAT_VERSION + 7, 1]) + b"xxxx"),
        ("not bytes at all", 12345),
        ("None", None),
    ]
    for label, case in cases:
        try:
            got = unprotect(case)
            ok = got is None
        except Exception as exc:
            got, ok = type(exc).__name__, False
        if available() or label not in ("truncated blob", "bit-flipped blob"):
            check("%s -> None" % label, ok, "got=%r" % (got if not ok else None))
        else:
            check("%s (fallback host: payload returned)" % label, True)

    print("\n[5] write_secret / read_secret leave no temp or backup file")
    tmpdir = tempfile.mkdtemp(prefix="nwsec-selftest-")
    try:
        target = os.path.join(tmpdir, "accounts.json")
        write_secret(target, secret)
        after = sorted(os.listdir(tmpdir))
        check("directory holds exactly the target", after == ["accounts.json"], "listing=%r" % (after,))
        check("no .tmp leftover", not any(n.endswith(".tmp") for n in after))
        check("no .bak sidecar", not any(n.endswith(".bak") for n in after))
        check("read_secret round trip", read_secret(target) == secret)
        with open(target, "rb") as f:
            on_disk = f.read()
        check("on-disk blob is headered", on_disk.startswith(MAGIC))
        if available():
            check("on-disk bytes do not contain the sentinel", b"TOKEN-SENTINEL" not in on_disk)
        check("missing file -> None", read_secret(os.path.join(tmpdir, "nope.json")) is None)

        # A legacy plaintext vault sitting on disk must load, then be upgraded
        # in place by the next write. This is the whole migration story.
        legacy_path = os.path.join(tmpdir, "legacy.json")
        with open(legacy_path, "wb") as f:
            f.write(legacy)
        check("legacy file reads through read_secret", read_secret(legacy_path) == legacy)
        write_secret(legacy_path, legacy)
        with open(legacy_path, "rb") as f:
            upgraded = f.read()
        check("legacy file upgraded to headered blob", upgraded.startswith(MAGIC))
        check("upgraded file still reads back", read_secret(legacy_path) == legacy)
        check("no leftovers after upgrade",
              sorted(os.listdir(tmpdir)) == ["accounts.json", "legacy.json"],
              "listing=%r" % (sorted(os.listdir(tmpdir)),))

        # Corrupt the file on disk and confirm a read returns None instead of
        # taking the widget down at startup.
        bad_path = os.path.join(tmpdir, "bad.bin")
        with open(bad_path, "wb") as f:
            f.write(MAGIC + bytes([FORMAT_VERSION, SCHEME_DPAPI_USER]) + b"not a real dpapi blob")
        check("corrupt file on disk -> None",
              read_secret(bad_path) is None or not available(),
              "got=%r" % (read_secret(bad_path),))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n[7] empty is not unreadable, and an unreadable vault is not overwritten")
    tmpdir7 = tempfile.mkdtemp(prefix="nwsec-f1-")
    try:
        missing = os.path.join(tmpdir7, "nope.json")
        check("absent -> STATUS_ABSENT", read_secret_ex(missing) == (None, STATUS_ABSENT))

        zero = os.path.join(tmpdir7, "zero.json")
        open(zero, "wb").close()
        check("zero length -> STATUS_ABSENT", read_secret_ex(zero) == (None, STATUS_ABSENT))

        leg = os.path.join(tmpdir7, "legacy.json")
        with open(leg, "wb") as f:
            f.write(legacy)
        check("legacy plaintext -> STATUS_OK", read_secret_ex(leg) == (legacy, STATUS_OK))

        sealed_path = os.path.join(tmpdir7, "sealed.json")
        write_secret(sealed_path, secret)
        check("wrapped vault -> STATUS_OK", read_secret_ex(sealed_path) == (secret, STATUS_OK))
        check("readback_ok true for what was written", readback_ok(sealed_path, secret) is True)
        check("readback_ok false for other bytes", readback_ok(sealed_path, b"{}") is False)

        # An undecryptable vault: headered, claims DPAPI, payload is garbage.
        # On a fallback host there is no DPAPI to fail, so an unknown scheme
        # byte is used instead -- unreadable on every platform.
        bad = os.path.join(tmpdir7, "bad.json")
        payload = (bytes([SCHEME_DPAPI_USER]) if available() else bytes([99]))
        with open(bad, "wb") as f:
            f.write(MAGIC + bytes([FORMAT_VERSION]) + payload + b"not a real blob")
        before = open(bad, "rb").read()
        got = read_secret_ex(bad)
        check("undecryptable -> STATUS_UNREADABLE, not ABSENT", got == (None, STATUS_UNREADABLE),
              "got=%r" % (got[1],))
        check("read_secret() still collapses it to None", read_secret(bad) is None)
        refused = False
        try:
            write_secret(bad, b'{"version": 1, "accounts": []}')
        except VaultUnreadableError:
            refused = True
        check("write over unreadable vault raises", refused)
        check("original bytes untouched by the refusal", open(bad, "rb").read() == before)
        check("no temp left by the refusal",
              not any(n.endswith(".tmp") for n in os.listdir(tmpdir7)))
        forced = False
        try:
            write_secret(bad, secret, allow_unreadable_overwrite=True)
            forced = read_secret_ex(bad) == (secret, STATUS_OK)
        except Exception:
            forced = False
        check("explicit override still permits the overwrite", forced)
    finally:
        shutil.rmtree(tmpdir7, ignore_errors=True)

    print("\n[6] forced-unavailable fallback (child process, %s=1)" % DISABLE_ENV)
    child = r'''
import json, os, sys, tempfile
sys.path.insert(0, sys.argv[1])
import aisecrets as s
out = {}
out["available"] = s.available()
out["backend"] = s.backend_name()
data = b'{"accounts": ["FALLBACK-SENTINEL"]}'
blob = s.protect(data)
out["kind"] = s.blob_kind(blob)
out["is_protected"] = s.is_protected(blob)
out["round_trip"] = s.unprotect(blob) == data
out["payload_is_plain"] = b"FALLBACK-SENTINEL" in blob
d = tempfile.mkdtemp(prefix="nwsec-fb-")
p = os.path.join(d, "accounts.json")
s.write_secret(p, data)
out["listing"] = sorted(os.listdir(d))
out["read_back"] = s.read_secret(p) == data
out["legacy_ok"] = s.unprotect(b'{"version": 1}') == b'{"version": 1}'
out["corrupt_none"] = s.unprotect(s.MAGIC + bytes([s.FORMAT_VERSION, 99]) + b"zz") is None
out["describe_note_says_plaintext"] = "plaintext" in s.describe()["note"]
os.remove(p); os.rmdir(d)
print(json.dumps(out, sort_keys=True))
'''
    env = dict(os.environ)
    env[DISABLE_ENV] = "1"
    here = os.path.dirname(os.path.abspath(__file__))
    proc = subprocess.run([sys.executable, "-c", child, here], env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        check("child process ran", False, proc.stderr.strip()[-400:])
    else:
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        print("  child result: %s" % json.dumps(res, sort_keys=True))
        check("available() is False", res["available"] is False)
        check("backend_name() is %s" % BACKEND_PLAINTEXT, res["backend"] == BACKEND_PLAINTEXT)
        check("blob_kind is %s" % BACKEND_PLAINTEXT, res["kind"] == BACKEND_PLAINTEXT)
        check("is_protected() is False (loudly visible)", res["is_protected"] is False)
        check("round trip still works", res["round_trip"] is True)
        check("fallback payload is admittedly plain", res["payload_is_plain"] is True)
        check("write/read round trip", res["read_back"] is True)
        check("no temp or backup left", res["listing"] == ["accounts.json"], "listing=%r" % (res["listing"],))
        check("legacy read still unchanged", res["legacy_ok"] is True)
        check("unknown scheme -> None", res["corrupt_none"] is True)
        check("describe() admits plaintext", res["describe_note_says_plaintext"] is True)

    print("\n%s  (%d checks failed)" % ("ALL CHECKS PASSED" if not fails else "FAILURES: " + ", ".join(fails), len(fails)))
    sys.exit(1 if fails else 0)
