"""Per-provider polling policy for the AI plan-usage endpoints.

Net Watch shows how much of each AI subscription has been used. The numbers
come from undocumented "usage" endpoints that the vendors built for their own
clients. No vendor publishes a rate limit for them, and getting one wrong does
not just fail a request: it can leave the account answering 429 for hours, and
that also breaks the vendor's own tools on the same machine. This module decides
when each account may be asked, and how long to stay quiet after a refusal.
It makes no network calls; callers claim a key before a request and report the
outcome afterwards.

Why these numbers
-----------------
codex (ChatGPT ``backend-api/wham/usage``): every 60 s.
    The official Codex TUI polls this endpoint every 60 s itself
    (openai/codex ``ChatWidget::prefetch_rate_limits``, issue #10869), and no
    report of a 429 at that cadence was found. 60 s is also the floor: we never
    ask more often than the first-party client does.

claude (Anthropic ``api/oauth/usage``): every 180 s, floor 120 s.
    anthropics/claude-code#30930 (still open) reports persistent 429s at 30,
    60 and 120 s polling, with ``Retry-After: 0`` that does not mean "retry
    now". Production measurements from another poller found 7 reads per 10 min
    gave 429 on 29% of polls and 3-4 reads per 10 min gave 1-2%. 60 s is 10
    reads per 10 min, so it is unsafe; 180 s is about 3.3 reads per 10 min.

Every other provider: every 300 s, floor 120 s.
    There is no evidence either way, so the conservative choice is the one
    that cannot hurt the account.

Rate-limit backoff starts at 300 s for claude (doubling to a 1800 s cap) and
120 s for codex (doubling to the 900 s default cap). A server Retry-After is
honoured when it is longer than our own backoff (clamped to 6 h), but a value of
0 or a missing header never shortens anything. An existing hold is never
shortened by a later event, and holds are persisted to disk, so restarting the
widget cannot be used to escape one. A 403 that comes back as an HTML or
Cloudflare challenge holds the key for an hour.

A rate limit belongs to the account, not to the name we file it under: the same
login can be read through the CLI credential file ("legacy" key) and through the
vault. So a 429 or a block on any key holds every key of that provider.

The manual refresh button never overrides a hold, and never asks one key more
often than the provider floor (30 s at the least). A request that never reached
the vendor (offline) costs no rate limit and creates no hold. A login the vendor
refuses is retried hourly, not at the poll cadence, until the user logs in.

Every public function is thread-safe and never raises: a policy bug must not
take the widget down, so failures fall back to "not due" style answers or the
built-in defaults.
"""

import datetime
import email.utils
import json
import math
import os
import random
import shutil
import sys
import tempfile
import threading
import time

DEFAULT_INTERVAL = {"claude": 180, "codex": 60}
OTHER_INTERVAL = 300
FLOOR = {"claude": 120, "codex": 60}
OTHER_FLOOR = 120
CEIL = 3600
MANUAL_MIN = 30
JITTER = 0.10
RL_BASE = {"claude": 300, "codex": 120}
OTHER_RL_BASE = 300
RL_CAP = {"claude": 1800}
OTHER_RL_CAP = 900
BLOCKED_HOLD = 3600  # 403 + HTML/Cloudflare challenge
ERROR_CAP = 900  # generic error backoff cap
LOGIN_DEAD_HOLD = 3600  # vendor refused the grant; only a re-login fixes it
INFLIGHT_TTL = 120  # a claim with no recorded outcome expires after this

RETRY_AFTER_MAX = 6 * 3600  # longest server Retry-After we will honour
NEXT_WAKE_FLOOR = 5
STATE_VERSION = 1  # the "_v" marker that says "this file is ours"
_MAX_STATE_BYTES = 1 << 20
_MAX_EXPONENT = 20  # 2**20 is far past every cap; stops float overflow
_ALIASES = {"chatgpt": "codex", "openai": "codex", "anthropic": "claude"}
_ENV_NAMES = {"codex": "AI_POLL_SECONDS", "claude": "AI_POLL_SECONDS_CLAUDE"}
_ENV_OTHER = "AI_POLL_SECONDS_OTHER"
_FIELDS = ("last", "next_at", "hold_until", "strikes", "inflight_until")
# Hold kinds that mean the vendor is refusing the account itself, so they hold
# every key of the provider. A dead login or a generic error stays per key.
_PROVIDER_WIDE_KINDS = ("rate_limited", "blocked")

_lock = threading.RLock()
_state = {}
_provider_holds = {}  # provider -> hold_until imported or kept from a dropped key
_loaded = False
_path_override = None
_resolved_path = None
_distrust_since = 0.0  # load time of a corrupt file; 0 when the file was sane
_warned = set()


def _norm(provider):
    """Lower-case provider id with known aliases folded (chatgpt -> codex)."""
    name = str(provider or "").strip().lower()
    return _ALIASES.get(name, name)


def _now(now):
    """Wall-clock seconds; wall clock because holds survive restarts."""
    if isinstance(now, (int, float)) and not isinstance(now, bool) and math.isfinite(now):
        return float(now)
    return time.time()


def _finite(value):
    """Return value as a finite float, or None."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _warn_once(tag, message):
    """One line on stderr per tag per process; never carries a secret."""
    if tag in _warned:
        return
    _warned.add(tag)
    try:
        sys.stderr.write("aipolicy: %s\n" % message)
        sys.stderr.flush()
    except Exception:
        pass


def _env_value(name, env):
    """Read name from the given mapping first, then from os.environ."""
    if env is not None:
        try:
            value = env.get(name)
        except Exception:
            value = None
        if value not in (None, ""):
            return value
    return os.environ.get(name)


def _floor(provider):
    return FLOOR.get(provider, OTHER_FLOOR)


def _interval(provider, env=None):
    provider = _norm(provider)
    default = DEFAULT_INTERVAL.get(provider, OTHER_INTERVAL)
    raw = _env_value(_ENV_NAMES.get(provider, _ENV_OTHER), env)
    seconds = _finite(str(raw).strip()) if raw is not None else None
    if seconds is None:
        seconds = default
    return float(min(max(seconds, _floor(provider)), CEIL))


def interval(provider, env=None):
    """Seconds between scheduled polls of provider, env overrides clamped.

    env is an optional mapping (core's parsed .env) consulted before
    os.environ, because core does not export .env values into the process.
    """
    try:
        return _interval(provider, env)
    except Exception:
        return float(OTHER_INTERVAL)


def manual_floor(provider):
    """Shortest gap between two manual refreshes of one key of provider.

    The provider floor, never below MANUAL_MIN: a 30 s button on an endpoint
    whose evidence-based floor is 120 s would earn exactly the 429 the floor
    exists to avoid.
    """
    try:
        return float(max(MANUAL_MIN, _floor(_norm(provider))))
    except Exception:
        return float(max(MANUAL_MIN, OTHER_FLOOR))


def parse_retry_after(value, now=None):
    """Seconds from a Retry-After value (seconds or HTTP-date), else None.

    Zero, negative, past dates and anything unparsable return None, because
    Anthropic sends ``Retry-After: 0`` on a 429 that is anything but over.
    The result is clamped to RETRY_AFTER_MAX so a hostile or broken header
    cannot reach the UI as a year-long wait.
    """
    try:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        if isinstance(value, (int, float)):
            seconds = _finite(value)
        else:
            text = str(value).strip()
            if not text or "_" in text:  # float() accepts "1_000"; HTTP does not
                return None
            seconds = _finite(text)
            if seconds is None:
                when = email.utils.parsedate_to_datetime(text)
                if when is None:
                    return None
                if when.tzinfo is None:  # HTTP-dates are GMT by definition
                    when = when.replace(tzinfo=datetime.timezone.utc)
                seconds = _finite(when.timestamp() - _now(now))
        if seconds is None or seconds <= 0:
            return None
        return float(min(seconds, RETRY_AFTER_MAX))
    except Exception:
        return None


def key(provider, account_id):
    """State key for one account, e.g. 'claude:<id>'; no id -> 'legacy'."""
    try:
        ident = "" if account_id is None else str(account_id).strip()
        return "%s:%s" % (_norm(provider), ident or "legacy")
    except Exception:
        return "%s:legacy" % (_norm(provider) if provider is not None else "")


# ---- persistence -----------------------------------------------------------

def _default_path():
    return os.path.join(os.path.expanduser("~"), ".ipbar_ai_ratelimit.json")


def _is_our_file(path):
    """True when path is a regular, small JSON object carrying our marker."""
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return False
        if os.path.getsize(path) > _MAX_STATE_BYTES:
            return False
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return isinstance(doc, dict) and doc.get("_v") == STATE_VERSION
    except Exception:
        return False


def _configured_path():
    """AI_RL_FILE, expanded, or the default when unset or unsafe to own.

    The store is rewritten with os.replace, so a typo such as pointing it at a
    credential file would destroy that file. A target that already exists is
    only accepted when it is a state file this module wrote.
    """
    raw = os.environ.get("AI_RL_FILE", "").strip()
    if not raw:
        return _default_path()
    path = os.path.expandvars(os.path.expanduser(raw))
    if os.path.lexists(path) and not _is_our_file(path):
        _warn_once("rl_file", "AI_RL_FILE points at a file this widget did "
                   "not write; using the default state file instead")
        return _default_path()
    return path


def _path():
    if _path_override:
        return _path_override
    return _resolved_path or _configured_path()


def _clean_entry(raw, now):
    """A sane entry from untrusted JSON, or None."""
    if not isinstance(raw, dict):
        return None
    entry = {}
    for field in ("last", "next_at", "hold_until", "inflight_until"):
        number = _finite(raw.get(field))
        entry[field] = number if number is not None and number >= 0 else 0.0
    strikes = _finite(raw.get("strikes"))
    entry["strikes"] = int(min(max(strikes or 0, 0), 1000))
    entry["provider"] = _norm(raw.get("provider"))
    entry["kind"] = str(raw.get("kind") or "")[:32]
    # A hold beyond the longest one we could ever set means a broken clock or
    # a hand-edited file; bound it so the widget cannot be silenced forever.
    limit = now + max(RETRY_AFTER_MAX, BLOCKED_HOLD) + CEIL
    entry["hold_until"] = min(entry["hold_until"], limit)
    entry["next_at"] = min(entry["next_at"], limit)
    entry["inflight_until"] = min(entry["inflight_until"], now + INFLIGHT_TTL)
    return entry


def _keep_corrupt_copy(path):
    try:
        shutil.copyfile(path, path + ".corrupt")
    except Exception:
        pass


def _read_doc(path):
    """(doc, corrupt) for path; a missing file is empty, not corrupt."""
    try:
        if os.path.getsize(path) > _MAX_STATE_BYTES:
            return None, True
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return None, False
    except Exception:
        return None, True
    if not isinstance(doc, dict) or not isinstance(doc.get("keys"), dict):
        return None, True
    return doc, False


def _ensure_loaded():
    """Load state once. A corrupt file must not turn into a burst of polls.

    Losing the file would make every key look never-polled, so every account
    would be asked at once, holds included. Instead the damaged file is kept
    as <path>.corrupt and every key is treated as polled at load time.
    """
    global _loaded, _resolved_path, _distrust_since
    if _loaded:
        return
    _loaded = True
    _state.clear()
    _provider_holds.clear()
    _distrust_since = 0.0
    now = time.time()
    try:
        if not _path_override:
            _resolved_path = _configured_path()
        path = _path()
        doc, corrupt = _read_doc(path)
        if doc is not None:
            for name, raw in doc["keys"].items():
                entry = _clean_entry(raw, now)
                if entry is None or not isinstance(name, str):
                    corrupt = True  # a damaged entry may have carried a hold
                    continue
                _state[name] = entry
            holds = doc.get("providers")
            if isinstance(holds, dict):
                limit = now + RETRY_AFTER_MAX
                for name, until in holds.items():
                    until = _finite(until)
                    if isinstance(name, str) and until is not None and until > now:
                        _provider_holds[_norm(name)] = min(until, limit)
        if corrupt:
            _distrust_since = now
            _keep_corrupt_copy(path)
            _warn_once("corrupt", "AI usage policy state was unreadable; kept "
                       "a .corrupt copy and paused polls for one interval")
    except Exception:
        _state.clear()
        _provider_holds.clear()
        _distrust_since = now


def _save():
    """Atomically write state next to the target file; failures warn once."""
    path = _path()
    tmp = None
    try:
        folder = os.path.dirname(os.path.abspath(path))
        now = time.time()
        holds = {p: until for p, until in _provider_holds.items() if until > now}
        doc = {"_v": STATE_VERSION, "version": STATE_VERSION, "keys": _state,
               "providers": holds}
        fd, tmp = tempfile.mkstemp(prefix=".ai_rl.", suffix=".tmp", dir=folder)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(3):  # Windows: a reader may briefly hold the file
            try:
                os.replace(tmp, path)
                tmp = None
                break
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        if tmp:
            raise PermissionError("state file stayed locked")
    except Exception as exc:
        _warn_once("save", "could not save AI usage policy state (%s); holds "
                   "will not survive a restart" % type(exc).__name__)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _entry(name, provider):
    entry = _state.get(name)
    if entry is None:
        entry = {"last": 0.0, "next_at": 0.0, "hold_until": 0.0, "strikes": 0,
                 "inflight_until": 0.0, "provider": _norm(provider), "kind": ""}
        _state[name] = entry
    elif provider:
        entry["provider"] = _norm(provider)
    return entry


def _provider_of(name, provider=None):
    """provider, else the prefix of a 'provider:id' key."""
    if provider:
        return _norm(provider)
    return _norm(str(name).split(":", 1)[0])


def _jittered(provider):
    """Interval with jitter, never below the provider floor."""
    seconds = _interval(provider) * (1.0 + random.uniform(-JITTER, JITTER))
    return max(float(_floor(_norm(provider))), seconds)


def _exp(strikes):
    return 2 ** min(max(int(strikes), 0), _MAX_EXPONENT)


def _hold(name, provider, seconds, now, kind, strike=True):
    """Extend the hold to now+seconds (never shorten); return seconds left."""
    entry = _entry(name, provider)
    entry["hold_until"] = max(entry["hold_until"], now + max(seconds, 0.0))
    entry["next_at"] = max(entry["next_at"], entry["hold_until"])
    if strike:
        entry["strikes"] = int(entry["strikes"]) + 1
    entry["last"] = now
    entry["kind"] = kind
    entry["inflight_until"] = 0.0
    _save()
    return max(entry["hold_until"] - now, 0.0)


def _provider_hold_until(provider):
    """Latest hold_until that applies to every key of provider."""
    provider = _norm(provider)
    until = _provider_holds.get(provider, 0.0)
    for entry in _state.values():
        if entry["provider"] == provider and entry["kind"] in _PROVIDER_WIDE_KINDS:
            until = max(until, entry["hold_until"])
    return until


def _blocked_until(name, provider, now, manual):
    """Earliest time name may be requested (<= now means it may be now)."""
    provider = _provider_of(name, provider)
    until = _provider_hold_until(provider)
    entry = _state.get(name)
    # After a corrupt load nothing on disk can be trusted to say a key is
    # free, so every key not touched since then waits one interval.
    if _distrust_since and (entry is None or entry["last"] < _distrust_since):
        until = max(until, _distrust_since + _interval(provider))
    if entry is None:
        return until
    until = max(until, entry["hold_until"], entry["inflight_until"])
    if manual:
        last = entry["last"]
        if 0 < last <= now:
            until = max(until, last + manual_floor(provider))
    else:
        until = max(until, entry["next_at"])
    return until


# ---- public state API ------------------------------------------------------

def due(key, provider, now=None, manual=False):
    """(may_request, seconds_left) for key, read-only.

    A hold on this key or a provider-wide hold beats manual. Use claim() when
    about to send: due() alone cannot stop two threads both asking.
    """
    try:
        with _lock:
            _ensure_loaded()
            now = _now(now)
            left = _blocked_until(key, provider, now, bool(manual)) - now
            return (True, 0.0) if left <= 0 else (False, float(left))
    except Exception:
        return False, float(MANUAL_MIN)


def claim(key, provider, now=None, manual=False):
    """Atomically check due and reserve key for one request.

    Returns (granted, seconds_left). A granted claim marks the key in flight
    (and polled now) so a second claim -- another thread, a double click --
    is refused until a record_* call lands or INFLIGHT_TTL passes.
    """
    try:
        with _lock:
            _ensure_loaded()
            now = _now(now)
            left = _blocked_until(key, provider, now, bool(manual)) - now
            if left > 0:
                return False, float(left)
            entry = _entry(key, _provider_of(key, provider))
            entry["last"] = now
            entry["inflight_until"] = now + INFLIGHT_TTL
            entry["next_at"] = max(entry["next_at"], now + INFLIGHT_TTL)
            _save()
            return True, 0.0
    except Exception:
        return False, float(MANUAL_MIN)


def provider_hold_left(provider, now=None):
    """Seconds left on the longest 429/block hold across every key of provider."""
    try:
        with _lock:
            _ensure_loaded()
            return float(max(_provider_hold_until(provider) - _now(now), 0.0))
    except Exception:
        return 0.0


def record_success(key, provider, now=None):
    """A good read: clear strikes and hold, schedule the next poll."""
    try:
        with _lock:
            _ensure_loaded()
            now = _now(now)
            entry = _entry(key, provider)
            entry.update(last=now, strikes=0, hold_until=0.0, kind="ok",
                         inflight_until=0.0,
                         next_at=now + _jittered(entry["provider"]))
            _save()
    except Exception:
        pass


def record_rate_limited(key, provider, retry_after=None, now=None):
    """HTTP 429: hold max(own exponential backoff, server Retry-After)."""
    try:
        with _lock:
            _ensure_loaded()
            now = _now(now)
            entry = _entry(key, provider)
            name = entry["provider"]
            backoff = min(RL_BASE.get(name, OTHER_RL_BASE) * _exp(entry["strikes"]),
                          RL_CAP.get(name, OTHER_RL_CAP))
            server = parse_retry_after(retry_after, now) or 0.0
            return _hold(key, name, max(backoff, server), now, "rate_limited")
    except Exception:
        return float(OTHER_RL_BASE)


def record_blocked(key, provider, now=None):
    """403 with an HTML / Cloudflare challenge body: hold for an hour."""
    try:
        with _lock:
            _ensure_loaded()
            now = _now(now)
            return _hold(key, provider, float(BLOCKED_HOLD), now, "blocked")
    except Exception:
        return float(BLOCKED_HOLD)


def record_error(key, provider, now=None):
    """Any other vendor-side failure: interval * 2**strikes, capped."""
    try:
        with _lock:
            _ensure_loaded()
            now = _now(now)
            entry = _entry(key, provider)
            seconds = min(_interval(entry["provider"]) * _exp(entry["strikes"]), ERROR_CAP)
            return _hold(key, entry["provider"], float(seconds), now, "error")
    except Exception:
        return float(ERROR_CAP)


def record_offline(key, provider, now=None):
    """Transport failure (status 0): the vendor never saw it, so no strike and
    no hold; just try again one interval from now. An existing hold stays."""
    try:
        with _lock:
            _ensure_loaded()
            now = _now(now)
            entry = _entry(key, provider)
            entry["last"] = now
            entry["inflight_until"] = 0.0
            entry["next_at"] = max(now + _interval(entry["provider"]),
                                   entry["hold_until"])
            if entry["hold_until"] <= now:
                entry["kind"] = "offline"
            _save()
    except Exception:
        pass


def record_login_dead(key, provider, now=None):
    """The vendor refused the login: retry hourly, not at the poll cadence.

    Held for this key only (another account of the provider is unaffected) and
    lifted by clear(key) once the user logs in again. Returns seconds held.
    """
    try:
        with _lock:
            _ensure_loaded()
            now = _now(now)
            return _hold(key, provider, float(LOGIN_DEAD_HOLD), now,
                         "login_dead", strike=False)
    except Exception:
        return float(LOGIN_DEAD_HOLD)


def clear(key):
    """Lift a dead-login hold on key so it is due now. Returns True if lifted.

    Only a login_dead hold is lifted: logging in again does not end a 429,
    and a fresh token must never be used to dodge one.
    """
    try:
        with _lock:
            _ensure_loaded()
            entry = _state.get(key)
            if entry is None or entry["kind"] != "login_dead":
                return False
            entry.update(hold_until=0.0, next_at=0.0, inflight_until=0.0, kind="")
            _save()
            return True
    except Exception:
        return False


def seed_hold(provider, until_ts, now=None):
    """Import a hold from elsewhere (an older build's retry_until) for every
    key of provider. Never shortens; clamped to RETRY_AFTER_MAX. Returns the
    provider's seconds left."""
    try:
        with _lock:
            _ensure_loaded()
            now = _now(now)
            name = _norm(provider)
            until = _finite(until_ts)
            if name and until is not None and until > now:
                until = min(until, now + RETRY_AFTER_MAX)
                if until > _provider_holds.get(name, 0.0):
                    _provider_holds[name] = until
                    _save()
            return float(max(_provider_hold_until(name) - now, 0.0))
    except Exception:
        return 0.0


def forget_except(active_keys):
    """Drop state for every key not in active_keys; returns how many dropped.

    Keys of removed accounts would otherwise stay overdue forever and pin the
    wake-up at the 5 s floor. A provider-wide hold on a dropped key is kept
    as a provider hold, so pruning can never shorten a 429.
    """
    try:
        active = {k for k in active_keys if isinstance(k, str)}
    except Exception:
        return 0
    try:
        with _lock:
            _ensure_loaded()
            now = time.time()
            stale = [name for name in _state if name not in active]
            for name in stale:
                entry = _state.pop(name)
                if entry["kind"] in _PROVIDER_WIDE_KINDS and entry["hold_until"] > now:
                    provider = entry["provider"]
                    _provider_holds[provider] = max(
                        _provider_holds.get(provider, 0.0), entry["hold_until"])
            if stale:
                _save()
            return len(stale)
    except Exception:
        return 0


def hold_left(key, now=None):
    """Seconds left on key's own hold, 0 when none (see provider_hold_left)."""
    try:
        with _lock:
            _ensure_loaded()
            entry = _state.get(key)
            if entry is None:
                return 0.0
            return max(entry["hold_until"] - _now(now), 0.0)
    except Exception:
        return 0.0


def next_wake(now=None, keys=None):
    """Seconds until the earliest key is due, holds included; floor 5.

    keys limits the answer to those keys (the accounts that are actually
    pollable); unknown ones count as due. No keys at all -> 0.
    """
    try:
        with _lock:
            _ensure_loaded()
            names = list(_state) if keys is None else \
                [k for k in keys if isinstance(k, str)]
            if not names:
                return 0.0
            now = _now(now)
            soonest = min(_blocked_until(name, None, now, False) for name in names) - now
            return float(max(soonest, NEXT_WAKE_FLOOR))
    except Exception:
        return float(NEXT_WAKE_FLOOR)


def snapshot():
    """Plain JSON-safe copy of the state (timestamps and counters only)."""
    try:
        with _lock:
            _ensure_loaded()
            now = time.time()
            keys = {}
            for name, entry in _state.items():
                row = {field: entry[field] for field in _FIELDS}
                row["provider"] = entry["provider"]
                row["kind"] = entry["kind"]
                row["hold_left"] = max(entry["hold_until"] - now, 0.0)
                keys[name] = row
            providers = {p: max(until - now, 0.0)
                         for p, until in _provider_holds.items() if until > now}
            return {"keys": keys, "providers": providers}
    except Exception:
        return {"keys": {}, "providers": {}}


def reset_for_tests(path=None):
    """Point the store at path (None -> AI_RL_FILE/default) and drop memory.

    The file itself is left alone, so a reset to the same path reloads what
    was persisted; use a fresh temp path for a clean slate.
    """
    global _loaded, _path_override, _resolved_path, _distrust_since
    try:
        with _lock:
            _path_override = str(path) if path else None
            _resolved_path = None
            _distrust_since = 0.0
            _state.clear()
            _provider_holds.clear()
            _warned.clear()
            _loaded = False
    except Exception:
        pass
