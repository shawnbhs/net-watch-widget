"""Isolated per-account interactive login for the AI usage rows.

Why this module has to exist at all
-----------------------------------
Every coding-assistant CLI this widget reads stores exactly ONE login. Running
the vendor's login command a second time, intending to add a second account,
does not produce a second sign-in prompt: the CLI finds the session it already
wrote in the user's home directory, decides it is already authenticated, and
hands back the SAME account. A user with a personal account and a work account
on two different addresses therefore cannot get the second one in at all, no
matter how many times he runs the command. That is not a widget bug -- it is
how the CLIs behave -- so the widget cannot fix it by asking more politely.

The way out is to remove the thing the CLI is finding. Each login here runs
inside a DISPOSABLE SANDBOX: a temporary directory handed to the CLI as its
home. The CLI looks there for an existing session, finds an empty directory,
and has no choice but to run a genuine fresh sign-in. The credential it writes
lands inside the sandbox rather than on top of the user's real one; the caller
copies it into the widget's vault (aiaccounts) and the sandbox is destroyed.
The user's existing working login is never read, never written, never touched.

Design rules this file obeys, because it runs a real authentication:

  * The sandbox is temporary, 0700, and is asserted to be OUTSIDE the real
    home and outside this repository before anything is written to it. If the
    assertion fails the login is refused. Destroying the one login the user
    already has, while trying to add a second, would be a strictly worse
    outcome than the bug we are fixing, so the check fails closed.
  * The launcher takes an argument list and never uses a shell, matching
    core.ai_login_launch, so no value in .env can smuggle in a second command.
  * Tear-down is registered the moment the sandbox exists, and runs from
    atexit as well as from the caller. A temporary directory left behind with
    a live refresh token in it is a security defect this codebase has already
    committed once and must not repeat.
  * Nothing here logs, prints or raises a token value. Provider slugs,
    sandbox paths, expiry timestamps, exit codes and reason strings are safe
    to surface; the credential body is not.

Standard library only -- the sidecar's single dependency is psutil and this
module must not add a second.
"""

import atexit
import errno
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

# Sibling modules, imported the same defensive way core.py imports them: a
# partial checkout or a quarantined file must degrade this feature to
# "unavailable" rather than take the widget down with it.
try:
    import aiaccounts as _vault
except Exception:                                    # pragma: no cover
    _vault = None
try:
    import aiproviders as _prov
except Exception:                                    # pragma: no cover
    _prov = None


# ── configuration ─────────────────────────────────────────────────────────────
# Read the same .env that core.py reads, with the same parser, rather than
# importing core: core pulls in psutil and winreg and starts doing work at
# import time, and this module has to be importable from a test runner that
# has neither. Keeping the two parsers identical in behaviour is deliberate --
# two different expansion rules for one settings file would be a bug waiting
# to happen.

def _load_env():
    """KEY=VALUE lines from .env beside this script; missing file is fine."""
    cfg = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.removeprefix("export ").partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                cfg[key.strip()] = value
    except OSError:
        pass
    return cfg


ENV = _load_env()


def env_str(key, default=""):
    """Config value from .env, else the process environment, else default."""
    value = (ENV.get(key, os.environ.get(key, "")) or "").strip()
    return value or default


# How long to wait for a human to finish a browser round-trip and paste a
# code back. Mirrors core.AI_LOGIN_WAIT / AI_LOGIN_POLL: the window is minutes
# rather than seconds, and the poll is slow because the only thing that can
# change during it is one small file appearing.
LOGIN_WAIT = 300.0
LOGIN_POLL = 1.0

# Grace period after the CLI process exits before we declare the credential
# missing. A CLI that writes its credential and immediately exits can lose the
# race against our poll by a few milliseconds, and calling that "cancelled"
# would be a lie the user has no way to debug.
EXIT_GRACE = 3.0

# How long a CLI gets to shut down politely before it is killed, and how hard
# the sandbox removal is retried afterwards. Windows keeps a directory locked
# until the last handle inside it is closed, and a handle can outlive the
# process that owned it by a few milliseconds, so a single rmtree immediately
# after the kill loses a race it did not have to lose.
STOP_GRACE = 5.0
REMOVE_TRIES = 10
REMOVE_DELAY = 0.2


def _slug(value):
    """A usable provider slug, or None for anything that is not one.

    Every public entry point in this module funnels its provider argument
    through here before it reaches a dict lookup or a string method. The UI
    calls these functions to decide whether to enable a button, and it does
    not sanitise what it passes; an unhashable value such as a list would
    otherwise raise TypeError out of a question whose only honest answers are
    yes and no. Returning None lets each caller answer "not a provider I
    know" without any of them having to repeat the type check.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned or None


# ── every route a process can take to "the home directory" ────────────────────
# Overriding HOME alone is NOT enough, and the reason is the whole point of
# this module. Each name below is a separate, independent way for a child
# process to answer the question "where does this user live?", and a vendor
# CLI that answers it by any route we left alone finds the user's REAL home,
# finds the session already sitting there, and signs the FIRST account in
# again -- or worse, writes its new session on top of the working one. Do not
# shorten this list.
#
#   HOME             the POSIX answer, and what both CLIs use when they run
#                    inside WSL, which is where they actually run here.
#   USERPROFILE      the Windows answer. CPython's os.path.expanduser and most
#                    Windows path helpers consult it BEFORE HOME, so a
#                    Windows-side CLI that expands "~" escapes a HOME-only
#                    sandbox without even trying.
#   HOMEDRIVE
#   HOMEPATH         the legacy Windows pair, still consulted as a fallback
#                    when USERPROFILE is absent. They are a PAIR and are only
#                    meaningful concatenated, so they are derived from one
#                    splitdrive of the sandbox root rather than set
#                    independently -- setting one and not the other yields a
#                    path that is neither the sandbox nor the real home, which
#                    is the worst of the three outcomes because it silently
#                    succeeds somewhere unexpected.
#   XDG_CONFIG_HOME
#   XDG_DATA_HOME
#   XDG_STATE_HOME
#   XDG_CACHE_HOME   the freedesktop answers. A CLI that stores its session
#                    under $XDG_CONFIG_HOME rather than under $HOME/.config
#                    would otherwise write straight into the user's real
#                    configuration tree even with HOME redirected, because
#                    these variables are absolute and do not follow HOME.
_HOME_ROOT_VARS = ("HOME", "USERPROFILE")

# The XDG directories are given real subdirectories of the sandbox rather than
# the sandbox root itself, so a CLI that writes to two of them does not end up
# with its data and its config on top of each other.
_XDG_DIRS = (
    ("XDG_CONFIG_HOME", (".config",)),
    ("XDG_DATA_HOME", (".local", "share")),
    ("XDG_STATE_HOME", (".local", "state")),
    ("XDG_CACHE_HOME", (".cache",)),
)


# ── the per-provider environment override table ───────────────────────────────
# THIS TABLE IS THE ONE PLACE TO EDIT when a variable name turns out to be
# wrong. The exact names the vendors honour are still being pinned down, so
# every field below is also overridable from .env, which means a corrected
# name can ship as a configuration change with no code change at all:
#
#   home_vars   variables set to the SANDBOX ROOT. Plain HOME is the baseline
#               because relocating HOME relocates a home directory on every
#               Unix-like system, which is where both CLIs actually run here.
#               .env key: AI_LOGIN_HOME_VARS_<PROV>  (semicolon separated)
#   config_var  the provider's dedicated configuration-directory variable,
#               set to the sandbox's config dir. Some CLIs honour this and
#               ignore HOME, so both are set and the CLI may pick either.
#               .env key: AI_LOGIN_CONFIG_VAR_<PROV>   ("" disables it)
#   config_rel  the config directory, relative to the sandbox root.
#               .env key: AI_LOGIN_CONFIG_REL_<PROV>
#   cred_rel    where the credential is expected to appear, relative to the
#               sandbox root. Kept as a POSIX-style string in the table and
#               split on "/" so a .env override reads naturally on either OS.
#               .env key: AI_LOGIN_CRED_REL_<PROV>
#   cmd_key     the .env key core.py already uses for this provider's login
#               command, reused verbatim so a user configures the command in
#               exactly one place for both the old and the isolated path.
#   unset_vars  names to `unset` INSIDE the distro before the CLI runs, e.g. a
#               proxy credential the user's own ~/.bashrc exports that would
#               make the CLI believe it is already authenticated and turn the
#               login into a silent no-op. This is a REMOVAL list, unlike
#               every field above it, and only takes effect on a wsl.exe/
#               bash -c launch (see _wrap_wsl_login_command) -- that is the
#               only point downstream of the rc file, so it is also the only
#               point a removal can happen at all; a parent-side env edit
#               cannot reach a variable the rc file creates on the far side
#               of the interop boundary.
#               .env key: AI_LOGIN_UNSET_VARS_<PROV>  (semicolon separated)
#   login_suffix  words appended to the configured command's tail when it does
#               not already contain the first of them, turning a bare CLI
#               invocation that would open an interactive REPL into a real,
#               non-interactive login subcommand. Empty means the configured
#               command already names its own subcommand (true of codex's
#               default, "... codex login"). Same wsl.exe/bash -c-only scope
#               as unset_vars.
#               .env key: AI_LOGIN_SUBCOMMAND_<PROV>  (space separated)
_PROVIDER_ENV = {
    "claude": {
        "home_vars":  ("HOME",),
        "config_var": "CLAUDE_CONFIG_DIR",
        "config_rel": ".claude",
        "cred_rel":   ".claude/.credentials.json",
        "cmd_key":    "AI_LOGIN_CMD_CLAUDE",
        # `claude auth login` confirmed live against the installed CLI's own
        # --help output ("auth" -> "login": "Sign in to your Anthropic
        # account"). Bare `claude` opens the interactive REPL, and `/login`
        # is a slash command typed INSIDE that REPL -- nothing here types it,
        # so a bare invocation never performs an OAuth flow at all.
        "unset_vars":   ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                          "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
        "login_suffix": ("auth", "login"),
    },
    "codex": {
        "home_vars":  ("HOME",),
        "config_var": "CODEX_HOME",
        "config_rel": ".codex",
        "cred_rel":   ".codex/auth.json",
        "cmd_key":    "AI_LOGIN_CMD_CODEX",
        # The configured command already ends in "login" (.env), so no
        # suffix is added here; the unset list is codex's analogue of
        # claude's proxy variables, listed for the same reason -- an
        # unproven guess, not yet confirmed against a live codex CLI.
        "unset_vars":   ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
        "login_suffix": (),
    },
}


def provider_env_spec(provider):
    """The table row for a provider with .env overrides already folded in.

    Returned as a fresh dict so a caller inspecting it (the UI shows the user
    which variables an isolated login will set) cannot mutate the table.
    """
    slug = _slug(provider)
    if slug is None:
        return None
    base = _PROVIDER_ENV.get(slug)
    if not base:
        return None
    up = slug.upper()
    spec = dict(base)
    raw_home = env_str("AI_LOGIN_HOME_VARS_%s" % up, "")
    if raw_home:
        spec["home_vars"] = tuple(
            n.strip() for n in raw_home.split(";") if n.strip())
    # An explicitly empty value must be able to DISABLE the config variable,
    # so this one distinguishes "unset" from "set to empty" rather than using
    # env_str's falsy-means-default behaviour.
    key = "AI_LOGIN_CONFIG_VAR_%s" % up
    if key in ENV or key in os.environ:
        spec["config_var"] = (ENV.get(key, os.environ.get(key, "")) or "").strip()
    spec["config_rel"] = env_str("AI_LOGIN_CONFIG_REL_%s" % up, spec["config_rel"])
    spec["cred_rel"] = env_str("AI_LOGIN_CRED_REL_%s" % up, spec["cred_rel"])
    raw_unset = env_str("AI_LOGIN_UNSET_VARS_%s" % up, "")
    if raw_unset:
        spec["unset_vars"] = tuple(
            n.strip() for n in raw_unset.split(";") if n.strip())
    else:
        spec.setdefault("unset_vars", ())
    raw_suffix = env_str("AI_LOGIN_SUBCOMMAND_%s" % up, "")
    if raw_suffix:
        spec["login_suffix"] = tuple(shlex.split(raw_suffix))
    else:
        spec.setdefault("login_suffix", ())
    return spec


def _rel_parts(rel):
    """Split a table/.env relative path into components, rejecting escapes.

    A relative path out of configuration must not be able to climb out of the
    sandbox with "..", because the sandbox boundary is the only thing standing
    between this module and the user's real credential file.
    """
    parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p and p != "."]
    if any(p == ".." for p in parts) or os.path.isabs(str(rel or "")):
        raise ValueError("relative path escapes the sandbox: %r" % (rel,))
    return parts


def _home_override_env(root):
    """Every home-resolution variable, all of them pointing at the sandbox.

    Returns {name: value}. The directories the XDG names refer to are created
    here rather than left to the CLI, because a CLI that finds its configured
    config directory missing is entitled to fall back to a built-in default --
    and that default is computed from the real home on most implementations.
    """
    over = {}
    for name in _HOME_ROOT_VARS:
        over[name] = root
    # Derived from ONE splitdrive so the pair can never disagree. On a POSIX
    # root there is no drive letter, which leaves HOMEDRIVE empty and HOMEPATH
    # holding the whole path -- still a consistent pair, and still the sandbox.
    drive, tail = os.path.splitdrive(root)
    over["HOMEDRIVE"] = drive
    over["HOMEPATH"] = tail or root
    for name, parts in _XDG_DIRS:
        path = os.path.join(root, *parts)
        over[name] = path
        try:
            os.makedirs(path, mode=stat.S_IRWXU, exist_ok=True)
        except OSError:
            # A directory we could not create is still better named than left
            # pointing at the user's real one, so the variable is set anyway.
            pass
    return over


# ── the "never touch the real credentials" guard ──────────────────────────────

def _protected_roots():
    """Directories this module must never write inside, normalised.

    Four families: the real home directory, every per-provider configuration
    directory under it, the parent directory of every credential file
    aiaccounts knows how to read -- which is what covers WSL homes reached
    through the wsl.localhost share -- and this repository itself.

    One root is then dropped again, and the reason is worth stating because it
    looks like a hole: on Windows the per-user temporary directory lives
    UNDER the user profile (%LOCALAPPDATA%\\Temp), so keeping the profile in
    this list would reject every sandbox this module can legitimately create.
    Any root that is an ancestor of the temporary directory is therefore
    filtered out, and the guarantee is carried instead by the two rules that
    survive: the path must sit under the temporary directory, and it must not
    be inside any credential or configuration directory that is not itself an
    ancestor of that temporary directory. A real credential directory can
    never be an ancestor of the temp directory, so nothing real is exempted.
    """
    roots = []
    try:
        home = os.path.expanduser("~")
    except Exception:
        home = ""
    if home:
        roots.append(home)
        for spec in _PROVIDER_ENV.values():
            try:
                roots.append(os.path.join(home, *_rel_parts(spec["config_rel"])))
            except Exception:
                continue
    for var in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        val = os.environ.get(var)
        if val:
            roots.append(val)
    if _vault is not None:
        for pid in _PROVIDER_ENV:
            try:
                for path in _vault.cli_cred_paths(pid) or []:
                    roots.append(os.path.dirname(path))
            except Exception:
                continue
    roots.append(os.path.dirname(os.path.abspath(__file__)))

    try:
        tmp = os.path.abspath(tempfile.gettempdir())
    except Exception:
        tmp = ""
    out = []
    for r in roots:
        try:
            full = os.path.abspath(r)
        except Exception:
            continue
        if tmp and _is_within(tmp, full):
            continue
        out.append(os.path.normcase(full))
    return out


def _is_within(child, parent):
    """True when child is parent or lives underneath it, textually and safely.

    os.path.commonpath would raise across drive letters, which on Windows is
    the common case rather than an error, so the comparison is done on
    normalised strings with an explicit separator to stop "C:\\foo" from
    matching "C:\\foobar".
    """
    c = os.path.normcase(os.path.abspath(child))
    p = os.path.normcase(os.path.abspath(parent))
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def assert_sandbox_path_safe(path):
    """Refuse any sandbox that is not a fresh directory in temporary space.

    Raises rather than returning a flag on purpose. Every caller in this file
    is about to write into this directory and then delete it recursively; a
    boolean that someone forgets to check would be a recursive delete pointed
    at a real home directory. The failure mode of raising is a login that does
    not start, which is recoverable; the failure mode of not raising is the
    user losing the one working account he still has.
    """
    if not isinstance(path, (str, bytes, os.PathLike)):
        raise ValueError("sandbox path is not a path")
    if not path:
        raise ValueError("sandbox path is empty")
    try:
        full = os.path.abspath(path)
    except (TypeError, ValueError) as exc:
        raise ValueError("sandbox path is not usable: %s" % type(exc).__name__)
    for root in _protected_roots():
        if _is_within(full, root):
            raise ValueError(
                "refusing sandbox inside a protected directory: %s" % full)
    tmp = os.path.abspath(tempfile.gettempdir())
    if not _is_within(full, tmp):
        raise ValueError(
            "refusing sandbox outside the temporary directory: %s" % full)
    return True


# ── sandbox handle ────────────────────────────────────────────────────────────

class Sandbox(object):
    """A disposable fake home for exactly one interactive login.

    Attributes are plain data so the caller (and the UI behind it) can show
    the user precisely what an isolated login is about to do: which directory
    is being used, which variables are being overridden, and where the
    credential is expected to land.
    """

    __slots__ = ("provider", "dir", "env", "cred_path", "config_dir",
                 "proc", "_closed", "_lock", "wsl_root")

    def __init__(self, provider, directory, env, cred_path, config_dir,
                 wsl_root=None):
        self.provider = provider
        self.dir = directory
        self.env = env
        self.cred_path = cred_path
        self.config_dir = config_dir
        # The sandbox root translated to its WSL-side path ("/mnt/c/..."),
        # computed once by create_sandbox via `wslpath`. None on a machine
        # without WSL, or when the translation failed -- launch_login treats
        # that as "cannot force HOME inside the distro" and falls back to the
        # WSLENV-only behaviour rather than raising, since a Windows-only
        # provider command never needed this in the first place.
        self.wsl_root = wsl_root
        # The handle of the CLI launched into this sandbox, remembered so that
        # tear-down can stop it. Windows refuses to remove a directory while a
        # process holds a handle inside it -- and the child's working directory
        # IS the sandbox root -- so a tear-down that does not know about the
        # process cannot remove the sandbox at all on the one path where it
        # matters most, the timeout, where the CLI is still running by
        # definition and a credential may already be sitting on disk.
        self.proc = None
        self._closed = False
        self._lock = threading.Lock()

    def __repr__(self):
        return "<Sandbox %s dir=%s closed=%s>" % (
            self.provider, self.dir, self._closed)

    @property
    def closed(self):
        return self._closed


# Every live sandbox, so an interpreter exit that skips the caller's cleanup
# still removes directories holding credential material. Keyed by path because
# a sandbox may be destroyed more than once and the second call must be a
# no-op rather than an error.
_LIVE = {}
_LIVE_LOCK = threading.Lock()


# Sandboxes tear-down could not remove. Kept so the failure is VISIBLE: a
# directory holding a freshly minted credential that silently survived is the
# defect this codebase has already shipped once, and a caller that cannot
# learn about it cannot warn the user or retry.
_LEFTOVER = []
_LEFTOVER_LOCK = threading.Lock()


def leftover_sandboxes():
    """Sandbox directories tear-down could not remove, oldest first.

    Paths only -- never a credential. A non-empty result means a temporary
    directory that may contain a live refresh token is still on disk and the
    user should be told, which is why this is public rather than a log line.
    """
    with _LEFTOVER_LOCK:
        return list(_LEFTOVER)


def _note_leftover(root):
    with _LEFTOVER_LOCK:
        if root not in _LEFTOVER:
            _LEFTOVER.append(root)


def _clear_leftover(root):
    with _LEFTOVER_LOCK:
        if root in _LEFTOVER:
            _LEFTOVER.remove(root)


def _atexit_cleanup():                               # pragma: no cover
    with _LIVE_LOCK:
        boxes = list(_LIVE.values())
    for box in boxes:
        try:
            destroy_sandbox(box)
        except Exception:
            pass
    # One last attempt at anything that refused to go earlier. By exit time
    # the child processes are gone, so a removal that lost a handle race
    # during the run usually succeeds now.
    for root in leftover_sandboxes():
        try:
            if _remove_tree(root):
                _clear_leftover(root)
        except Exception:
            pass


atexit.register(_atexit_cleanup)


# ── public API ────────────────────────────────────────────────────────────────

def login_command(provider):
    """The configured login argv for a provider, or None.

    Split with shlex and returned as a list, never as a string, because the
    launcher must never see a shell. Mirrors core.ai_login_launch exactly.
    """
    spec = provider_env_spec(provider)
    if not spec:
        return None
    raw = env_str(spec["cmd_key"], "").strip()
    if not raw:
        return None
    try:
        argv = shlex.split(raw)
    except ValueError:
        return None
    return argv or None


def isolated_login_supported(provider):
    """True when an isolated login could actually run for this provider here.

    Asked BEFORE the button is enabled, so the UI can be honest rather than
    letting the user click, watch a console open, and wait five minutes for a
    timeout. Four things have to hold: the provider is in the override table,
    the provider registry considers it live rather than planned, a login
    command is configured, and temporary space is usable.
    """
    slug = _slug(provider)
    if slug is None or slug not in _PROVIDER_ENV:
        # Deliberately an answer rather than an exception. This is asked about
        # whatever the UI happens to be holding -- None for "nothing selected",
        # a dict from a half-parsed row -- and a question about capability
        # must be total: anything unrecognised simply has no isolated login.
        return False
    if _prov is not None:
        try:
            meta = _prov.provider(slug) or {}
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if (meta.get("status") or "planned") != "live":
            return False
    if not login_command(slug):
        return False
    try:
        probe = tempfile.mkdtemp(prefix="ailogin-probe-")
    except Exception:
        return False
    try:
        assert_sandbox_path_safe(probe)
    except Exception:
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    return True


def create_sandbox(provider):
    """Make the disposable home and return the handle describing it.

    The directory is created by tempfile with mode 0700 already applied, the
    safety assertion runs before a single byte is written into it, and the
    handle is registered for atexit tear-down immediately -- registering after
    the CLI is launched would leave a window where a crash strands a live
    credential on disk.
    """
    spec = provider_env_spec(provider)
    if not spec:
        raise ValueError("unknown provider: %s" % (_slug(provider) or "?"))
    provider = _slug(provider)
    cred_parts = _rel_parts(spec["cred_rel"])
    cfg_parts = _rel_parts(spec["config_rel"])
    if not cred_parts:
        raise ValueError("no credential path configured for %s" % provider)

    root = tempfile.mkdtemp(prefix="ailogin-%s-" % provider)
    try:
        assert_sandbox_path_safe(root)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    try:
        os.chmod(root, stat.S_IRWXU)
    except OSError:
        # Windows honours very little of the POSIX mode. The directory is
        # already inside a per-user temporary location, so this is a
        # best-effort tightening rather than the only protection.
        pass

    config_dir = os.path.join(root, *cfg_parts) if cfg_parts else root
    cred_path = os.path.join(root, *cred_parts)
    for d in (config_dir, os.path.dirname(cred_path)):
        try:
            os.makedirs(d, mode=stat.S_IRWXU, exist_ok=True)
        except OSError:
            pass

    # The overrides start from the caller's environment so the CLI still
    # finds its interpreter, its PATH and its proxy settings; only the
    # location-of-home keys are replaced.
    env = dict(os.environ)
    names = []
    # The universal set first: every variable through which any process on
    # either platform can resolve a home directory, all aimed at the sandbox.
    for name, value in _home_override_env(root).items():
        env[name] = value
        names.append(name)
    # Then the provider row, which may name additional variables or may
    # re-state one of the above; either way the sandbox root is the value.
    for name in spec["home_vars"]:
        if not isinstance(name, str) or not name:
            continue
        env[name] = root
        if name not in names:
            names.append(name)
    if spec["config_var"]:
        env[spec["config_var"]] = config_dir
        names.append(spec["config_var"])
    # A Windows launcher reaches the CLI through the WSL interop executable,
    # and variables set on the Windows side do not cross into the distro
    # unless they are named in WSLENV. The "/p" flag asks the interop layer to
    # translate the value from a Windows path to a Linux one, which is exactly
    # what these variables hold. Without this the overrides would be silently
    # dropped and the CLI would read the user's real home -- the precise
    # failure this module exists to prevent.
    if os.name == "nt" and names:
        existing = [p for p in (env.get("WSLENV") or "").split(":") if p]
        for name in names:
            # HOMEDRIVE and HOMEPATH are excluded on purpose: neither holds a
            # complete path, so asking the interop layer to translate them
            # would produce two mangled fragments. They exist for a
            # Windows-side CLI, and a Windows-side CLI reads them directly.
            if name in ("HOMEDRIVE", "HOMEPATH"):
                continue
            entry = "%s/p" % name
            if entry not in existing:
                existing.append(entry)
        env["WSLENV"] = ":".join(existing)

    # WSLENV carries CLAUDE_CONFIG_DIR and the XDG vars across the interop
    # boundary correctly (measured; see a2-sandbox.md) -- but NOT HOME. WSL's
    # interop layer resets HOME from /etc/passwd on the far side regardless of
    # HOME/p, in both login and non-login shells, so the sandbox's baseline
    # isolation variable is silently dropped on every wsl.exe launch. The only
    # remaining place to set it is inside the distro, in the command line
    # itself (see _wrap_wsl_login_command), which needs the sandbox root
    # already translated to its WSL-side path. That translation is done ONCE,
    # here, rather than inside launch_login: calling `wslpath` on a value that
    # WSLENV's own /p flag has already translated reproduces the doubled
    # "/mnt/c/mnt/c/..." artifact seen during diagnosis, so this must be the
    # single source of truth for the WSL-side path, computed from the
    # UNtranslated Windows root exactly once.
    wsl_root = None
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["wsl.exe", "wslpath", "-a", "-u", root.replace("\\", "/")],
                capture_output=True, text=True, timeout=10)
            candidate = (out.stdout or "").strip()
            if out.returncode == 0 and candidate:
                wsl_root = candidate
        except Exception:
            # No WSL installed, wslpath missing, or the call simply failed.
            # launch_login treats a None wsl_root as "cannot force HOME
            # in-distro" and leaves the WSLENV-only behaviour in place rather
            # than raising -- a provider command that never targets wsl.exe
            # never needed this value anyway.
            wsl_root = None

    box = Sandbox(provider, root, env, cred_path, config_dir, wsl_root=wsl_root)
    with _LIVE_LOCK:
        _LIVE[os.path.normcase(root)] = box
    return box


def _is_wsl_launcher(argv):
    """True when argv[0] names the WSL interop executable."""
    if not argv:
        return False
    head = os.path.basename(str(argv[0])).lower()
    return head in ("wsl", "wsl.exe")


def _wrap_wsl_login_command(provider, argv, wsl_root):
    """Rewrite a `wsl.exe ... bash -c<flags> "<cmd>"` argv so the in-distro
    command scrubs interfering vars and sets HOME itself, and carries the
    provider's real login subcommand.

    Returns argv unchanged when the shape is not recognised -- a provider
    command that does not go through `bash -c*` gets none of this, which is
    strictly the old behaviour rather than a broken new one; this function
    only ever adds isolation, it never removes the caller's ability to run a
    plain command.

    WHY this happens in the command line rather than the environment: both
    defects it fixes live downstream of things a Windows parent process
    cannot reach. `~/.bashrc` re-creates ANTHROPIC_* variables INSIDE the
    distro during shell start-up, after `bash -l` sources it -- a parent-side
    env dict never contained them to begin with, so there is nothing there to
    delete. And WSL's interop layer resets HOME from /etc/passwd on the far
    side of the boundary regardless of what WSLENV carries (measured; see
    a2-sandbox.md) -- so HOME can only be forced from inside the distro too.
    Both fixes therefore have to be text baked into the command bash runs,
    executed AFTER the rc file's own exports, which is exactly what placing
    them in the trailing command string achieves.
    """
    if not _is_wsl_launcher(argv):
        return argv
    try:
        dashdash = argv.index("--")
    except ValueError:
        return argv
    rest = argv[dashdash + 1:]
    if not rest:
        return argv
    shell = os.path.basename(str(rest[0])).lower()
    if shell not in ("bash", "bash.exe"):
        return argv
    # The flags word (e.g. "-lic") must select -c, because everything after
    # it is a single command STRING bash hands to the shell, not a further
    # arg list -- rewriting anything else would silently break argv shapes
    # this function was never designed to parse.
    if len(rest) < 3:
        return argv
    flags = rest[1]
    if not isinstance(flags, str) or "c" not in flags:
        return argv
    # Everything after the flags word is the command bash receives. Normally
    # this is exactly one element (a single shlex token from .env, whether or
    # not it happened to contain spaces); joining is a safety margin for a
    # trailing-args edge case rather than the expected input shape.
    base_cmd = " ".join(str(p) for p in rest[2:])

    spec = provider_env_spec(provider) or {}
    unset_vars = spec.get("unset_vars") or ()
    suffix = spec.get("login_suffix") or ()

    cmd = base_cmd
    if suffix:
        # Appended only when the command does not already end in the login
        # subcommand -- a user who has already customised the command in
        # .env to include it must not get it duplicated onto the tail.
        cmd_words = shlex.split(cmd) if cmd else []
        suffix_words = list(suffix)
        already = (len(cmd_words) >= len(suffix_words)
                   and cmd_words[-len(suffix_words):] == suffix_words)
        if not already:
            cmd = cmd + " " + " ".join(shlex.quote(w) for w in suffix_words)

    prefix = ""
    if unset_vars:
        # Runs AFTER `bash -l` has already sourced the rc file (that is the
        # only reason ANTHROPIC_* exists on this side at all), so the unset
        # actually removes what the rc file just created -- a parent-side env
        # edit could never do this, see the function docstring.
        prefix += "unset " + " ".join(shlex.quote(v) for v in unset_vars) + "; "
    if wsl_root:
        # Set INSIDE the distro because WSLENV cannot carry HOME across the
        # interop boundary at all (measured; see a2-sandbox.md). wsl_root is
        # the WSL-side path computed once in create_sandbox from the
        # untranslated Windows root, so this cannot double-translate into
        # "/mnt/c/mnt/c/...".
        prefix += "env HOME=%s " % shlex.quote(wsl_root)

    wrapped = prefix + cmd
    return list(argv[:dashdash + 1]) + list(rest[:2]) + [wrapped]


def launch_login(provider, sandbox, argv=None, console=True):
    """Start the provider's interactive login inside the sandbox.

    Returns the Popen handle. The console is VISIBLE and must stay that way:
    the login is a browser round-trip with a code the user pastes back, so a
    hidden window would leave him staring at a widget that never finishes,
    with the prompt he needs to answer sitting in a console he cannot see.

    argv is accepted for tests only -- it lets the self-test drive a harmless
    local script through the same code path without contacting a vendor. In
    production the command comes from .env, is split with shlex, and is
    launched with an argument list and no shell, so a value in .env cannot
    append a second command.
    """
    if sandbox is None or sandbox.closed:
        raise ValueError("sandbox is not usable")
    if sandbox.provider != provider:
        raise ValueError("sandbox belongs to provider %s, not %s"
                         % (sandbox.provider, provider))
    # Re-checked here rather than trusted from creation time: this is the last
    # moment before another process is pointed at the directory.
    assert_sandbox_path_safe(sandbox.dir)

    args = list(argv) if argv else login_command(provider)
    if not args:
        raise ValueError("no login command configured for %s" % provider)
    # Only the production path (argv taken from .env) is rewritten. The
    # self-test drives a throwaway local Python script through an explicit
    # argv precisely so it can prove this module's plumbing without a vendor
    # CLI in the loop; rewriting that argv as if it were a wsl.exe/bash -c
    # command would be a no-op in practice (_wrap_wsl_login_command already
    # ignores anything that is not shaped like one) but would also defeat the
    # point of a caller-supplied argv being trusted verbatim.
    if argv is None:
        args = _wrap_wsl_login_command(provider, args, sandbox.wsl_root)

    # tempfile.gettempdir() rather than sandbox.dir: Windows refuses to
    # delete a directory that is a live process's working directory, and on
    # the timeout path the CLI is -- by definition -- still running when
    # destroy_sandbox tries to remove it. Using a cwd outside the sandbox
    # entirely removes that race instead of relying solely on killing the
    # process first.
    kwargs = {"env": sandbox.env, "cwd": tempfile.gettempdir()}
    if os.name == "nt" and console:
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    proc = subprocess.Popen(args, **kwargs)
    # Remembered on the handle so tear-down can stop it without the caller
    # having to thread the process back in. The caller may still pass its own
    # handle to destroy_sandbox; this is the safety net for the paths where it
    # does not, which includes the atexit path where there is no caller left.
    sandbox.proc = proc
    return proc


def read_sandbox_cred(sandbox):
    """The parsed credential sitting in the sandbox, or None.

    None covers three cases that are all "not yet": no file, a file still
    being written and therefore unparseable, and a parsed document whose
    shape the provider registry does not recognise. Treating a half-written
    file as a failure would abort logins that were about to succeed.
    """
    path = getattr(sandbox, "cred_path", None)
    if not path or not isinstance(path, str):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not doc:
        return None
    if _vault is not None:
        try:
            if not _vault.cred_fingerprint(getattr(sandbox, "provider", None),
                                           doc):
                return None
        except Exception:
            return None
    return doc


def wait_for_cred(sandbox, proc=None, timeout=LOGIN_WAIT, poll=LOGIN_POLL):
    """Block until the credential appears in the sandbox.

    Returns (cred, reason). On success cred is the parsed document and reason
    is "ok". On failure cred is None and reason names WHICH failure, because
    the three are not interchangeable to a user deciding what to do next:

      "cancelled"   the CLI exited cleanly without writing a credential, so
                    the user backed out of the browser flow.
      "cli-error: exit N"
                    the CLI exited non-zero, so the command itself failed --
                    a wrong login command in .env looks like this.
      "timeout after Ns"
                    nothing exited and nothing appeared within the window.

    Never returns a token in the reason string.
    """
    if sandbox is None or not getattr(sandbox, "cred_path", ""):
        return None, "no sandbox"
    try:
        timeout = max(1.0, float(timeout))
    except (TypeError, ValueError):
        timeout = LOGIN_WAIT
    try:
        poll = max(0.05, float(poll))
    except (TypeError, ValueError):
        poll = LOGIN_POLL
    deadline = time.monotonic() + timeout
    exited_at = None
    while True:
        cred = read_sandbox_cred(sandbox)
        if cred is not None:
            return cred, "ok"
        try:
            gone = proc is not None and proc.poll() is not None
        except Exception:
            gone = False
        if gone:
            # The CLI is gone. Keep polling for a short grace period: writing
            # the file and exiting is not atomic and we may simply have looked
            # a few milliseconds too early.
            if exited_at is None:
                exited_at = time.monotonic()
            elif time.monotonic() - exited_at >= EXIT_GRACE:
                code = getattr(proc, "returncode", None)
                if code == 0:
                    return None, "cancelled"
                return None, "cli-error: exit %s" % code
        if time.monotonic() >= deadline:
            return None, "timeout after %ds" % int(timeout)
        time.sleep(poll)


def _shred(path):
    """Overwrite a credential file before unlinking it, best effort.

    Not a guarantee on a journalling or copy-on-write filesystem, and not
    claimed to be one. It is cheap, it removes the plaintext from the obvious
    place, and the file is small.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    try:
        with open(path, "r+b") as fh:
            fh.write(b"\0" * size)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass


def _stop_process(proc, grace=STOP_GRACE):
    """Ask a CLI to exit, then insist. True when it is definitely gone.

    Politeness first, because a CLI killed mid-write can leave a half-written
    credential that the next poll would read as corrupt. But politeness has a
    deadline: a login CLI sitting on a prompt nobody is going to answer will
    never exit on its own, and while it lives the sandbox cannot be removed.
    """
    if proc is None:
        return True
    try:
        if proc.poll() is not None:
            return True
    except Exception:
        return False
    for stop in (getattr(proc, "terminate", None), getattr(proc, "kill", None)):
        if stop is None:
            continue
        try:
            stop()
        except Exception:
            # Already dead, or a handle we are not allowed to signal. Either
            # way the wait below is the thing that decides.
            pass
        try:
            proc.wait(timeout=grace)
            return True
        except Exception:
            continue
    try:
        return proc.poll() is not None
    except Exception:
        return False


def _remove_tree(root):
    """Shred then remove the sandbox, retrying while Windows lets go.

    A handle can outlive the process that held it by a moment, so the first
    removal after a kill can fail on a directory that becomes removable a
    fraction of a second later. Retrying a bounded number of times costs two
    seconds in the worst case and turns an intermittent leaked credential into
    a reliable removal.
    """
    if not root or not isinstance(root, str):
        return False
    for attempt in range(REMOVE_TRIES):
        if not os.path.exists(root):
            return True
        try:
            for cur, _dirs, files in os.walk(root):
                for name in files:
                    _shred(os.path.join(cur, name))
        except OSError:
            pass
        try:
            shutil.rmtree(root)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return True
        if not os.path.exists(root):
            return True
        if attempt + 1 < REMOVE_TRIES:
            time.sleep(REMOVE_DELAY)
    return not os.path.exists(root)


def destroy_sandbox(sandbox, proc=None):
    """Remove the sandbox and everything in it. Safe to call more than once.

    Tear-down has to survive the failure paths as well as the happy one -- a
    cancelled login, a killed CLI, a timeout with the CLI still running, an
    exception in the caller -- so this is idempotent, never raises, and is
    also wired to atexit. A temporary directory left behind holding a live
    refresh token is the defect this codebase has already shipped once.

    The sequence is: stop the process that was launched into this sandbox
    (terminate, wait briefly, escalate to kill), then remove the directory,
    retrying while the operating system releases the last handle. The removal
    runs from a finally block so that no failure while stopping the process --
    including an exception from a handle we are not allowed to signal -- can
    skip it.

    Returns True when nothing is left on disk. A False result is not cosmetic:
    it means a directory that may hold a credential survived, and the path is
    recorded in leftover_sandboxes() so the caller can say so out loud.
    """
    if sandbox is None:
        return False
    lock = getattr(sandbox, "_lock", None)
    root = getattr(sandbox, "dir", None)
    if lock is None or not isinstance(root, str) or not root:
        return False
    with lock:
        if getattr(sandbox, "_closed", False) and not os.path.exists(root):
            return True
        try:
            assert_sandbox_path_safe(root)
        except Exception:
            # Cannot prove this path is disposable, so do not delete it. The
            # registration is dropped so atexit stops retrying, and the path
            # is surfaced rather than forgotten: something is very wrong if a
            # sandbox handle points outside temporary space.
            sandbox._closed = True
            with _LIVE_LOCK:
                _LIVE.pop(os.path.normcase(root), None)
            _note_leftover(root)
            return False
        try:
            _stop_process(proc if proc is not None else
                          getattr(sandbox, "proc", None))
        finally:
            removed = _remove_tree(root)
            sandbox._closed = True
            try:
                sandbox.proc = None
            except Exception:
                pass
            with _LIVE_LOCK:
                _LIVE.pop(os.path.normcase(root), None)
            if removed:
                _clear_leftover(root)
            else:
                _note_leftover(root)
        return removed


def _fingerprint(provider, cred):
    """aiaccounts' non-secret fingerprint, or None, never raising.

    Identity is deliberately NOT derived from the token. Tokens rotate on
    every refresh, so a token-based identity would report the same account as
    a brand new one after each refresh and quietly fill the vault with copies
    of one login. aiaccounts.cred_fingerprint pairs the stable fields instead:
    subscription type with expiry for Anthropic, the account identifier for
    OpenAI.
    """
    if _vault is None:
        return None
    slug = _slug(provider)
    if slug is None or not isinstance(cred, dict):
        return None
    try:
        fp = _vault.cred_fingerprint(slug, cred)
    except Exception:
        return None
    return fp or None


def classify_account(provider, cred, accounts=None):
    """Is this freshly captured login a NEW account, or the same one again?

    This is the question the whole feature turns on. The user's complaint is
    that repeating the login gives him the same account back; if the widget
    registered that silently he would end up with two rows that are one
    account and still no second account. So the answer is computed and
    reported rather than assumed.

    The provider slug comes first because the fingerprint is provider-specific
    and cannot be computed without it. A caller that only wants the yes-or-no
    answer should use is_duplicate_account(cred, accounts) rather than reading
    fields out of the structure below -- and because a contract-shaped call
    with the credential in the first slot would otherwise bind one argument
    over and return a confident wrong answer, that case is detected here and
    the arguments are put back in the order they were plainly meant in.

    Returns a dict:
      status       "new" | "duplicate" | "unknown"
      duplicate    True/False for the same question, for callers that only
                   want a flag; False whenever the status is "unknown", since
                   an unrecognisable credential is not a proven match
      fingerprint  the non-secret fingerprint, or None
      account_id   the id of the already-registered account, when duplicate
      label        that account's label, when duplicate
      detail       a sentence safe to show the user

    No token value ever reaches any of those fields.
    """
    # A caller written to the (cred, accounts) contract lands its credential
    # in the provider slot. That is unmistakable -- a provider slug is a short
    # string and a credential is a dict -- so it is corrected rather than
    # answered wrongly.
    if isinstance(provider, dict) and _slug(cred) is not None:
        provider, cred, accounts = cred, provider, accounts

    out = {"status": "unknown", "duplicate": False, "fingerprint": None,
           "account_id": None, "label": None, "detail": ""}
    slug = _slug(provider)
    if _vault is None:
        out["detail"] = "account vault unavailable; cannot compare"
        return out
    fp = _fingerprint(slug, cred)
    if not fp:
        out["detail"] = ("credential shape not recognised for %s; cannot tell "
                         "whether this is a new account" % (slug or "?"))
        return out
    out["fingerprint"] = fp
    if accounts is None:
        try:
            accounts = _vault.list_accounts() or []
        except Exception:
            accounts = []
    if isinstance(accounts, dict) or isinstance(accounts, (str, bytes)):
        # A single row, or something that is not a list of rows at all.
        # Iterating a dict would yield its keys and iterating a string its
        # characters, and either would silently compare nothing.
        accounts = [accounts] if isinstance(accounts, dict) else []
    try:
        rows = list(accounts)
    except TypeError:
        rows = []
    for acct in rows:
        if not isinstance(acct, dict):
            continue
        if _slug(acct.get("provider")) != slug:
            continue
        other = _fingerprint(slug, acct.get("cred"))
        if other and other == fp:
            out["status"] = "duplicate"
            out["duplicate"] = True
            out["account_id"] = acct.get("id")
            out["label"] = acct.get("label")
            out["detail"] = ("this is the account already registered as %r; "
                             "the CLI signed the same account in again"
                             % (acct.get("label") or acct.get("id") or "?"))
            return out
    out["status"] = "new"
    out["detail"] = "a different account from every one already registered"
    return out


def is_duplicate_account(cred, accounts=None, provider=None):
    """Plain yes-or-no: has this credential's account already been registered?

    The companion to classify_account for callers that do not need the detail.
    Answers False when the credential cannot be fingerprinted at all, because
    the two mistakes do not cost the same: a missed duplicate adds a row the
    user deletes in one click, while a false duplicate refuses the genuinely
    new account he is trying to add and leaves him exactly where he started.

    The provider is inferred from the credential's own shape when it is not
    given, so the yes-or-no question really can be asked with just the two
    arguments the contract describes.
    """
    slug = _slug(provider)
    if slug is None:
        slug = _infer_provider(cred)
    if slug is None:
        return False
    return classify_account(slug, cred, accounts).get("duplicate") is True


def _infer_provider(cred):
    """Which provider wrote this credential, judged only by its shape.

    Non-secret keys only -- the presence of "claudeAiOauth" or of a "tokens"
    object is what distinguishes the two formats, and neither test looks at a
    value.
    """
    if not isinstance(cred, dict):
        return None
    for slug in _PROVIDER_ENV:
        if _fingerprint(slug, cred):
            return slug
    return None


# ── self-test ─────────────────────────────────────────────────────────────────
# Run with:  python ailogin.py
#
# The vendor OAuth flow is deliberately NOT exercised. This project runs from a
# geo-blocked region behind a fail-closed geographic gate, and an ungated
# authentication request risks the account itself -- so the test drives the
# exact same launch/capture/teardown path with a throwaway local script that
# writes a credential-shaped document into the sandbox and exits. What is
# proven here is this module's plumbing; what is not proven here, and cannot
# be from this machine, is any vendor's real sign-in.

_FAKE_CLI = '''
import json, os, sys

mode = sys.argv[1]
home = os.environ.get("HOME") or ""
rel = sys.argv[2]
if mode == "cancel":
    sys.exit(0)
if mode == "fail":
    sys.exit(3)
path = os.path.join(home, *rel.split("/"))
os.makedirs(os.path.dirname(path), exist_ok=True)
doc = {"claudeAiOauth": {"accessToken": "not-a-real-token",
                         "refreshToken": "not-a-real-token",
                         "subscriptionType": sys.argv[3],
                         "expiresAt": int(sys.argv[4])}}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(doc, fh)
'''


def _selftest():                                     # pragma: no cover
    ok = [0]
    bad = [0]

    def check(name, cond, extra=""):
        if cond:
            ok[0] += 1
            print("  PASS  %s %s" % (name, extra))
        else:
            bad[0] += 1
            print("  FAIL  %s %s" % (name, extra))

    print("ailogin self-test (no vendor login is performed)")

    print("\n[1] override table")
    spec = provider_env_spec("claude")
    check("claude row present", spec is not None)
    check("HOME is a home var", "HOME" in (spec or {}).get("home_vars", ()))
    check("config var named", bool((spec or {}).get("config_var")))
    check("cred_rel set", bool((spec or {}).get("cred_rel")),
          "-> %s" % (spec or {}).get("cred_rel"))
    check("unknown provider rejected", provider_env_spec("nope") is None)

    print("\n[2] sandbox path guard")
    home = os.path.expanduser("~")
    for label, path in (("real home", home),
                        ("home config dir", os.path.join(home, ".claude")),
                        ("repo dir", os.path.dirname(os.path.abspath(__file__))),
                        ("non-temp path", os.path.join(home, "x", "y"))):
        try:
            assert_sandbox_path_safe(path)
            check("refuses %s" % label, False, "(it did NOT refuse)")
        except ValueError:
            check("refuses %s" % label, True)
    try:
        _rel_parts("../../.claude/.credentials.json")
        check("refuses .. in cred_rel", False)
    except ValueError:
        check("refuses .. in cred_rel", True)

    print("\n[3] sandbox creation")
    box = create_sandbox("claude")
    try:
        check("dir exists", os.path.isdir(box.dir), "-> %s" % box.dir)
        check("dir is in temp",
              _is_within(box.dir, tempfile.gettempdir()))
        # NOT "outside the home directory": on Windows the per-user temp dir
        # is legitimately inside the profile, so that would be the wrong
        # invariant. What must hold is that the sandbox is outside every real
        # credential and configuration directory, and that the guard still
        # refuses the home directory itself.
        cfgdir = os.path.join(home, provider_env_spec("claude")["config_rel"])
        check("dir is outside the real config dir",
              not _is_within(box.dir, cfgdir))
        if _vault is not None:
            creds = _vault.cli_cred_paths("claude") or []
            check("dir is outside every known cred dir",
                  not any(_is_within(box.dir, os.path.dirname(p)) for p in creds),
                  "(%d checked)" % len(creds))
        check("HOME override points at sandbox", box.env.get("HOME") == box.dir)
        check("USERPROFILE override points at sandbox",
              box.env.get("USERPROFILE") == box.dir)
        check("HOMEDRIVE+HOMEPATH rejoin to the sandbox",
              (box.env.get("HOMEDRIVE", "") + box.env.get("HOMEPATH", ""))
              == box.dir)
        check("XDG config dir is inside the sandbox",
              _is_within(box.env.get("XDG_CONFIG_HOME", ""), box.dir))
        check("cred path inside sandbox", _is_within(box.cred_path, box.dir),
              "-> %s" % os.path.relpath(box.cred_path, box.dir))
        if os.name != "nt":
            mode = stat.S_IMODE(os.stat(box.dir).st_mode)
            check("mode is 0700", mode == 0o700, "-> %o" % mode)
        else:
            check("WSLENV carries HOME",
                  "HOME/p" in (box.env.get("WSLENV") or ""),
                  "-> %s" % box.env.get("WSLENV"))
        check("no credential yet", read_sandbox_cred(box) is None)

        print("\n[4] capture through a FAKE login command")
        fake = os.path.join(tempfile.gettempdir(), "ailogin_fakecli.py")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write(_FAKE_CLI)
        rel = provider_env_spec("claude")["cred_rel"]
        argv = [sys.executable, fake, "ok", rel, "pro", "1893456000000"]
        proc = launch_login("claude", box, argv=argv, console=False)
        cred, why = wait_for_cred(box, proc, timeout=20, poll=0.2)
        check("credential captured", cred is not None, "reason=%s" % why)
        check("reason is ok", why == "ok")
        check("landed at the expected path", os.path.isfile(box.cred_path))
        check("no token in the reason string", "not-a-real-token" not in why)

        print("\n[5] duplicate detection")
        if _vault is None:
            print("  SKIP  aiaccounts not importable")
        else:
            res = classify_account("claude", cred, accounts=[])
            check("empty vault -> new", res["status"] == "new",
                  "detail=%s" % res["detail"])
            existing = [{"id": "abc123", "label": "work",
                         "provider": "claude", "cred": cred}]
            res = classify_account("claude", cred, accounts=existing)
            check("same fingerprint -> duplicate", res["status"] == "duplicate",
                  "id=%s" % res["account_id"])
            other = json.loads(json.dumps(cred))
            other["claudeAiOauth"]["expiresAt"] = 1893456999000
            res = classify_account("claude", other, accounts=existing)
            check("different fingerprint -> new", res["status"] == "new")
            res = classify_account("claude", {"garbage": 1}, accounts=existing)
            check("unrecognised shape -> unknown", res["status"] == "unknown")

        print("\n[6] tear-down")
        path = box.dir
        check("destroy returns True", destroy_sandbox(box) is True)
        check("directory is gone", not os.path.exists(path))
        check("second destroy is a no-op", destroy_sandbox(box) is True)
        check("unregistered from atexit",
              os.path.normcase(path) not in _LIVE)
    finally:
        destroy_sandbox(box)

    print("\n[7] cancelled and failed logins")
    fake = os.path.join(tempfile.gettempdir(), "ailogin_fakecli.py")
    rel = provider_env_spec("claude")["cred_rel"]
    for mode, expect in (("cancel", "cancelled"), ("fail", "cli-error: exit 3")):
        b = create_sandbox("claude")
        try:
            p = launch_login("claude", b,
                             argv=[sys.executable, fake, mode, rel, "pro", "0"],
                             console=False)
            cred, why = wait_for_cred(b, p, timeout=20, poll=0.2)
            check("%s -> no credential" % mode, cred is None)
            check("%s -> %r" % (mode, expect), why == expect, "got %r" % why)
        finally:
            destroy_sandbox(b)
    try:
        os.remove(fake)
    except OSError:
        pass

    print("\n[8] supported-provider probe")
    print("  info  claude supported here: %s" % isolated_login_supported("claude"))
    print("  info  codex  supported here: %s" % isolated_login_supported("codex"))
    check("unknown provider unsupported",
          isolated_login_supported("nope") is False)

    print("\n%d passed, %d failed" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


if __name__ == "__main__":                           # pragma: no cover
    raise SystemExit(_selftest())
