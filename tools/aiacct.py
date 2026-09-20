"""Terminal front end for the AI account vault.

The widget's account list lives behind an Electron UI, which is exactly the
wrong place for it to live when something has gone wrong. Every time this
machine has lost the widget -- a wedged WSL share, a GPU launch failure, a
renderer that never painted -- the account vault became unreachable at the
same moment, even though the vault itself is a plain JSON file that was
perfectly healthy. This module exists so that the recovery path needs nothing
but a Python interpreter: no Node, no Electron, no display.

It is also simply a better diagnostic surface. A 200-pixel always-on-top
widget can say "expired"; a terminal can say which of the two copies of the
same login is expired, where each copy lives, and whether the failure was the
credential or the network. That difference is what this file is for.

Three rules shape every output path below, and they are not negotiable:

  * No credential value is ever printed. Not a token, not a refresh token,
    not a fragment of one, not in a verbose mode, not in an error message.
    Timestamps, ids, provider slugs, labels and reasons are safe; anything
    that could be replayed against a provider is not. Values are therefore
    rendered through an explicit key allowlist rather than a blocklist, so a
    field added to a credential shape later cannot leak by default.
  * No raw traceback reaches the user. A traceback from a credential tool
    prints local variables' repr in some tooling and lands in shell
    scrollback either way, so the top level catches everything and prints a
    single short line instead.
  * A command that touches the network says so, and says plainly when the
    request never arrived. The intended users of this tool include people
    behind a region gate where both live providers refuse outright, and
    "failed" is a useless thing to tell them when the truthful answer is
    "your egress was blocked before the provider ever saw you".

Standard library only, on purpose: the recovery tool must not be able to fail
because a dependency is missing.
"""

import argparse
import json
import os
import stat
import sys
import time

# The tool lives in tools/ but imports the vault modules from the repo root,
# so the root goes on sys.path explicitly. Resolving it from __file__ rather
# than the current working directory means the tool works when invoked by
# absolute path from anywhere, which is how it will be used during a repair.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

# Anything whose key matches one of these fragments is rendered as a redaction
# marker no matter where it came from. This is a second line of defence: every
# printer below already works from an explicit allowlist of safe keys, and
# this catches the case where a provider adapter grows a new field and someone
# later widens an allowlist without thinking about it.
_SECRET_HINTS = ("token", "secret", "password", "passwd", "cred", "key",
                 "authorization", "auth", "cookie", "session_id", "api")


def _is_secretish(key):
    k = str(key).lower()
    return any(h in k for h in _SECRET_HINTS)


# Anything longer than this in a provider-supplied string is assumed to be a
# token rather than a label. Real usage fields are plan names, counts and
# timestamps; nothing legitimate in them is 80 characters of opaque text. The
# cap exists because a provider can add a secret-bearing field under an
# innocuous name at any time, and a key-name check alone would print it.
_MAX_SCALAR = 80


def _looks_like_token(value):
    """True for strings that carry a credential's shape rather than a label.

    JWTs are the specific case that matters: a Codex id_token is three
    base64 segments joined by dots, and it arrives under names as harmless as
    'id_token' or nested inside a debug echo of the request.
    """
    if not isinstance(value, str):
        return False
    if len(value) > _MAX_SCALAR:
        return True
    parts = value.split(".")
    return len(parts) == 3 and all(len(p) > 8 for p in parts)


def _sanitize(value, depth=0):
    """Recursively strip anything credential-shaped out of provider data.

    This has to recurse, and the reason is not theoretical: an adapter that
    echoes the raw provider response back under a harmless key like
    'raw_response' buries a refresh token one level below any top-level key
    check. A flat redaction pass looks correct, prints nothing suspicious in
    testing, and leaks the moment a provider changes its response shape.

    Containers are walked to a shallow depth and then collapsed, because a
    deeply nested blob is never useful quota information and walking it
    forever only widens the surface that has to stay correct.
    """
    if depth > 3:
        return "<omitted>"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out[k] = "<redacted>" if _is_secretish(k) else _sanitize(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(v, depth + 1) for v in value[:8]]
    if _looks_like_token(value):
        return "<redacted>"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # An unrecognised object could stringify to anything, including a repr
    # that embeds the credential it was built from, so its type is all that
    # is safe to report.
    return "<%s>" % type(value).__name__


def _fail(msg, code=EXIT_FAIL):
    """Print a one-line error to stderr and raise the exit code.

    Errors go to stderr so that `aiacct list | column -t` and friends keep
    working, and the non-zero exit is what makes the tool usable from a
    repair script that has to stop when something is wrong.
    """
    sys.stderr.write("aiacct: %s\n" % msg)
    raise SystemExit(code)


# ── module loading ────────────────────────────────────────────────────────────
def _load_modules():
    """Import aiaccounts and aiproviders, or explain why we cannot.

    Both modules are under active development by other people, so all three
    realistic failures are handled the same way -- file absent, file half
    written (a SyntaxError from a partial save), or an import-time error --
    because from the user's point of view they are the same situation: the
    vault code is not usable right now, and that is not their fault and not a
    reason to show them a stack trace.
    """
    mods = {}
    for name in ("aiaccounts", "aiproviders"):
        try:
            mods[name] = __import__(name)
        except ImportError:
            _fail("module %s.py is not present in %s -- this tool must run "
                  "from inside the repository." % (name, _REPO_ROOT),
                  EXIT_USAGE)
        except SyntaxError:
            _fail("module %s.py could not be parsed; it is probably being "
                  "written right now. Retry in a moment." % name, EXIT_USAGE)
        except Exception as e:
            _fail("module %s.py failed to import (%s)."
                  % (name, type(e).__name__), EXIT_USAGE)
    return mods["aiaccounts"], mods["aiproviders"]


# ── formatting helpers ────────────────────────────────────────────────────────
def _fmt_time(ts):
    """A UNIX timestamp as local wall-clock time plus a relative hint.

    Both halves earn their place. The absolute time is what a user compares
    against a provider's dashboard; the relative one is what actually answers
    "is this the stale copy?" without mental arithmetic at 2am.
    """
    if ts is None:
        return "unknown"
    try:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (ValueError, OSError, OverflowError):
        return "unreadable"
    return "%s (%s)" % (when, _fmt_delta(ts - time.time()))


def _fmt_delta(secs):
    past = secs < 0
    secs = abs(int(secs))
    if secs < 90:
        out = "%ds" % secs
    elif secs < 5400:
        out = "%dm" % (secs // 60)
    elif secs < 172800:
        out = "%dh %dm" % (secs // 3600, (secs % 3600) // 60)
    else:
        out = "%dd" % (secs // 86400)
    return (out + " ago") if past else ("in " + out)


def _fmt_iso(value):
    """Render a stored ISO timestamp as local time, falling back to as-is.

    added_at is written by the vault as a UTC ISO string. Showing it raw is
    correct but unhelpful when the question is "did I add this before or
    after I re-logged in", so it is converted when it parses and passed
    through untouched when it does not.
    """
    if not value:
        return "unknown"
    try:
        import datetime as dt
        s = str(value).replace("Z", "+00:00")
        return _fmt_time(dt.datetime.fromisoformat(s).timestamp())
    except Exception:
        return str(value)


def _status_word(aip, acct):
    """One word summarising an account's credential, for the list column.

    Deliberately distinct words for distinct situations, because the whole
    point of the rework this tool accompanies was that "expired" was being
    printed for at least three unrelated conditions:

      planned       -- the vendor is registered but has no usage endpoint, so
                       the credential's state is moot.
      unrecoverable -- only a fresh browser login can fix it; no refresh can.
      stale         -- the access token has lapsed but a refresh should work.
      live          -- the access token is still valid right now.
      unknown       -- the credential shape is unrecognised. Not a synonym
                       for dead: we simply cannot tell, and saying "expired"
                       here is what sent users off to redo working logins.
    """
    pid = acct.get("provider")
    meta = aip.provider(pid)
    if meta is None:
        return "unknown-provider"
    if meta.get("status") != "live":
        return "planned"
    cred = acct.get("cred")
    try:
        if aip.is_unrecoverable(pid, cred):
            return "unrecoverable"
        exp = aip.cred_expiry(pid, cred)
    except Exception:
        return "unknown"
    if exp is None:
        return "unknown"
    return "live" if exp > time.time() else "stale"


def _expiry_of(aip, acct):
    try:
        return aip.cred_expiry(acct.get("provider"), acct.get("cred"))
    except Exception:
        return None


def _short(account_id):
    return (account_id or "")[:8]


def _resolve(aia, wanted):
    """Find one account by full id or by an unambiguous id prefix.

    `list` prints shortened ids by default because a full uuid4 hex ruins the
    table, and a shortened id that cannot be pasted into the next command
    would make the shortening actively harmful. So prefixes resolve here, and
    an ambiguous prefix is an error rather than a silent pick of the first
    match -- picking would eventually remove the wrong account.
    """
    if not wanted:
        _fail("no account id given", EXIT_USAGE)
    accounts = aia.list_accounts()
    for a in accounts:
        if a.get("id") == wanted:
            return a
    hits = [a for a in accounts if (a.get("id") or "").startswith(wanted)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        _fail("no account matches id '%s' (try: aiacct list --full-ids)"
              % wanted)
    _fail("id '%s' is ambiguous: %s" % (
        wanted, ", ".join(_short(a.get("id")) for a in hits)))


def _safe_view(aip, acct):
    """The publishable projection of an account: everything except the secret.

    Built by naming the safe keys rather than by deleting the unsafe one. If
    the vault later grows a second secret-bearing field, a blocklist would
    leak it on the next release and this will not.
    """
    return {
        "id": acct.get("id"),
        "provider": acct.get("provider"),
        "label": acct.get("label"),
        "added_at": acct.get("added_at"),
        "source": acct.get("source"),
        "cred_path": acct.get("cred_path"),
        "expires_at": _expiry_of(aip, acct),
        "status": _status_word(aip, acct),
    }


def _table(rows, headers):
    """Minimal column-aligned table.

    Hand-rolled because the tool may not assume anything beyond the standard
    library is installed, and a dependency-free recovery tool is the entire
    premise of this file.
    """
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    out.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        out.append("  ".join(str(c).ljust(widths[i])
                             for i, c in enumerate(row)).rstrip())
    return "\n".join(out)


# ── network-facing messaging ──────────────────────────────────────────────────
# A single place that turns an adapter's error string into something a user in
# a geo-blocked region can act on. The adapters are careful to distinguish
# "never reached the server" (offline) from a real HTTP status, and throwing
# that distinction away in the presentation layer would undo the work.
_NET_NOTES = {
    "offline": (
        "The request never reached the provider. On this kind of connection "
        "that usually means one of: egress blocked, a dropped tunnel, DNS "
        "failure, or a regional block applied before the provider answered. "
        "It says nothing about whether the credential is still valid."),
    "token rejected": (
        "The provider answered and refused the access token (HTTP 401). Try "
        "'aiacct show' to see whether the credential is merely stale and can "
        "be refreshed, or unrecoverable and needs a fresh browser login."),
    "rate limited": (
        "The provider answered with HTTP 429. Wait and retry; this is not a "
        "credential problem."),
}


def _net_note(err):
    """Explanatory paragraph for an adapter error, or a regional-gate hint."""
    if err in _NET_NOTES:
        return _NET_NOTES[err]
    if err.startswith("http 403") or err.startswith("http 451"):
        return ("The provider answered but refused the request outright "
                "(HTTP %s). Both live providers return this to clients in "
                "regions they do not serve, so a working credential can "
                "still produce this result. It is a location refusal, not an "
                "expired login." % err.split()[-1])
    if err.startswith("http "):
        return ("The provider answered with an unexpected status (%s). That "
                "is a provider-side or gateway response, not a statement "
                "about your credential." % err)
    if err.startswith("adapter error"):
        return ("The adapter itself failed before or while parsing the "
                "response. The exception type is shown; no request details "
                "are printed because they carry the credential.")
    return ""


def _print_kv(data, indent="  "):
    """Print a flat dict of provider data, sanitized.

    Every value goes through _sanitize rather than being trusted, because
    this is the only place in the tool that prints data the provider chose
    the shape of.
    """
    for key in sorted(data):
        if _is_secretish(key):
            value = "<redacted>"
        else:
            value = _sanitize(data[key])
            # Providers report reset moments as raw epoch seconds. Printing
            # 1789838698.8 helps nobody decide whether to keep working, so
            # time-like keys are rendered the same way every other timestamp
            # in this tool is.
            if (isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and any(h in str(key).lower()
                            for h in ("reset", "expire", "_at", "until"))
                    and value > 1_000_000_000):
                value = _fmt_time(float(value))
            elif isinstance(value, (dict, list)):
                value = json.dumps(value, sort_keys=True)
        print("%s%-16s %s" % (indent, str(key) + ":", value))


# ── commands ──────────────────────────────────────────────────────────────────
def _render_list(args, aia, aip):
    accounts = aia.list_accounts()
    if args.provider:
        accounts = [a for a in accounts if a.get("provider") == args.provider]
    if args.json:
        print(json.dumps([_safe_view(aip, a) for a in accounts], indent=2))
        return EXIT_OK
    if not accounts:
        print("No accounts registered.")
        print("Vault: %s" % aia.accounts_file())
        print("Run 'aiacct import' to register logins the CLIs already have.")
        return EXIT_OK
    rows = []
    for a in accounts:
        ident = a.get("id") if args.full_ids else _short(a.get("id"))
        rows.append([ident, a.get("provider") or "?", a.get("label") or "",
                     _fmt_time(_expiry_of(aip, a)), _status_word(aip, a)])
    print(_table(rows, ["ID", "PROVIDER", "LABEL", "EXPIRES", "STATUS"]))
    if not args.full_ids:
        print("\n%d account(s). Ids are shortened; any unique prefix works as "
              "an ACCOUNT_ID, or use --full-ids." % len(rows))
    return EXIT_OK


def cmd_list(args, aia, aip):
    """One render, or a repeating one under --watch.

    The repeating form exists because the interesting numbers here move while
    you work: a token lapses, the widget refreshes it, an account flips from
    stale back to live. Re-running the command by hand to see that happen is
    exactly the kind of chore a watch flag removes.
    """
    interval = getattr(args, "watch", None)
    if interval is None:
        return _render_list(args, aia, aip)
    # A zero or negative interval would spin the vault file as fast as the
    # disk allows, which is a denial of service against the user's own laptop
    # rather than a useful diagnostic, so it is refused rather than clamped.
    if interval < 0.5:
        _fail("--watch needs an interval of at least 0.5 seconds.", EXIT_USAGE)
    # JSON output is meant to be piped into something else; redrawing it on a
    # timer produces a stream that is not valid JSON, so the two are refused
    # together rather than silently producing garbage.
    if getattr(args, "json", False):
        _fail("--watch and --json cannot be combined: a repeating render is "
              "not a parseable JSON document.", EXIT_USAGE)
    clearable = False
    try:
        clearable = sys.stdout.isatty()
    except Exception:
        clearable = False
    try:
        while True:
            # Only a real terminal gets the cursor-home-and-erase sequence.
            # Writing escape codes into a redirected file would corrupt a log
            # that someone is keeping of a long watch session.
            if clearable:
                sys.stdout.write("\033[H\033[2J")
            else:
                print()
            print("aiacct list -- %s, refreshing every %gs (Ctrl-C to stop)"
                  % (time.strftime("%H:%M:%S"), interval))
            print()
            _render_list(args, aia, aip)
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        # Caught here rather than left to the top level so that stopping a
        # watch is an ordinary, successful end to the command. A traceback
        # from a credential tool is precisely where a secret would end up in
        # shell scrollback, so Ctrl-C never produces one.
        print("\nStopped watching.")
        return EXIT_OK


def cmd_show(args, aia, aip):
    acct = _resolve(aia, args.account_id)
    pid = acct.get("provider")
    meta = aip.provider(pid) or {}
    cred = acct.get("cred")
    exp = _expiry_of(aip, acct)
    try:
        dead = bool(aip.is_unrecoverable(pid, cred))
    except Exception:
        dead = False
    print("id:          %s" % acct.get("id"))
    print("provider:    %s (%s, %s)" % (pid, meta.get("name") or "unknown",
                                        meta.get("status") or "unregistered"))
    print("label:       %s" % (acct.get("label") or ""))
    print("added:       %s" % _fmt_iso(acct.get("added_at")))
    print("source:      %s" % (acct.get("source") or "unknown"))
    print("imported from: %s" % (acct.get("cred_path")
                                 or "not imported from a file"))
    if acct.get("cred_updated_at"):
        print("refreshed:   %s" % _fmt_iso(acct.get("cred_updated_at")))
    print("expires:     %s" % _fmt_time(exp))
    print("status:      %s" % _status_word(aip, acct))
    # The refresh window is the single most useful thing this command can
    # say, because it is the difference between "the widget will fix this by
    # itself on the next poll" and "stop waiting and go log in again".
    if dead:
        print("refreshable: no -- unrecoverable. The refresh token is absent "
              "or has itself lapsed (max %d days); only a fresh browser "
              "login with the vendor's CLI will restore this account."
              % (aip.CRED_MAX_STALE // 86400))
    elif exp is None:
        print("refreshable: unknown -- the credential shape is not one this "
              "build recognises, so no claim is made either way.")
    elif exp > time.time():
        print("refreshable: not needed -- the access token is still valid.")
    else:
        print("refreshable: yes -- inside the %d-day refresh window; the "
              "next poll should renew it without any login."
              % (aip.CRED_MAX_STALE // 86400))
    print("\nThe credential itself is never printed by this tool.")
    return EXIT_OK


def cmd_add(args, aia, aip):
    pid = args.provider
    if aip.provider(pid) is None:
        _fail("unknown provider '%s'. Known: %s"
              % (pid, ", ".join(aip.provider_ids())), EXIT_USAGE)
    meta = aip.provider(pid)
    if not args.from_file:
        # There is no honest headless path to an OAuth login: both live
        # providers require an interactive browser round trip, and inventing
        # a fake one here would produce an account row that can never poll.
        # So the tool says exactly what to run instead of pretending.
        print("Cannot register '%s' without a credential file." % pid)
        print()
        print("OAuth logins for these vendors need an interactive browser "
              "session, which this tool deliberately does not fake. Do the "
              "login with the vendor's own CLI, then bring the result in:")
        print()
        print("  1. %s" % (meta.get("hint") or "Complete the vendor login."))
        print("  2. aiacct import")
        print("     ...which scans the known CLI credential locations and "
              "registers anything not already in the vault.")
        print()
        print("If the credential file is somewhere non-standard (a WSL home, "
              "an encrypted volume), point at it directly instead:")
        print()
        print("  aiacct add %s %s --from-file PATH"
              % (pid, args.label or "LABEL"))
        print()
        if meta.get("status") != "live":
            print("Note: '%s' is registered as '%s'. It can be stored and "
                  "listed, but polling it returns 'not supported yet' until "
                  "a verified usage endpoint exists." % (pid, meta["status"]))
        return EXIT_USAGE
    path = os.path.expanduser(os.path.expandvars(args.from_file))
    if not os.path.isfile(path):
        _fail("no such credential file: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cred = json.load(f)
    except ValueError:
        _fail("%s is not valid JSON, so it is not a credential file this "
              "tool can read." % path)
    except OSError as e:
        _fail("cannot read %s (%s)" % (path, e.strerror or "unreadable"))
    if not isinstance(cred, dict):
        _fail("%s does not contain a JSON object." % path)
    acct = aia.add_account(pid, args.label, cred, "import", path)
    print("Registered %s as '%s'." % (pid, acct.get("label")))
    print("id:      %s" % acct.get("id"))
    print("expires: %s" % _fmt_time(_expiry_of(aip, acct)))
    print("status:  %s" % _status_word(aip, acct))
    return EXIT_OK


def cmd_remove(args, aia, aip):
    acct = _resolve(aia, args.account_id)
    label = acct.get("label") or acct.get("provider")
    if not args.yes:
        # Interactive by default because removal is destructive in a way the
        # user cannot undo from here: the vault holds the only copy of a
        # credential that has been refreshed past what the CLI has on disk.
        try:
            answer = input("Remove %s account '%s' (%s)? [y/N] "
                           % (acct.get("provider"), label,
                              _short(acct.get("id"))))
        except EOFError:
            _fail("no terminal to confirm on; pass --yes to remove "
                  "non-interactively.")
        if answer.strip().lower() not in ("y", "yes"):
            print("Left alone.")
            return EXIT_OK
    if not aia.remove_account(acct.get("id")):
        _fail("removal failed; the account is no longer in the vault.")
    print("Removed '%s' (%s)." % (label, _short(acct.get("id"))))
    return EXIT_OK


def cmd_rename(args, aia, aip):
    acct = _resolve(aia, args.account_id)
    old = acct.get("label")
    if not aia.rename_account(acct.get("id"), args.label):
        _fail("rename failed; the account is no longer in the vault.")
    print("Renamed '%s' to '%s' (%s)." % (old, args.label,
                                          _short(acct.get("id"))))
    return EXIT_OK


def cmd_import(args, aia, aip):
    before = {a.get("id") for a in aia.list_accounts()}
    try:
        new = aia.import_from_cli()
    except Exception as e:
        _fail("import failed (%s)" % type(e).__name__)
    after = aia.list_accounts()
    if new:
        print("Added %d account(s):" % len(new))
        rows = [[_short(a.get("id")), a.get("provider") or "?",
                 a.get("label") or "", a.get("cred_path") or "?"]
                for a in new]
        print(_table(rows, ["ID", "PROVIDER", "LABEL", "FROM"]))
    else:
        # "imported 0 accounts" on its own reads like a failure and sends
        # people looking for a bug. The vault is the authority on why the
        # number is zero, so say which of the two reasons applies.
        if before:
            print("Added 0 accounts: every CLI login found on this machine "
                  "is already registered in the vault.")
        else:
            print("Added 0 accounts: no CLI credential file was found in any "
                  "known location.")
            print("Log in with the vendor CLI first ('claude', or "
                  "'codex login'), then run this again.")
    skipped = len(after) - len(new)
    if skipped > 0:
        print("\nSkipped %d already-registered account(s); their stored "
              "credentials were left untouched." % skipped)
    return EXIT_OK


def cmd_usage(args, aia, aip):
    acct = _resolve(aia, args.account_id)
    pid = acct.get("provider")
    meta = aip.provider(pid) or {}
    if meta.get("status") != "live":
        print("%s is registered as '%s': there is no verified usage endpoint "
              "for it yet, so no quota can be read."
              % (pid, meta.get("status") or "unregistered"))
        return EXIT_FAIL
    # Announced before the call, not after, so that a request hanging against
    # a silently-dropped connection still leaves the user knowing what the
    # tool is waiting on.
    print("Querying %s for '%s' over the network..."
          % (pid, acct.get("label") or pid))
    data = aip.fetch_usage(pid, acct.get("cred"))
    if not isinstance(data, dict):
        _fail("the provider adapter returned an unusable result.")
    err = data.get("error")
    if err:
        # The adapter's error strings are fixed vocabulary today, but this is
        # a value that originates near the response, so it is sanitized
        # rather than trusted before it reaches the terminal.
        err = _sanitize(err)
        print("Could not read usage: %s" % err)
        note = _net_note(str(err))
        if note:
            print()
            print(note)
        return EXIT_FAIL
    print("Usage for '%s' (%s):" % (acct.get("label") or pid, pid))
    _print_kv({k: v for k, v in data.items() if v is not None})
    return EXIT_OK


def cmd_providers(args, aia, aip):
    rows = []
    for p in aip.PROVIDERS:
        supported = "yes" if p.get("status") == "live" else "no"
        rows.append([p.get("id"), p.get("name"), p.get("status"),
                     supported, p.get("auth")])
    print(_table(rows, ["ID", "NAME", "STATUS", "POLLABLE", "AUTH"]))
    live = [p["id"] for p in aip.PROVIDERS if p.get("status") == "live"]
    print("\n%d provider(s) registered; only %s can be polled today."
          % (len(aip.PROVIDERS), " and ".join(live) or "none"))
    print("The rest are storable and listable, but 'usage' answers 'not "
          "supported yet' rather than guessing at an endpoint.")
    if args.hints:
        print()
        for p in aip.PROVIDERS:
            print("%-12s %s" % (p.get("id"), p.get("hint") or ""))
    return EXIT_OK


# ── doctor ────────────────────────────────────────────────────────────────────
# Fallback knowledge of where the CLIs keep credentials, used only when the
# vault module does not expose its own helpers (it is under active
# development, and doctor losing its most valuable check because a helper was
# renamed would defeat the purpose of a recovery tool).
_FALLBACK_CLI = {
    "claude": os.path.join(".claude", ".credentials.json"),
    "codex": os.path.join(".codex", "auth.json"),
}


def _cli_paths(aia, provider_id):
    fn = getattr(aia, "cli_cred_paths", None)
    if callable(fn):
        try:
            return list(fn(provider_id))
        except Exception:
            pass
    tail = _FALLBACK_CLI.get(provider_id)
    if not tail:
        return []
    return [os.path.join(os.path.expanduser("~"), tail)]


def _fingerprint(aia, provider_id, cred):
    fn = getattr(aia, "cred_fingerprint", None)
    if callable(fn):
        try:
            return fn(provider_id, cred)
        except Exception:
            return None
    return None


# Files the widget writes beside the vault on purpose, mapped to the sentence
# that explains what each one is for. The .imported marker is the important
# entry: the sidecar writes it once it has imported the CLI logins it found,
# so that an account the user deliberately deleted is never silently
# re-imported on the next launch. An earlier version of this tool listed it
# as debris and advised deleting it, which was actively harmful advice --
# following it would resurrect every account the user had removed.
_EXPECTED_NEIGHBOURS = {
    ".imported": (
        "first-run import marker, written by the sidecar. It records that the "
        "CLI logins on this machine have already been offered to the vault, "
        "so an account you deleted on purpose is not re-imported behind your "
        "back on the next launch. It holds no credential material. Do not "
        "delete it: doing so would resurrect the accounts you removed."
    ),
}


def _neighbour_files(path):
    """Sort the files sitting beside the vault into expected and debris.

    The distinction matters more than it looks. A surviving .tmp means an
    atomic write died midway, and that file is a second copy of live refresh
    tokens at rest -- a real finding that deserves a warning. A marker file
    the widget wrote on purpose is neither a credential nor a fault, and
    lumping the two together under one alarming heading teaches the user to
    delete things they need. So they are classified here, once, and reported
    separately everywhere else.

    Returns a dict with two sorted lists: "expected" holds (path, reason)
    pairs, "debris" holds plain paths.
    """
    directory = os.path.dirname(path) or "."
    base = os.path.basename(path)
    expected = []
    debris = []
    try:
        names = os.listdir(directory)
    except OSError:
        return {"expected": [], "debris": []}
    for name in names:
        if name == base or not name.startswith(base):
            continue
        suffix = name[len(base):]
        reason = _EXPECTED_NEIGHBOURS.get(suffix)
        if reason:
            expected.append((os.path.join(directory, name), reason))
        elif name.endswith((".tmp", ".bak", ".old", "~", ".swp")) or \
                name.startswith(base + "."):
            debris.append(os.path.join(directory, name))
    return {"expected": sorted(expected), "debris": sorted(debris)}


_BLOB_KIND_NOTES = {
    "dpapi-user": "the bytes on disk are sealed with DPAPI under this user "
                  "account. Another user on this machine, or a copy of the "
                  "file carried elsewhere, cannot read the tokens.",
    "plaintext-fallback": "the bytes on disk are wrapped in the encryption "
                          "envelope but NOT encrypted. Anything that can open "
                          "the file can read the tokens.",
    "legacy-unwrapped": "the bytes on disk are a bare JSON vault written "
                        "before encryption existed. The tokens are readable "
                        "by anything that can open the file; the next write "
                        "by the widget upgrades it in place.",
    "unknown": "the bytes on disk match no format this build recognises. "
               "That is worth investigating before trusting the vault.",
}


def _safe_note(value):
    """Render an explanatory sentence from another module without trusting it.

    The generic sanitiser redacts any scalar longer than 80 characters,
    because that is what an opaque token looks like. A full English sentence
    trips the same rule, and printing "<redacted>" where the explanation of
    the user's encryption status should be is a worse outcome than printing
    nothing. So prose is allowed through a narrower gate instead: a
    credential is one unbroken run of characters, whereas a sentence is many
    short words separated by spaces. A value with any word long enough to be
    a token, or with no spaces at all, is refused rather than shown.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or " " not in text:
        return None
    if any(len(word) > 40 for word in text.split()):
        return None
    return text[:400]


def _load_secrets():
    """Import the encryption module defensively.

    This is imported here rather than at the top of the file because it is
    the one dependency that may legitimately be absent or half-written: it is
    newer than the rest of the vault, and a recovery tool that cannot start
    because an optional module is mid-edit is worthless precisely when it is
    needed. Returns (module, explanation); exactly one of the two is set.
    """
    try:
        import aisecrets
    except ImportError:
        return None, ("module aisecrets.py is not present beside the vault "
                      "modules, so this build has no encryption layer at all "
                      "and credentials are stored as plain JSON.")
    except SyntaxError:
        return None, ("module aisecrets.py could not be parsed; it is "
                      "probably being edited right now. No claim about "
                      "encryption can be made until it imports.")
    except Exception as e:
        return None, ("module aisecrets.py failed to import (%s), so this "
                      "tool cannot say whether the vault is encrypted."
                      % type(e).__name__)
    return aisecrets, ""


def _report_encryption(path, exists):
    """Say plainly whether the vault is encrypted at rest.

    Two separate questions are answered, because they can disagree and the
    disagreement is the whole point. What the module reports it *can* do is a
    capability claim; what the first bytes of the vault actually are is the
    ground truth. A user who believes their tokens are encrypted while the
    file on disk is plaintext has been quietly betrayed, so the plaintext
    case is stated in as many words rather than implied by omission.

    Returns the number of problems to add to doctor's count.
    """
    secrets, note = _load_secrets()
    if secrets is None:
        print("backend: unavailable")
        print("WARNING: %s" % note)
        print("Treat the vault as plaintext until this is resolved.")
        return 1
    problems = 0
    raw_info = {}
    try:
        raw_info = secrets.describe() or {}
    except Exception as e:
        print("backend: unreadable (describe() raised %s)" % type(e).__name__)
        raw_info = {}
    info = raw_info
    # Provider-shaped data printed here goes through the same sanitiser every
    # other printed value does. describe() is documented to hold no secret
    # material, but a diagnostic that trusts a claim it could instead verify
    # is how the one nested leak in this file got in.
    info = _sanitize(info)
    backend = info.get("backend")
    if backend is None:
        try:
            backend = _sanitize(secrets.backend_name())
        except Exception:
            backend = "unknown"
    active = info.get("encrypted")
    if active is None:
        try:
            active = bool(secrets.available())
        except Exception:
            active = None
    print("backend: %s" % backend)
    if active is True:
        print("active:  yes -- new writes are encrypted.")
    elif active is False:
        problems += 1
        print("active:  NO")
        print("WARNING: encryption is NOT active on this machine, so "
              "credentials are stored in plaintext. Anything running as this "
              "user, and anyone who can read the file, can read the refresh "
              "tokens in it.")
    else:
        problems += 1
        print("active:  unknown -- the module did not answer, so no "
              "assurance of encryption can be given. Assume plaintext.")
    # describe()'s note is fetched from the unsanitised dict on purpose: the
    # generic sanitiser has already rejected it for being a long scalar, and
    # _safe_note applies the check that actually distinguishes prose from a
    # token rather than the one that distinguishes short from long.
    note_text = _safe_note((raw_info or {}).get("note"))
    if note_text:
        print("note:    %s" % note_text)

    # The file itself is the authority. A module that reports it can encrypt
    # says nothing about bytes written before it was wired in, or by a build
    # where the backend was unavailable.
    if not exists:
        print("on disk: no vault file yet, so there is nothing to classify.")
        return problems
    try:
        # Only the header is needed to classify the blob, and reading the
        # whole file would pull every live token into this process for no
        # benefit.
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError as e:
        print("on disk: cannot read the vault to classify it (%s)"
              % (e.strerror or "unreadable"))
        return problems + 1
    try:
        kind = _sanitize(secrets.blob_kind(head))
    except Exception as e:
        print("on disk: classification failed (%s)" % type(e).__name__)
        return problems + 1
    print("on disk: %s -- %s"
          % (kind, _BLOB_KIND_NOTES.get(kind, "unrecognised classification.")))
    try:
        protected = bool(secrets.is_protected(head))
    except Exception:
        protected = False
    if not protected:
        problems += 1
        print("WARNING: the vault file as it exists right now is NOT "
              "encrypted. Whatever the backend above reports it is capable "
              "of, the tokens on disk are readable.")
    return problems


def cmd_doctor(args, aia, aip):
    problems = 0
    path = aia.accounts_file()
    print("== vault ==")
    print("path:   %s" % path)
    exists = os.path.isfile(path)
    print("exists: %s" % ("yes" if exists else "no"))
    if exists:
        try:
            st = os.stat(path)
            mode = stat.S_IMODE(st.st_mode)
            print("mode:   %04o  size: %d bytes  modified: %s"
                  % (mode, st.st_size, _fmt_time(st.st_mtime)))
            if os.name != "nt" and mode & 0o077:
                problems += 1
                print("WARNING: the vault is readable by other users on this "
                      "system. It holds live refresh tokens; it should be "
                      "0600.")
            if os.name == "nt":
                print("note:   Windows honours only the read-only bit here, "
                      "so the mode above is advisory; NTFS ACLs are what "
                      "actually govern access.")
        except OSError as e:
            problems += 1
            print("ERROR: cannot stat the vault (%s)"
                  % (e.strerror or "unknown"))
        if not os.access(path, os.R_OK):
            problems += 1
            print("ERROR: the vault exists but is not readable by this user.")
    else:
        print("note:   nothing is registered yet; this is normal on a fresh "
              "install and 'aiacct import' will create it.")

    neighbours = _neighbour_files(path)
    if neighbours["expected"]:
        print("\nExpected file(s) beside the vault -- these are fine, leave "
              "them alone:")
        for npath, reason in neighbours["expected"]:
            print("  OK  %s" % npath)
            print("      %s" % reason)
    if neighbours["debris"]:
        problems += 1
        print("\nWARNING: %d leftover file(s) beside the vault:"
              % len(neighbours["debris"]))
        for s in neighbours["debris"]:
            print("  %s" % s)
        print("A surviving .tmp means an atomic write was interrupted. These "
              "are extra copies of live credentials at rest and should be "
              "deleted once the vault itself reads correctly; 'aiacct repair' "
              "will do it. This does not refer to the expected file(s) listed "
              "above, which contain no credentials.")

    print("\n== encryption at rest ==")
    problems += _report_encryption(path, exists)

    accounts = aia.list_accounts()
    print("\n== accounts (%d registered) ==" % len(accounts))
    if not accounts:
        print("none")
    for a in accounts:
        word = _status_word(aip, a)
        if word in ("unrecoverable", "unknown-provider"):
            problems += 1
        print("%-8s  %-10s %-18s %-14s %s"
              % (_short(a.get("id")), a.get("provider") or "?",
                 (a.get("label") or "")[:18], word,
                 _fmt_time(_expiry_of(aip, a))))

    # The check that justifies this whole command. The same login routinely
    # exists twice on this class of machine -- once in the Windows profile,
    # once inside a WSL home -- and the two copies drift apart. A stale copy
    # registered in the vault while a live copy sits unregistered on disk
    # looks exactly like "my subscription broke", which is the wrong
    # conclusion and costs an unnecessary re-login every time.
    print("\n== CLI credential locations ==")
    known = set()
    for a in accounts:
        fp = _fingerprint(aia, a.get("provider"), a.get("cred"))
        if fp:
            known.add(fp)
    seen_fp = {}
    unregistered = 0
    checked = 0
    # Every registered provider is asked, not just the two live ones: the
    # vault's cli_cred_paths() is the authority on where credentials live, and
    # hardcoding a pair here would silently stop checking a vendor on the day
    # its path is added there.
    for pid in aip.provider_ids():
        for cand in _cli_paths(aia, pid):
            checked += 1
            if not os.path.isfile(cand):
                continue
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    cred = json.load(f)
            except Exception:
                print("%-7s %s -- present but unreadable or not JSON"
                      % (pid, cand))
                continue
            fp = _fingerprint(aia, pid, cred)
            exp = None
            try:
                exp = aip.cred_expiry(pid, cred)
            except Exception:
                pass
            mark = "registered" if (fp and fp in known) else "NOT REGISTERED"
            if fp and fp not in known:
                unregistered += 1
            print("%-7s %s\n        expires %s -- %s"
                  % (pid, cand, _fmt_time(exp), mark))
            if fp:
                seen_fp.setdefault(fp, []).append((cand, exp))
    if not checked:
        print("no candidate locations to check")
    dupes = {fp: v for fp, v in seen_fp.items() if len(v) > 1}
    if dupes:
        print("\nNOTE: the same login was found in more than one location:")
        for fp, copies in dupes.items():
            for cand, exp in copies:
                print("  %s (expires %s)" % (cand, _fmt_time(exp)))
        print("Two copies of one login is normal when a CLI is installed both "
              "on Windows and inside WSL. They drift: whichever copy was used "
              "last has the newer token, and the vault holds only one of "
              "them. If usage fails for this account, the registered copy is "
              "probably the stale one -- remove it and re-import.")
    if unregistered:
        problems += 1
        print("\nWARNING: %d CLI login(s) on disk are NOT in the vault."
              % unregistered)
        print("This is the failure that masks a working subscription: a stale "
              "credential registered in the vault reports 'expired' while a "
              "live one sits unregistered on disk. Run 'aiacct import' to "
              "register them, then remove whichever duplicate is stale.")

    print("\n== summary ==")
    if problems:
        print("%d problem(s) found." % problems)
        return EXIT_FAIL
    print("No problems found.")
    return EXIT_OK


# ── repair ────────────────────────────────────────────────────────────────────
def _confirm(question, assume_yes):
    """Ask before doing something destructive, unless told not to.

    Repair is the one command here that deletes things without being pointed
    at them, so every action is described first and then confirmed. A closed
    stdin is treated as "no": a script that forgot --yes should leave the
    vault exactly as it found it rather than guess at consent.
    """
    if assume_yes:
        print("%s [y/N] y (--yes)" % question)
        return True
    try:
        answer = input("%s [y/N] " % question)
    except EOFError:
        print("no terminal to confirm on; skipped. Pass --yes to act "
              "non-interactively.")
        return False
    return answer.strip().lower() in ("y", "yes")


def cmd_repair(args, aia, aip):
    """Fix what doctor can detect, stating each action before taking it.

    The guiding rule is that a repair tool which damages a healthy system is
    worse than no repair tool, so nothing here acts on a guess: every branch
    either finds a concrete fault, describes it, and asks, or says there was
    nothing to do and leaves.
    """
    path = aia.accounts_file()
    print("Vault: %s" % path)
    actions = 0
    failures = 0

    # 1. Leftover temp files. These are the only files beside the vault that
    #    are safe to delete, and the only ones that are a second copy of live
    #    refresh tokens at rest.
    neighbours = _neighbour_files(path)
    for npath, reason in neighbours["expected"]:
        print("\nLeaving %s alone." % npath)
        print("  %s" % reason)
    for debris in neighbours["debris"]:
        actions += 1
        print("\nFound a leftover file beside the vault:")
        print("  %s" % debris)
        print("The vault is written atomically through a temp file, so this "
              "one survived an interrupted write. It is a second copy of "
              "live credentials sitting at rest, which is why removing it is "
              "an improvement rather than a loss -- the vault itself is "
              "unaffected.")
        if _confirm("Delete this leftover file?", args.yes):
            try:
                os.remove(debris)
                print("Deleted.")
            except OSError as e:
                failures += 1
                print("Could not delete it (%s). Remove it by hand."
                      % (e.strerror or "unknown"))
        else:
            print("Left in place.")

    # 2. Accounts whose credential can no longer be refreshed. They cannot be
    #    revived from here by any means, so they poll, fail, and make the
    #    whole list look broken until the user re-adds them anyway.
    dead = []
    for a in aia.list_accounts():
        try:
            if aip.is_unrecoverable(a.get("provider"), a.get("cred")):
                dead.append(a)
        except Exception:
            # An adapter that cannot answer is not evidence of a dead
            # account, and deleting on a failed check would be exactly the
            # damage this command must not do.
            continue
    for a in dead:
        actions += 1
        label = a.get("label") or a.get("provider")
        print("\nAccount '%s' (%s, %s) is unrecoverable: its refresh token "
              "is absent or has itself lapsed, so no poll will ever revive "
              "it. Restoring it needs a fresh browser login with the "
              "vendor's CLI followed by 'aiacct import' -- removing the dead "
              "row here loses nothing you still have."
              % (label, a.get("provider"), _short(a.get("id"))))
        if _confirm("Remove this account from the vault?", args.yes):
            if aia.remove_account(a.get("id")):
                print("Removed.")
            else:
                failures += 1
                print("Removal failed; the account is no longer in the vault.")
        else:
            print("Kept.")

    # 3. CLI logins on disk that the vault does not know about. This is the
    #    failure that masks a working subscription, so repair offers the
    #    import rather than only naming the problem.
    known = set()
    for a in aia.list_accounts():
        fp = _fingerprint(aia, a.get("provider"), a.get("cred"))
        if fp:
            known.add(fp)
    unregistered = []
    for pid in aip.provider_ids():
        for cand in _cli_paths(aia, pid):
            if not os.path.isfile(cand):
                continue
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    cred = json.load(f)
            except Exception:
                continue
            fp = _fingerprint(aia, pid, cred)
            if fp and fp not in known:
                unregistered.append((pid, cand))
    if unregistered:
        actions += 1
        print("\n%d CLI login(s) on disk are not registered in the vault:"
              % len(unregistered))
        for pid, cand in unregistered:
            print("  %-7s %s" % (pid, cand))
        print("An unregistered live login next to a stale registered one is "
              "what makes a working subscription report 'expired'. Importing "
              "adds them; it never overwrites or removes what is already "
              "there.")
        if _confirm("Import these logins into the vault?", args.yes):
            try:
                new = aia.import_from_cli()
            except Exception as e:
                failures += 1
                new = []
                print("Import failed (%s)." % type(e).__name__)
            for a in new:
                print("Imported %s as '%s' (%s)."
                      % (a.get("provider"), a.get("label"),
                         _short(a.get("id"))))
            if not new and not failures:
                print("Nothing was imported; the marker file beside the vault "
                      "records that these logins were already offered once "
                      "and declined by deletion. Use 'aiacct add PROVIDER "
                      "LABEL --from-file PATH' to register one deliberately.")
        else:
            print("Not imported.")

    print()
    if not actions:
        print("Nothing to repair: no leftover temp file, no unrecoverable "
              "account, and every CLI login on disk is registered.")
        print("The .imported marker beside the vault is left untouched by "
              "design. It is not debris: deleting it would let the sidecar "
              "re-import accounts you deliberately removed.")
        return EXIT_OK
    if failures:
        print("%d repair action(s) attempted, %d could not be completed."
              % (actions, failures))
        return EXIT_FAIL
    print("%d repair action(s) handled. Run 'aiacct doctor' to confirm."
          % actions)
    return EXIT_OK


# ── argument parsing ──────────────────────────────────────────────────────────
def build_parser():
    p = argparse.ArgumentParser(
        prog="aiacct",
        description="Inspect and repair the AI subscription account vault "
                    "from a terminal, without the widget running.",
        epilog="No command in this tool ever prints a credential value.")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("list", help="show every registered account")
    sp.add_argument("--full-ids", action="store_true",
                    help="print untruncated account ids")
    sp.add_argument("--provider", help="only show one provider's accounts")
    sp.add_argument("--json", action="store_true",
                    help="machine-readable output (credential omitted)")
    sp.add_argument("--watch", type=float, metavar="SECONDS",
                    help="re-render every SECONDS until Ctrl-C")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="detail for one account")
    sp.add_argument("account_id", help="full id or any unique prefix")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("add", help="register an account from a credential file")
    sp.add_argument("provider")
    sp.add_argument("label")
    sp.add_argument("--from-file", metavar="PATH",
                    help="path to a credential file written by the vendor CLI")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("remove", help="unregister an account")
    sp.add_argument("account_id", help="full id or any unique prefix")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="skip the confirmation prompt (for scripts)")
    sp.set_defaults(func=cmd_remove)

    sp = sub.add_parser("rename", help="change an account's label")
    sp.add_argument("account_id", help="full id or any unique prefix")
    sp.add_argument("label", metavar="NEW_LABEL")
    sp.set_defaults(func=cmd_rename)

    sp = sub.add_parser("import", help="register logins the CLIs already have")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("usage",
                        help="fetch one account's quota (touches the network)")
    sp.add_argument("account_id", help="full id or any unique prefix")
    sp.set_defaults(func=cmd_usage)

    sp = sub.add_parser("providers", help="list every known vendor")
    sp.add_argument("--hints", action="store_true",
                    help="also print each vendor's login hint")
    sp.set_defaults(func=cmd_providers)

    sp = sub.add_parser("doctor", help="diagnose the vault and CLI logins")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("repair",
                        help="fix what doctor found, asking before each step")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="skip every confirmation prompt (for scripts)")
    sp.set_defaults(func=cmd_repair)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    aia, aip = _load_modules()
    return args.func(args, aia, aip)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.stderr.write("\naiacct: interrupted\n")
        raise SystemExit(130)
    except Exception as exc:
        # The last line of defence. A traceback here would print the repr of
        # local variables in some runners and would land in shell scrollback
        # in all of them -- and the locals on this stack include credential
        # objects. So only the exception's class name escapes, never its
        # message and never the frames.
        sys.stderr.write(
            "aiacct: unexpected internal error (%s). No details are printed "
            "because this stack can hold credential values; re-run with the "
            "vault path set to a copy if you need to debug.\n"
            % type(exc).__name__)
        raise SystemExit(EXIT_FAIL)
