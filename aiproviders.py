"""Provider registry plus the adapters that actually talk to a vendor.

Two jobs, kept in one file because they are the same knowledge:

  1. PROVIDERS -- what the UI is allowed to offer. Every vendor the user might
     want a row for, whether or not we can poll it yet.
  2. The adapters for the ones that work.

Eight vendors are marked live: claude, codex, cursor, copilot, windsurf,
devin, kimi and cline. The remaining four are registered as `planned`:
storable, listable, renameable, and answering any poll with a plain "not
supported yet". That gap is deliberate and it is not laziness.

The rule that governs this file has not changed: an entry only becomes live
when its endpoint, its authentication and its response field names were all
read from a primary source -- vendor documentation or the vendor's own client
code -- and never assembled from a pattern or a plausible guess. A fabricated
endpoint is strictly worse than a blank row: a blank row says "we do not know
your quota", while a fabricated one says "you have 40% left" and is believed.
Each adapter below therefore carries a provenance comment stating where its
knowledge came from and how confident that makes it, so a future reader can
tell a verified endpoint from a guessed one without trusting this paragraph.

The four that stay planned each fail that bar for a different reason, and the
reason is recorded next to the registry entry rather than in a commit message,
because the next person to look will be looking here.

Everything here is synchronous and has no schedule of its own. The caller
decides when a request happens, which is what keeps core.py's fail-closed geo
gate in charge: on a blocked egress the gate simply never calls in.

Standard library only -- the sidecar's single third-party dependency is psutil
and it stays that way.
"""

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

HTTP_TIMEOUT = 20

# A credential whose expiry is older than this can never be revived: the
# refresh token behind it has itself lapsed, so every retry is a round trip
# that can only fail. Claude's refresh_token lifetime measures ~1363194s
# (~15.8 days); 16 days is that rounded up, and Codex is not shorter. Callers
# use is_unrecoverable() to say "this needs a real re-login" instead of
# retrying an unrecoverable credential until the user gives up on the widget.
CRED_MAX_STALE = 16 * 86400

# A custom User-Agent is mandatory on both hosts. urllib's default and
# requests' default are both WAF-blocked, and the failure looks like a
# provider outage rather than a client mistake, so it is worth stating.
CLAUDE_UA = "claude-cli/2.0.32 (external, cli)"
CLAUDE_USAGE = "https://api.anthropic.com/api/oauth/usage"
# platform.claude.com, NOT console.anthropic.com: the console host's
# /v1/oauth/token returns 404 now, even though most guidance online still
# names it. Changing this back will produce an endless "refresh failed (404)".
CLAUDE_TOKEN = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

CODEX_UA = "codex_cli_rs/0.20.0"
CODEX_USAGE = "https://chatgpt.com/backend-api/wham/usage"
CODEX_TOKEN = "https://auth.openai.com/oauth/token"
CODEX_CLIENT = "app_EMoamEEZ73f0CkXaXp7hrann"

# Every host below also rejects urllib's default User-Agent, so one is always
# sent. We identify ourselves honestly instead of impersonating a vendor
# client: what these WAFs object to is the stock library UA, not a UA they do
# not recognise, and a request that lies about its origin is a request nobody
# can debug from the server side.
WIDGET_UA = "net-watch-widget/1.0 (usage-poll; +stdlib-urllib)"

# Cursor. Provenance: the Connect RPC path and the OAuth client id are the
# ones Cursor's own desktop client sends, as read from third-party clients
# built against it; the cursor.com/api/usage form is the older REST shape that
# the dashboard itself calls. Confidence: good for both, and the adapter tries
# the RPC first and only falls back to REST, so a change to either one
# degrades rather than lies.
CURSOR_RPC = ("https://api2.cursor.sh/aiserver.v1.DashboardService/"
              "GetCurrentPeriodUsage")
CURSOR_REST = "https://cursor.com/api/usage"
CURSOR_TOKEN = "https://api2.cursor.sh/oauth/token"
CURSOR_CLIENT = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"

# GitHub Copilot. Provenance: /copilot_internal/user is the endpoint the
# editor plugins call to learn what the signed-in account is entitled to, and
# the quota_snapshots field names are the ones it returns. Confidence: good,
# with one caveat that is handled explicitly below -- free-tier accounts answer
# with an entirely different body.
COPILOT_USER = "https://api.github.com/copilot_internal/user"
# The endpoint refuses requests that do not claim to be an editor. The value
# only has to be a plausible editor build string; it is not an entitlement.
COPILOT_EDITOR = "vscode/1.95.0"

# Windsurf and Devin. Provenance: both are Cognition products sharing one
# backend, and this Connect RPC is what the Windsurf IDE calls on startup to
# learn the seat's plan status. Confidence: good for the path and the
# planStatus field names. Note the unusual authentication: the API key travels
# inside the JSON body under `metadata`, not in an Authorization header, so a
# 401 here is not a header mistake.
COGNITION_STATUS = ("https://server.codeium.com/"
                    "exa.seat_management_pb.SeatManagementService/"
                    "GetUserStatus")

# Kimi Code. Provenance: the Kimi coding CLI polls /coding/v1/usages with the
# subscription's bearer token. Confidence: good for host and path, moderate
# for the body, because two response generations are still in circulation (a
# ratio-based one and an older count-based one) and both are handled.
#
# Kimi Code the SUBSCRIPTION is not the Moonshot open-platform API credit
# balance. They are separate systems with separate meters: the platform
# balance says nothing about how much of a coding subscription is consumed, so
# it is deliberately not read here even though it is easier to reach.
KIMI_USAGE = "https://api.kimi.com/coding/v1/usages"

# Cline. Provenance: the extension's own account client calls this path and
# the {success, data:{limits:[...]}} envelope is what it parses. Confidence:
# good. resetsAt is nullable in that envelope, which is handled rather than
# assumed away.
CLINE_USAGE = "https://api.cline.bot/api/v1/users/me/plan/usage-limits"

NOT_SUPPORTED = "not supported yet"


PROVIDERS = [
    {"id": "claude", "name": "Claude Code", "status": "live",
     "auth": "oauth-cli",
     "hint": "Run 'claude' and complete the browser login, then import."},
    {"id": "codex", "name": "OpenAI Codex / ChatGPT", "status": "live",
     "auth": "oauth-cli",
     "hint": "Run 'codex login' and complete the browser login, then import."},
    {"id": "cursor", "name": "Cursor", "status": "live",
     "auth": "oauth-app",
     "hint": "Log in inside the Cursor app, then import its session token."},
    {"id": "copilot", "name": "GitHub Copilot", "status": "live",
     "auth": "oauth-token",
     "hint": "Log in with 'gh auth login', then import the OAuth token."},
    {"id": "windsurf", "name": "Windsurf", "status": "live",
     "auth": "api-key",
     "hint": "Copy the API key from Windsurf settings, then import it."},
    {"id": "devin", "name": "Devin", "status": "live",
     "auth": "api-key",
     "hint": "Create an API key in Devin settings, then import it."},
    # Replit stays planned and will stay planned until something changes at
    # their end. The usage API is an Enterprise-plan, org-admin surface, so an
    # ordinary personal plan has no endpoint to call at all; and Replit is
    # browser-first, so there is no local credential file to import either.
    # Both halves of what an adapter needs are missing, not just the shape.
    {"id": "replit", "name": "Replit", "status": "planned",
     "auth": "unknown",
     "hint": "Usage API is Enterprise/admin-only; nothing to poll on a "
             "personal plan."},
    {"id": "kimi", "name": "Kimi Code", "status": "live",
     "auth": "oauth-cli",
     "hint": "Log in with the Kimi CLI, then import its credential."},
    # GLM stays planned on purpose. A candidate endpoint exists -- the Z.ai /
    # bigmodel console serves the coding plan's quota to its own dashboard --
    # but the response FIELD NAMES were never read from a primary source, only
    # the URL was. An adapter written against guessed field names does not
    # fail visibly; it renders confident wrong percentages, which is the exact
    # failure this file is built to avoid. Whoever picks this up should
    # capture one real dashboard response first and paste its keys here, then
    # flip the status in the same commit as the adapter.
    {"id": "glm", "name": "GLM Coding Plan", "status": "planned",
     "auth": "unknown",
     "hint": "Console endpoint known, response fields unverified; not "
             "polled."},
    {"id": "cline", "name": "Cline", "status": "live",
     "auth": "api-key",
     "hint": "Sign in inside the Cline extension, then import its token."},
    # Antigravity stays planned for two independent reasons, either of which
    # alone would be enough: the response shape behind the endpoints that were
    # found is unknown, and no source describes where the credential lives on
    # Windows, so even a correct adapter would have nothing to authenticate
    # with on this platform.
    {"id": "antigravity", "name": "Google Antigravity", "status": "planned",
     "auth": "unknown",
     "hint": "Response shape unknown and no Windows credential path known."},
    # Railway stays planned, and it is worth saying that it may never fit.
    # The GraphQL transport is documented but the specific usage query was
    # never read, so there is nothing verified to send. More fundamentally,
    # Railway bills infrastructure by consumption: there is no quota window
    # and no reset clock, so the "x% used, resets at y" row this widget draws
    # has no honest Railway equivalent. A spend-to-date figure would need a
    # different row type before it could be shown.
    {"id": "railway", "name": "Railway", "status": "planned",
     "auth": "unknown",
     "hint": "Consumption billing, no quota window; usage query unverified."},
]

_BY_ID = {p["id"]: p for p in PROVIDERS}


def provider(pid):
    return _BY_ID.get(pid)


def provider_ids():
    return [p["id"] for p in PROVIDERS]


def is_live(pid):
    p = _BY_ID.get(pid)
    return bool(p and p["status"] == "live")


# ── transport ─────────────────────────────────────────────────────────────────
def _http_json(url, headers, data=None, timeout=HTTP_TIMEOUT):
    """Returns (status, parsed_or_None, raw_body). Never raises.

    status 0 means the request never reached the server at all -- DNS, no
    route, a dropped tunnel, TLS refusal. Keeping that distinct from a real
    HTTP status is the whole basis of the failure classification below, so it
    gets its own sentinel rather than being folded into an exception.
    """
    try:
        req = urllib.request.Request(
            url, data=data, method="POST" if data is not None else "GET",
            headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw), raw
            except Exception:
                return resp.status, None, raw
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        try:
            return e.code, json.loads(raw), raw
        except Exception:
            return e.code, None, raw
    except Exception as e:
        # Body carries the exception TYPE only. The URL and headers are in
        # scope here and headers hold a bearer token; str(e) on some socket
        # errors echoes request context, so nothing but the class name is
        # allowed out.
        return 0, None, type(e).__name__


_DEAD_MARKERS = ("expired", "invalid_grant", "revoked")


def _classify(status, raw):
    """Turn a failed refresh into one of exactly three honest reasons.

    This is the bug the whole multi-account rework exists to fix, so it is
    spelled out rather than collapsed into a one-liner:

      'login expired'  -- 400/401 AND the body names the grant as expired,
                          invalid or revoked. Only a refusal of the grant
                          itself proves the login is gone.
      'offline'        -- status 0. The request never reached the provider,
                          which says NOTHING about the credential. Calling
                          this "login expired" sends the user off to redo an
                          OAuth round trip that was never broken; on a
                          connection that drops as often as this one, that is
                          most of the false alarms.
      'refresh failed (N)' -- anything else non-200. A provider-side problem,
                          and the status code is kept so a 404 (wrong token
                          host) is distinguishable from a 500 (their outage).

    Note the AND in the first case: a bare 400 with no such marker is a
    malformed-request or provider-side fault, not a dead login.
    """
    if status == 0:
        return "offline"
    body = (raw or "").lower()
    if status in (400, 401) and any(k in body for k in _DEAD_MARKERS):
        return "login expired"
    return "refresh failed (%s)" % status


# ── credential shapes ─────────────────────────────────────────────────────────
def _dict(cred):
    """The credential as a dict, or an empty one.

    `(cred or {})` is not enough: a credential that is neither a dict nor
    falsy -- a bare number or True, which a corrupted vault can produce -- is
    truthy and has no .get, and the AttributeError escapes cred_expiry() and
    is_unrecoverable(), neither of which is wrapped by the dispatch layer's
    try/except. Those two are supposed to answer "cannot tell" for a shape
    they do not recognise, not raise, so the coercion happens here once.
    """
    return cred if isinstance(cred, dict) else {}


def _claude_oauth(cred):
    o = _dict(cred).get("claudeAiOauth")
    return o if isinstance(o, dict) else None


def _codex_tokens(cred):
    t = _dict(cred).get("tokens")
    return t if isinstance(t, dict) else None


def _jwt_exp(tok):
    """exp claim out of a JWT without verifying it.

    Not a security decision: the token was handed to us by the provider and is
    about to be sent straight back. All we want is the expiry so we can avoid
    a pointless round trip, and Codex publishes it nowhere else.
    """
    try:
        seg = (tok or "").split(".")[1]
        seg += "=" * (-len(seg) % 4)
        exp = json.loads(base64.urlsafe_b64decode(seg)).get("exp")
        return float(exp) if exp else None
    except Exception:
        return None


def cred_expiry(provider_id, cred):
    """Access-token expiry as UNIX seconds, or None when unknown.

    Exists so callers can sort accounts and decide staleness without knowing
    that Claude stores epoch MILLISECONDS in a nested object while Codex hides
    it in a JWT claim. A None here means "cannot tell", which must not be read
    as "expired" -- a shape we do not recognise is not a dead login.
    """
    if provider_id == "claude":
        o = _claude_oauth(cred)
        if not o:
            return None
        v = o.get("expiresAt")
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v / 1000.0 if v else None
    if provider_id == "codex":
        t = _codex_tokens(cred)
        if not t:
            return None
        return _jwt_exp(t.get("id_token")) or _jwt_exp(t.get("access_token"))
    if provider_id == "cursor":
        # Cursor's session token is a JWT, so the expiry is in the token
        # itself and nowhere else in the credential.
        return _jwt_exp(_token(_vendor_cred(cred, "cursor", "cursorAuth"),
                               "accessToken", "access_token", "token"))
    if provider_id == "kimi":
        k = _vendor_cred(cred, "kimi", "kimiAuth")
        for key in ("expires_at", "expiresAt", "expiry"):
            v = _num((k or {}).get(key))
            if v:
                # Written as epoch seconds in some generations and epoch
                # milliseconds in others; a value that far in the future can
                # only be milliseconds, since seconds that large is year 33658.
                return v / 1000.0 if v > 1e11 else v
        return _jwt_exp(_token(k, "access_token", "accessToken", "token"))
    if provider_id in _NEVER_EXPIRES:
        # Not "unknown" but "there is no expiry": these vendors issue
        # long-lived keys with no exp claim and no expiry field. None is still
        # the right answer for a caller asking when it lapses, and
        # is_unrecoverable() below makes sure None is never read as dead.
        return None
    return None


def is_unrecoverable(provider_id, cred):
    """True when no refresh can possibly work and only a re-login will do.

    Two ways to be unrecoverable, and both show up on real machines:
      * the expiry is further in the past than the refresh token's own
        lifetime (CRED_MAX_STALE), or
      * there is no refresh token in the file at all. A logged-out Claude
        credential is not deleted, it is gutted: expiresAt drops to 0 and
        refreshToken disappears. Ranking such a file by expiry puts it last,
        but attempting to refresh from it still yields a confusing error.

    An unknown expiry returns False on purpose. "I cannot read this" must not
    graduate into "your login is dead".
    """
    if provider_id in _NEVER_EXPIRES:
        # A token that never expires cannot be stale, so it can never be
        # unrecoverable. Saying otherwise would send the user to re-login over
        # a credential that is still perfectly valid -- the same class of
        # false alarm as reporting "login expired" for an offline poll.
        return False
    if provider_id == "claude":
        o = _claude_oauth(cred)
        if not o:
            return False
        if not o.get("refreshToken"):
            return True
    elif provider_id == "codex":
        t = _codex_tokens(cred)
        if not t:
            return False
        if not t.get("refresh_token"):
            return True
    exp = cred_expiry(provider_id, cred)
    if exp is None:
        return False
    return exp < time.time() - CRED_MAX_STALE


# ── durable credential identity ──────────────────────────────────────────────
# Why this section exists, stated plainly because it is the fix for a bug the
# user actually hit: a Claude credential file contains NO account identifier.
# The vault's fingerprint was therefore built from subscriptionType plus
# expiresAt -- and expiresAt is rewritten on EVERY token issue, including the
# silent refresh the CLI does on its own. That makes the "fingerprint" a
# timestamp wearing an identity's clothes:
#
#   * the duplicate check never fires for claude, so signing into the SAME
#     account a second time writes a SECOND row -- the reported symptom;
#   * a tombstone keyed on it goes stale the first time the token renews, so
#     removal has to fall back to matching the credential file's path;
#   * sidecar's re-auth guard cannot refuse on a fingerprint that is expected
#     to differ every time, so it is written to tighten automatically once a
#     durable one exists.
#
# Where durable identity actually lives. Claude Code stores the signed-in
# account in the CLI's own state file, `~/.claude.json`, under `oauthAccount`
# -- NOT in `.claude/.credentials.json`. Read from the live file on this
# machine, that object carries (key names only, no values ever read out of
# this module): accountUuid, emailAddress, organizationUuid, organizationRole,
# organizationName, displayName, billingType, accountCreatedAt,
# subscriptionCreatedAt, seatTier, workspaceRole, hasExtraUsageEnabled,
# ccOnboardingFlags, claudeCodeTrialDurationDays, claudeCodeTrialEndsAt (and,
# on some builds, organizationRateLimitTier / organizationType /
# userRateLimitTier). `accountUuid` is a stable per-account UUID and is
# exactly the identifier the credential file lacks.
#
# This is a LOCAL FILE READ, never a network call. There is no offline way to
# learn the account behind a Claude access token other than asking Anthropic,
# and this module must not do that; upstream has been asked to put an id in
# the credential file and declined (the issue is closed "not planned"). So the
# CLI's own state file is the best material available, and when it is missing
# -- a credential imported by hand, a pruned profile, a vault row from before
# this change -- we say so via `durable=False` rather than inventing an id.
#
# Nothing here reads, returns, hashes or logs a token, a refresh token, or any
# other secret. The email address IS treated as sensitive: it is never
# returned in a fingerprint, only a truncated SHA-256 of its lowercased form,
# which compares equal for the same account and reveals nothing.

# The CLI state file that holds `oauthAccount`, relative to the home directory
# that also holds `.claude/.credentials.json`.
CLAUDE_STATE_FILE = ".claude.json"

# The oauthAccount fields worth carrying, most durable first. accountUuid is a
# real account identifier; emailAddress is the human-stable fallback for older
# CLI builds that wrote no uuid. organizationUuid is deliberately NOT used on
# its own: two accounts in one organisation share it, so it would merge them.
_CLAUDE_ID_KEYS = ("accountUuid", "emailAddress")

# Tiny memo so a sweep over several accounts does not re-read and re-parse the
# same multi-hundred-kilobyte state file once per row. Keyed by path plus the
# stat identity of the file, so an edit by the CLI invalidates it for free. It
# holds only the two id fields above -- never the rest of the document, which
# includes project history.
_CLAUDE_STATE_CACHE = {}
_CLAUDE_STATE_CACHE_MAX = 32


def _claude_state_paths(cred_path):
    """Candidate `.claude.json` locations for a given credential file path.

    The ordinary layout is `<home>/.claude/.credentials.json` beside
    `<home>/.claude.json`, so the first candidate is the parent of the
    `.claude` directory. Two fallbacks follow for layouts that nest
    differently (a sandboxed CLI home, a relocated CLAUDE_CONFIG_DIR): the
    credential's own directory, and that directory's parent. Order is
    best-first and every candidate is only ever opened read-only.
    """
    if not cred_path or not isinstance(cred_path, str):
        return []
    try:
        d = os.path.dirname(os.path.abspath(cred_path))
    except Exception:
        return []
    cands = []
    parent = os.path.dirname(d)
    if os.path.basename(d).lower() in (".claude", "claude") and parent:
        cands.append(os.path.join(parent, CLAUDE_STATE_FILE))
    cands.append(os.path.join(d, CLAUDE_STATE_FILE))
    if parent:
        cands.append(os.path.join(parent, CLAUDE_STATE_FILE))
    seen, out = set(), []
    for p in cands:
        k = os.path.normcase(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def _read_claude_state(path):
    """The two id fields out of one `.claude.json`, or None. Never raises.

    Returns a dict with at most accountUuid and emailAddress. Everything else
    in that file -- project paths, prompt history, cached feature flags -- is
    dropped here and never enters the process's long-lived state.
    """
    try:
        st = os.stat(path)
        key = (os.path.normcase(path), st.st_mtime_ns, st.st_size)
    except Exception:
        return None
    hit = _CLAUDE_STATE_CACHE.get(key)
    if hit is not None:
        return hit or None
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        doc = None
    out = {}
    if isinstance(doc, dict):
        acct = doc.get("oauthAccount")
        if isinstance(acct, dict):
            for k in _CLAUDE_ID_KEYS:
                v = acct.get(k)
                if isinstance(v, str) and v.strip():
                    out[k] = v.strip()
    if len(_CLAUDE_STATE_CACHE) >= _CLAUDE_STATE_CACHE_MAX:
        _CLAUDE_STATE_CACHE.clear()
    _CLAUDE_STATE_CACHE[key] = out
    return out or None


def _claude_account_ids(cred, cred_path=None):
    """{accountUuid?, emailAddress?} for a Claude credential, or {}.

    Two sources, in order:
      1. the credential object itself, if an importer has already stapled an
         `oauthAccount` (or a flat `accountUuid`) onto it -- which is what a
         vault row written by a future import path will look like, and costs
         no file access at all;
      2. the CLI state file next to `cred_path`.

    A credential with neither is not an error and not a different account; it
    is an account whose identity we cannot establish, and every caller below
    degrades to the legacy scheme for it instead of guessing.
    """
    out = {}
    c = _dict(cred)
    acct = c.get("oauthAccount")
    if not isinstance(acct, dict):
        o = _claude_oauth(cred) or {}
        sub = o.get("oauthAccount")
        acct = sub if isinstance(sub, dict) else {}
    for k in _CLAUDE_ID_KEYS:
        v = acct.get(k) if isinstance(acct, dict) else None
        if not (isinstance(v, str) and v.strip()):
            v = c.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    if out:
        return out
    for p in _claude_state_paths(cred_path):
        got = _read_claude_state(p)
        if got:
            return dict(got)
    return {}


def _email_digest(email):
    """A stable, non-reversible tag for an email address.

    The address itself never leaves this function. Fingerprints are compared,
    stored in the vault, written into tombstone files and shown in diagnostic
    listings; an address in any of those is a privacy leak for no gain, since
    only equality is ever needed. Lowercased first because the same account
    can be typed either way, truncated because 64 bits of SHA-256 is far more
    than enough to separate a handful of accounts.
    """
    try:
        norm = str(email).strip().lower().encode("utf-8")
    except Exception:
        return None
    if not norm:
        return None
    return hashlib.sha256(norm).hexdigest()[:16]


def _claude_legacy_fp(cred):
    """The pre-existing claude fingerprint, byte for byte.

    Reproduced here rather than imported so this module keeps its "no sibling
    imports" property, and reproduced EXACTLY because vault rows and tombstone
    files already on disk contain this string. Changing its shape by one
    character would orphan every one of them -- which would present as the
    user's original bug (a known account seen as new) plus a second one (a
    deliberately removed account coming back).
    """
    o = _claude_oauth(cred)
    if not o:
        return None
    return "claude:%s:%s" % (o.get("subscriptionType") or "?",
                             o.get("expiresAt") or 0)


def _codex_fp(cred):
    """Codex's fingerprint: account_id, which is already durable."""
    t = _codex_tokens(cred)
    if not t:
        return None
    acct = t.get("account_id")
    if not acct:
        return None
    return "codex:%s" % acct


def cred_identity(provider_id, cred, cred_path=None):
    """What we know about WHOSE account this credential is.

    Always a dict, never an exception:

      kind     "account_uuid" | "email" | "account_id" | "expiry" | None
      durable  True when the value survives a token refresh
      fp       the fingerprint string, or None when even the legacy scheme
               cannot be applied (an unrecognised credential shape)
      source   "cred" when the id came from the credential object, "cli-state"
               when it came from the CLI's own `.claude.json`, "legacy" when
               no durable id was available

    `durable=False` is a real answer and callers must respect it: it means
    "this fingerprint changes when the token renews", which is precisely when
    a duplicate check has to fall back to something else (the credential path)
    rather than conclude the account is new.

    No value in the returned dict is a secret. The email case returns only a
    truncated digest.
    """
    out = {"provider": provider_id, "kind": None, "durable": False,
           "fp": None, "source": "legacy"}
    if provider_id == "codex":
        fp = _codex_fp(cred)
        if fp:
            out.update(kind="account_id", durable=True, fp=fp, source="cred")
        return out
    if provider_id != "claude":
        return out
    if _claude_oauth(cred) is None:
        # Not a claude credential shape at all. None, so callers skip it
        # rather than importing a mystery -- the long-standing contract.
        return out
    ids = _claude_account_ids(cred, cred_path)
    src = "cred" if (isinstance(_dict(cred).get("oauthAccount"), dict)
                     or _dict(cred).get("accountUuid")) else "cli-state"
    uid = ids.get("accountUuid")
    if uid:
        out.update(kind="account_uuid", durable=True,
                   fp="claude:acct:%s" % uid, source=src)
        return out
    dig = _email_digest(ids.get("emailAddress")) if ids.get("emailAddress") \
        else None
    if dig:
        out.update(kind="email", durable=True,
                   fp="claude:email:%s" % dig, source=src)
        return out
    fp = _claude_legacy_fp(cred)
    if fp:
        out.update(kind="expiry", durable=False, fp=fp, source="legacy")
    return out


def cred_fingerprint(provider_id, cred, cred_path=None):
    """The best fingerprint available for this credential, or None.

    Durable when the account is identifiable, the legacy expiry-based string
    when it is not. Callers that need to know which they got ask
    `is_durable_fingerprint()` or use `cred_identity()` directly.
    """
    return cred_identity(provider_id, cred, cred_path).get("fp")


def is_durable_fingerprint(fp):
    """True when this fingerprint string survives a token refresh.

    Recognises the stored form, so it answers correctly for a value read back
    out of the vault or a tombstone file with no credential in hand.
    """
    if not isinstance(fp, str) or not fp:
        return False
    if fp.startswith("codex:"):
        return True
    return fp.startswith("claude:acct:") or fp.startswith("claude:email:")


def cred_fingerprint_candidates(provider_id, cred, cred_path=None):
    """Every fingerprint form this credential answers to, best first.

    This is the back-compatibility mechanism, and it is a matcher rather than
    a migration on purpose. A migration would have to rewrite the vault AND
    the tombstone sidecar AND do it before the CLI's next silent refresh moved
    the legacy value out from under it; a row or tombstone written by an older
    build, or by a build that never saw the CLI state file, would still be
    left behind. Answering to both forms costs one extra string comparison and
    cannot orphan anything.

    So a claude credential with a known account yields:
        ["claude:acct:<uuid>", "claude:<tier>:<expiresAt>"]
    The legacy element is kept second and is still an exact match for a row
    stored before this change, as long as that row's credential has not been
    refreshed since -- and when it HAS been refreshed, the durable element
    matches instead, which is the entire point.
    """
    out = []
    ident = cred_identity(provider_id, cred, cred_path)
    if ident.get("fp"):
        out.append(ident["fp"])
    if provider_id == "claude":
        legacy = _claude_legacy_fp(cred)
        if legacy and legacy not in out:
            out.append(legacy)
    return out


def cred_fingerprint_matches(provider_id, cred, stored, cred_path=None):
    """True when `stored` identifies the same account as this credential.

    `stored` is whatever a vault row or a tombstone entry recorded, in either
    format. Asymmetry is deliberate and is the safe direction: a legacy stored
    value matches only while the token behind it has not been renewed, while a
    durable stored value keeps matching forever. A False here never deletes
    anything -- it only means "treat as a different account" -- so the failure
    mode of an unmatched legacy tombstone is the one that already existed
    before this change, not a new one.
    """
    if not stored or not isinstance(stored, str):
        return False
    return stored in cred_fingerprint_candidates(provider_id, cred, cred_path)

# ── Claude adapter ────────────────────────────────────────────────────────────
def _claude_usage(cred):
    o = _claude_oauth(cred)
    if not o or not o.get("accessToken"):
        return {"error": "no credential"}
    st, p, raw = _http_json(CLAUDE_USAGE, {
        "Authorization": "Bearer " + o["accessToken"],
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": CLAUDE_UA,
        "Accept": "application/json",
    })
    if st == 0:
        return {"error": "offline"}
    if st == 401:
        return {"error": "token rejected"}
    if st == 429:
        return {"error": "rate limited"}
    if st != 200 or not isinstance(p, dict):
        return {"error": "http %s" % st}
    fh = p.get("five_hour") or {}
    sd = p.get("seven_day") or {}
    out = {
        "provider": "claude",
        "session_pct": fh.get("utilization"),
        "session_reset": fh.get("resets_at"),
        "week_pct": sd.get("utilization"),
        "week_reset": sd.get("resets_at"),
        "extra_usage": p.get("extra_usage"),
    }
    # The per-model cap arrives in a `limits` array whose order is not
    # contractual, so pick by kind and prefer whichever limit the account is
    # actively burning; ties go to the highest utilisation, which is the one
    # about to bite.
    best = None
    for lim in (p.get("limits") or []):
        if not isinstance(lim, dict) or lim.get("kind") != "weekly_scoped":
            continue
        pct = lim.get("percent")
        if pct is None:
            continue
        name = (((lim.get("scope") or {}).get("model") or {})
                .get("display_name") or "model")
        if best is None or lim.get("is_active") or pct > best[1]:
            best = (name, pct, lim.get("resets_at"))
    if best:
        out["model_name"], out["model_pct"], out["model_reset"] = best
    return out


def _claude_refresh(cred):
    o = _claude_oauth(cred)
    if not o:
        return None, "no credential"
    if not o.get("refreshToken"):
        # Gutted logged-out file: refreshing cannot work and the provider
        # would answer with a generic 400 that _classify would read as a dead
        # login. True, but we can say it without spending a request.
        return None, "login expired"
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": o["refreshToken"],
        "client_id": CLAUDE_CLIENT,
    }).encode()
    st, p, raw = _http_json(CLAUDE_TOKEN, {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": CLAUDE_UA,
    }, data=body)
    if st == 200 and isinstance(p, dict) and p.get("access_token"):
        # Mutate a copy of the WHOLE credential and hand it all back. The
        # refresh token rotates: the one we just used is already dead at the
        # provider, so returning anything less than the complete object risks
        # the caller persisting a credential missing its new refresh token,
        # which is the one way to genuinely brick a login.
        new = json.loads(json.dumps(cred))
        n = new["claudeAiOauth"]
        n["accessToken"] = p["access_token"]
        if p.get("refresh_token"):
            n["refreshToken"] = p["refresh_token"]
        if p.get("expires_in"):
            n["expiresAt"] = int((time.time() + float(p["expires_in"])) * 1000)
        if p.get("scope"):
            n["scopes"] = p["scope"].split()
        return new, None
    return None, _classify(st, raw)


# ── Codex adapter ─────────────────────────────────────────────────────────────
def _codex_usage(cred):
    t = _codex_tokens(cred)
    if not t or not t.get("access_token"):
        return {"error": "no credential"}
    h = {
        "Authorization": "Bearer " + t["access_token"],
        "originator": "codex_cli_rs",
        "User-Agent": CODEX_UA,
        "Accept": "application/json",
    }
    if t.get("account_id"):
        h["chatgpt-account-id"] = t["account_id"]
    st, p, raw = _http_json(CODEX_USAGE, h)
    if st == 0:
        return {"error": "offline"}
    if st == 401:
        return {"error": "token rejected"}
    if st == 429:
        return {"error": "rate limited"}
    if st != 200 or not isinstance(p, dict):
        return {"error": "http %s" % st}
    rl = p.get("rate_limit") or {}
    out = {"provider": "codex",
           "plan": (p.get("plan_type") or "").upper() or None,
           "credits": (p.get("credits") or {}).get("balance"),
           "limit_reached": rl.get("limit_reached")}
    # Map windows by their declared length, never by primary/secondary
    # position: OpenAI currently sends primary=5h and secondary=weekly, but
    # that ordering has never been documented as stable.
    for key in ("primary_window", "secondary_window"):
        w = rl.get(key) or {}
        if not isinstance(w, dict) or not w:
            continue
        secs = w.get("limit_window_seconds") or 0
        slot = "session" if secs and secs <= 86400 else "week"
        out[slot + "_pct"] = w.get("used_percent")
        out[slot + "_reset"] = w.get("reset_at")
        out[slot + "_window"] = secs
    return out


def _codex_refresh(cred):
    t = _codex_tokens(cred)
    if not t:
        return None, "no credential"
    if not t.get("refresh_token"):
        return None, "login expired"
    # OpenAI returns earliest_refresh_at alongside the rotated token and means
    # it: refreshing before that instant is throttled provider-side, and the
    # throttle response is indistinguishable from a real failure. Honouring it
    # locally costs one comparison and avoids teaching the user to distrust a
    # correct error message.
    ear = t.get("earliest_refresh_at")
    ear_ts = _parse_ts(ear)
    if ear_ts and ear_ts > time.time():
        return None, "too soon (earliest refresh %s)" % ear
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": t["refresh_token"],
        "client_id": CODEX_CLIENT,
        "scope": "openid profile email",
    }).encode()
    st, p, raw = _http_json(CODEX_TOKEN, {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": CODEX_UA,
    }, data=body)
    if st == 200 and isinstance(p, dict) and p.get("access_token"):
        new = json.loads(json.dumps(cred))
        n = new["tokens"]
        n["access_token"] = p["access_token"]
        if p.get("refresh_token"):
            n["refresh_token"] = p["refresh_token"]
        if p.get("id_token"):
            n["id_token"] = p["id_token"]
        if p.get("earliest_refresh_at"):
            n["earliest_refresh_at"] = p["earliest_refresh_at"]
        return new, None
    return None, _classify(st, raw)


def _parse_ts(v):
    """Accept epoch seconds or an ISO 8601 string; None when neither.

    earliest_refresh_at has been observed in both shapes, and guessing wrong
    would silently disable the throttle guard.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        import datetime as _dt
        s = str(v).replace("Z", "+00:00")
        return _dt.datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


# ── shared shaping helpers for the vendors added after claude/codex ──────────
# Claude and Codex each hand back a dict keyed provider / <window>_pct /
# <window>_reset, where a window is "session" (the short rolling one), "week"
# or "month", the percentage is PERCENT CONSUMED on 0..100, and the reset is
# whatever timestamp form the vendor published. Every adapter below normalises
# onto exactly that, so the UI never learns a vendor's name to read a number.
# Anything a vendor offers beyond the windows (counts, dollar overage, plan
# name) is added as an extra key the UI may ignore; nothing vendor-specific is
# ever smuggled into the window keys themselves.

# Vendors whose credential is documented as long-lived with no expiry field.
# Kept in one place because two separate functions must agree about it.
_NEVER_EXPIRES = ("copilot", "windsurf", "devin", "cline")


def _vendor_cred(cred, *wrappers):
    """The inner credential object, whether it is wrapped or flat.

    The vault stores whatever the import path produced, and for these vendors
    that is sometimes a bare {"token": ...} and sometimes the same thing under
    a vendor key. Accepting both here keeps the shape question out of six
    adapters. A credential that is not a dict at all yields None, because
    every caller then reports "no credential" rather than raising.
    """
    if not isinstance(cred, dict):
        return None
    for w in wrappers:
        v = cred.get(w)
        if isinstance(v, dict):
            return v
    return cred


def _token(obj, *names):
    """First non-empty string among the named keys, else None."""
    if not isinstance(obj, dict):
        return None
    for n in names:
        v = obj.get(n)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _num(v):
    """A float, or None for anything that is not a plain number.

    Vendors send numbers as strings often enough to be worth tolerating, and
    an unparseable value must become None rather than propagate an exception
    out of an adapter that is contractually total.
    """
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _clamp_pct(v):
    """A percentage on 0..100, or None. Never a negative or a 137%."""
    n = _num(v)
    if n is None:
        return None
    return max(0.0, min(100.0, n))


def _pct_of(used, limit):
    """used/limit as a percentage, or None when the limit is unusable.

    A zero or missing limit is not 0% and not 100%; it is unknown, and an
    unlimited pool reported as 0% used would read as "plenty left" by luck
    rather than by measurement.
    """
    u, l = _num(used), _num(limit)
    if u is None or l is None or l <= 0:
        return None
    return max(0.0, min(100.0, u / l * 100.0))


def _usage_http_error(st, parsed):
    """The usage-side half of the three-way classification, or None on 200.

    Same discipline as _classify() applies and for the same reason: status 0
    is 'offline' and says nothing whatsoever about the credential, a 401/403
    is the token being refused, and every other non-200 keeps its status code
    so a wrong host (404) stays distinguishable from a vendor outage (500).
    Nothing here ever reports a dead login, because a usage endpoint refusing
    a request is not proof that the grant behind it is gone.
    """
    if st == 0:
        return {"error": "offline"}
    if st in (401, 403):
        return {"error": "token rejected"}
    if st == 429:
        return {"error": "rate limited"}
    if st != 200 or not isinstance(parsed, dict):
        return {"error": "http %s" % st}
    return None


def _no_refresh(_cred, why):
    """Refresh result for a vendor that has no refresh flow to offer.

    Returning an honest reason beats returning a fake success: the caller logs
    it once and stops retrying, and the reason distinguishes "this token never
    needs refreshing" from "we could not refresh it".
    """
    return None, why


# ── Cursor adapter ──────────────────────────────────────────────────────────
def _cursor_user_id(tok):
    """The numeric user id Cursor's REST endpoint wants, out of the JWT.

    It is not a claim of its own: the `sub` claim is "<tenant>|<user id>" and
    only the second field is the id the dashboard passes as ?user=. Decoding
    is unverified base64 on a token we already hold and are about to send
    back, so there is no security decision here, only a parse.
    """
    try:
        seg = (tok or "").split(".")[1]
        seg += "=" * (-len(seg) % 4)
        sub = json.loads(base64.urlsafe_b64decode(seg)).get("sub")
    except Exception:
        return None
    if not isinstance(sub, str) or "|" not in sub:
        return None
    parts = sub.split("|")
    return parts[1] or None if len(parts) > 1 else None


def _cursor_shape(p):
    """Normalise either Cursor response into the house usage shape, or None.

    Both the RPC and the REST form key the body by MODEL NAME, with
    numRequests used and maxRequestUsage as the cap, so the flagship pool has
    to be picked out rather than read from a fixed field. We take the pool
    with the largest declared cap: that is the plan's headline allowance,
    whereas picking the first key would depend on dict ordering nobody
    promised.
    """
    if not isinstance(p, dict):
        return None
    best = None
    for name, v in p.items():
        if not isinstance(v, dict):
            continue
        used = _num(v.get("numRequests", v.get("num_requests")))
        if used is None:
            continue
        lim = _num(v.get("maxRequestUsage", v.get("max_request_usage")))
        if best is None or (lim or 0) > (best[2] or 0):
            best = (name, used, lim)
    if best is None:
        return None
    name, used, lim = best
    out = {
        "provider": "cursor",
        "model_name": name,
        "month_pct": _pct_of(used, lim),
        "month_used": used,
        "month_limit": lim,
    }
    # startOfMonth is the period ANCHOR, not a reset instant. Cursor publishes
    # no reset timestamp, so month_reset is deliberately left out instead of
    # being computed from the anchor: "resets in 3 days" derived from an
    # assumed 30-day period would be a guess wearing a clock's clothes.
    anchor = p.get("startOfMonth") or p.get("start_of_month")
    if anchor:
        out["month_start"] = anchor
    return out


def _cursor_usage(cred):
    tok = _token(_vendor_cred(cred, "cursor", "cursorAuth"),
                 "accessToken", "access_token", "token")
    if not tok:
        return {"error": "no credential"}
    st, p, raw = _http_json(CURSOR_RPC, {
        "Authorization": "Bearer " + tok,
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "Accept": "application/json",
        "User-Agent": WIDGET_UA,
    }, data=b"{}")
    if st == 200:
        out = _cursor_shape(p)
        if out:
            out["source"] = "rpc"
            return out
    if st == 0:
        # The RPC never reached a server, so the REST host would not either,
        # and 'offline' is the honest answer. Retrying a second host here
        # would only turn one wrong diagnosis into two round trips.
        return {"error": "offline"}
    # RPC answered but not usefully. The REST form is the dashboard's own
    # older path and is worth one attempt before giving up, because the two
    # are versioned independently.
    uid = _cursor_user_id(tok)
    if not uid:
        return _usage_http_error(st, p) or {"error": "unrecognised response"}
    url = CURSOR_REST + "?" + urllib.parse.urlencode({"user": uid})
    st2, p2, raw2 = _http_json(url, {
        # Session cookie form is "<user id>::<jwt>" with the separator
        # percent-encoded, which is why this is built by hand rather than
        # handed to a cookie jar.
        "Cookie": ("WorkosCursorSessionToken=" + urllib.parse.quote(uid)
                   + "%3A%3A" + tok),
        "Accept": "application/json",
        "User-Agent": WIDGET_UA,
    })
    err = _usage_http_error(st2, p2)
    if err:
        return err
    out = _cursor_shape(p2)
    if not out:
        return {"error": "unrecognised response"}
    out["source"] = "rest"
    return out


def _cursor_refresh(cred):
    o = _vendor_cred(cred, "cursor", "cursorAuth")
    if not o:
        return None, "no credential"
    rt = _token(o, "refreshToken", "refresh_token")
    if not rt:
        return None, "login expired"
    body = json.dumps({
        "grant_type": "refresh_token",
        "client_id": CURSOR_CLIENT,
        "refresh_token": rt,
    }).encode()
    st, p, raw = _http_json(CURSOR_TOKEN, {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": WIDGET_UA,
    }, data=body)
    if st == 200 and isinstance(p, dict) and p.get("access_token"):
        # Whole credential back, in the shape it arrived in, for the same
        # reason as Claude: the refresh token may have rotated and a partial
        # write is how a working login gets bricked.
        new = json.loads(json.dumps(cred))
        n = new.get("cursor") if isinstance(new.get("cursor"), dict) else (
            new.get("cursorAuth") if isinstance(new.get("cursorAuth"), dict)
            else new)
        n["accessToken"] = p["access_token"]
        if p.get("refresh_token"):
            n["refreshToken"] = p["refresh_token"]
        return new, None
    return None, _classify(st, raw)


# ── GitHub Copilot adapter ──────────────────────────────────────────────────
def _copilot_pools_paid(snaps):
    """quota_snapshots -> {pool: {...}} for a paid seat."""
    pools = {}
    for name in ("premium_interactions", "chat", "completions"):
        s = snaps.get(name)
        if not isinstance(s, dict):
            continue
        if s.get("unlimited"):
            # An unlimited pool has no percentage. Reporting 0% would be a
            # number the user could misread as a measurement; the flag says
            # what is true.
            pools[name] = {"unlimited": True, "pct": None}
            continue
        ent, rem = _num(s.get("entitlement")), _num(s.get("remaining"))
        pct = None
        if s.get("percent_remaining") is not None:
            pct = _clamp_pct(100.0 - (_num(s.get("percent_remaining")) or 0.0))
        elif ent is not None and rem is not None:
            pct = _pct_of(ent - rem, ent)
        pools[name] = {
            "unlimited": False,
            "pct": pct,
            "used": None if (ent is None or rem is None) else ent - rem,
            "limit": ent,
        }
    return pools


def _copilot_pools_free(p):
    """The free-tier body -> the same pool dict.

    Free accounts do NOT get quota_snapshots. They get monthly_quotas (the
    allowance per pool), limited_user_quotas (what is LEFT) and
    limited_user_reset_date. The two shapes share no field names, so a reader
    that only knows the paid one finds nothing, fills in zeros, and shows a
    free account a full quota bar it does not have. An honest error would be
    bad; a confident wrong bar is worse, hence this second branch.
    """
    monthly = p.get("monthly_quotas")
    left = p.get("limited_user_quotas")
    if not isinstance(monthly, dict):
        return {}
    pools = {}
    for name in ("chat", "completions"):
        ent = _num(monthly.get(name))
        rem = _num((left or {}).get(name)) if isinstance(left, dict) else None
        if ent is None and rem is None:
            continue
        pools[name] = {
            "unlimited": False,
            "pct": None if (ent is None or rem is None)
                   else _pct_of(ent - rem, ent),
            "used": None if (ent is None or rem is None) else ent - rem,
            "limit": ent,
        }
    return pools


def _copilot_usage(cred):
    o = _vendor_cred(cred, "copilot", "github")
    tok = _token(o, "oauth_token", "token", "access_token", "accessToken")
    if not tok:
        return {"error": "no credential"}
    st, p, raw = _http_json(COPILOT_USER, {
        # "token <oauth>", not "Bearer": this endpoint is on the classic
        # GitHub OAuth scheme and rejects the Bearer form.
        "Authorization": "token " + tok,
        "Accept": "application/json",
        "Editor-Version": COPILOT_EDITOR,
        "User-Agent": WIDGET_UA,
    })
    err = _usage_http_error(st, p)
    if err:
        return err
    snaps = p.get("quota_snapshots")
    if isinstance(snaps, dict) and snaps:
        pools, tier = _copilot_pools_paid(snaps), "paid"
        reset = p.get("quota_reset_date")
    else:
        pools, tier = _copilot_pools_free(p), "free"
        reset = p.get("limited_user_reset_date")
    if not pools:
        # Neither shape was present. Saying so beats inventing a window.
        return {"error": "unrecognised response"}
    out = {"provider": "copilot", "tier": tier, "pools": pools}
    if p.get("copilot_plan"):
        out["plan"] = str(p["copilot_plan"]).upper()
    # Copilot meters per calendar month, so the headline window is the month.
    # premium interactions are the pool that actually runs out on a paid seat;
    # on free, chat is. Whichever is chosen, the rest stay visible in pools.
    for name in ("premium_interactions", "chat", "completions"):
        pool = pools.get(name)
        if pool and pool.get("pct") is not None:
            out["month_pct"] = pool["pct"]
            out["month_used"] = pool.get("used")
            out["month_limit"] = pool.get("limit")
            out["month_pool"] = name
            break
    if reset:
        out["month_reset"] = reset
    return out


# ── Windsurf / Devin adapter (one Cognition backend, two products) ──────────
def _cognition_usage(cred, pid, ide_name, wrappers):
    o = _vendor_cred(cred, *wrappers)
    key = _token(o, "apiKey", "api_key", "token", "access_token", "accessToken")
    if not key:
        return {"error": "no credential"}
    # The key goes in the BODY under metadata. That is not a mistake being
    # copied: this service takes no Authorization header at all, so sending
    # one and omitting the metadata block returns an unauthenticated error
    # that looks exactly like a bad key.
    body = json.dumps({"metadata": {
        "apiKey": key,
        "ideName": ide_name,
        "ideVersion": "1.0.0",
    }}).encode()
    st, p, raw = _http_json(COGNITION_STATUS, {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "Accept": "application/json",
        "User-Agent": WIDGET_UA,
    }, data=body)
    err = _usage_http_error(st, p)
    if err:
        return err
    ps = p.get("planStatus")
    if not isinstance(ps, dict) or not ps:
        return {"error": "unrecognised response"}
    out = {"provider": pid}

    # Two traps live in this response and both are handled explicitly.
    #
    # First: these percentages are REMAINING, not used. Passing them straight
    # through would show a nearly exhausted plan as nearly untouched -- an
    # inversion that looks plausible on every screenshot and is only caught
    # when someone hits a limit the widget said was fine.
    #
    # Second: this is proto3 JSON, which OMITS fields holding the zero value.
    # An absent percentage therefore does not mean "unknown", it means zero
    # remaining, i.e. one hundred percent consumed -- exactly the reading that
    # matters most. We only draw that conclusion when something else proves
    # the object was populated (the reset timestamp for the daily window, any
    # sibling field for the weekly one), so a genuinely empty response still
    # reports unknown rather than a fabricated 100%.
    def consumed(pct_key, anchors):
        if pct_key in ps:
            rem = _num(ps.get(pct_key))
            return None if rem is None else _clamp_pct(100.0 - rem)
        if any(k in ps for k in anchors):
            return 100.0
        return None

    daily_anchor = ("dailyQuotaResetAtUnix",)
    sess = consumed("dailyQuotaRemainingPercent", daily_anchor)
    if sess is not None:
        out["session_pct"] = sess
    reset = _num(ps.get("dailyQuotaResetAtUnix"))
    if reset:
        out["session_reset"] = reset
    week = consumed("weeklyQuotaRemainingPercent", tuple(
        k for k in ps if k != "weeklyQuotaRemainingPercent"))
    if week is not None:
        out["week_pct"] = week
    micros = _num(ps.get("overageBalanceMicros"))
    if micros is not None:
        # Micros: a millionth of a dollar. Kept as dollars because that is
        # what a human reads, and rounded to cents so floating point does not
        # print $3.0000000000000004.
        out["overage_usd"] = round(micros / 1000000.0, 2)
    if len(out) == 1:
        return {"error": "unrecognised response"}
    return out


def _windsurf_usage(cred):
    return _cognition_usage(cred, "windsurf", "windsurf",
                            ("windsurf", "codeium"))


def _devin_usage(cred):
    # Devin's key is documented as persistent and non-expiring, so a 401/403
    # here means the credential we were given is the wrong thing entirely --
    # a different product's key, or a truncated paste -- not that a refresh is
    # due. _usage_http_error already reports that as "token rejected" rather
    # than "login expired", which is precisely the distinction wanted.
    return _cognition_usage(cred, "devin", "devin", ("devin", "cognition"))


# ── Kimi Code adapter ───────────────────────────────────────────────────────
_KIMI_WINDOWS = {
    "five_hour": "session", "fivehour": "session", "5h": "session",
    "five_hourly": "session", "session": "session",
    "weekly": "week", "week": "week", "seven_day": "week",
    "monthly": "month", "month": "month",
}


def _kimi_window_rows(usages):
    """Yield (slot, row) pairs from either response generation.

    The current shape is a dict of window name -> object; an older count-based
    one is still in circulation as a list of objects carrying their own window
    name. Both are read here because a user mid-upgrade would otherwise see a
    blank row and no reason for it.
    """
    if isinstance(usages, dict):
        for name, row in usages.items():
            slot = _KIMI_WINDOWS.get(str(name).lower().replace("-", "_"))
            if slot and isinstance(row, dict):
                yield slot, row
    elif isinstance(usages, list):
        for row in usages:
            if not isinstance(row, dict):
                continue
            name = row.get("window") or row.get("type") or row.get("name")
            slot = _KIMI_WINDOWS.get(str(name).lower().replace("-", "_"))
            if slot:
                yield slot, row


def _kimi_usage(cred):
    k = _vendor_cred(cred, "kimi", "kimiAuth")
    tok = _token(k, "access_token", "accessToken", "token", "apiKey")
    if not tok:
        return {"error": "no credential"}
    st, p, raw = _http_json(KIMI_USAGE, {
        "Authorization": "Bearer " + tok,
        "Accept": "application/json",
        # The CLI also sends a device identifier. Its exact header name was
        # not read from a primary source, and inventing one that the service
        # ignores is harmless while inventing one it validates is not, so only
        # the platform hint is sent and the id is deliberately omitted.
        "x-msh-platform": "cli",
        "User-Agent": WIDGET_UA,
    })
    err = _usage_http_error(st, p)
    if err:
        return err
    usages = p.get("usages")
    if usages is None and isinstance(p.get("data"), dict):
        usages = p["data"].get("usages")
    out = {"provider": "kimi"}
    for slot, row in _kimi_window_rows(usages):
        pct = None
        # Ratio generation: 0..1 of the pool consumed.
        for key in ("ratio", "usage_ratio", "used_ratio", "usedRatio"):
            r = _num(row.get(key))
            if r is not None:
                pct = _clamp_pct(r * 100.0)
                break
        if pct is None:
            # Legacy count generation: used against a limit.
            pct = _pct_of(row.get("used", row.get("usage")),
                          row.get("limit", row.get("quota")))
        if pct is None:
            # A window Kimi did not report is ABSENT, not zero. Writing 0 here
            # would draw an empty bar for a pool we never measured, which is
            # indistinguishable from a pool that is genuinely untouched.
            continue
        out[slot + "_pct"] = pct
        reset = (row.get("reset_at") or row.get("resetAt")
                 or row.get("reset_time") or row.get("refresh_at"))
        if reset:
            out[slot + "_reset"] = reset
    if len(out) == 1:
        return {"error": "unrecognised response"}
    return out


# ── Cline adapter ───────────────────────────────────────────────────────────
_CLINE_WINDOWS = {"five_hour": "session", "weekly": "week",
                  "monthly": "month"}


def _cline_usage(cred):
    o = _vendor_cred(cred, "cline")
    tok = _token(o, "token", "access_token", "accessToken", "apiKey")
    if not tok:
        return {"error": "no credential"}
    st, p, raw = _http_json(CLINE_USAGE, {
        "Authorization": "Bearer " + tok,
        "Accept": "application/json",
        "User-Agent": WIDGET_UA,
    })
    err = _usage_http_error(st, p)
    if err:
        return err
    data = p.get("data")
    limits = (data or {}).get("limits") if isinstance(data, dict) else None
    if p.get("success") is False or not isinstance(limits, list):
        return {"error": "unrecognised response"}
    out = {"provider": "cline"}
    for row in limits:
        if not isinstance(row, dict):
            continue
        slot = _CLINE_WINDOWS.get(str(row.get("type") or "").lower())
        if not slot:
            continue
        pct = _clamp_pct(row.get("percentUsed"))
        if pct is None:
            continue
        out[slot + "_pct"] = pct
        # resetsAt is documented nullable -- a pool with no scheduled reset
        # sends null rather than omitting the key. The window is still real
        # and its percentage is still worth showing, so only the clock is
        # dropped.
        if row.get("resetsAt"):
            out[slot + "_reset"] = row["resetsAt"]
    if len(out) == 1:
        return {"error": "unrecognised response"}
    return out


_ADAPTERS = {
    "claude": {"usage": _claude_usage, "refresh": _claude_refresh},
    "codex": {"usage": _codex_usage, "refresh": _codex_refresh},
    "cursor": {"usage": _cursor_usage, "refresh": _cursor_refresh},
    # Copilot's OAuth token is long-lived and this endpoint accepts it as-is,
    # so there is no refresh flow to call. The reason is stated instead of
    # faking a success, so a caller never records a refresh that never ran.
    "copilot": {"usage": _copilot_usage,
                "refresh": lambda c: _no_refresh(c, "token does not expire")},
    "windsurf": {"usage": _windsurf_usage,
                 "refresh": lambda c: _no_refresh(c, "token does not expire")},
    "devin": {"usage": _devin_usage,
              "refresh": lambda c: _no_refresh(c, "token does not expire")},
    # Kimi's access token DOES expire and a refresh token sits beside it, but
    # the refresh endpoint was never read from a primary source. Guessing a
    # token URL is worse than admitting the gap: a wrong URL produces a 404
    # that reads as a provider outage, and a right-looking wrong one could
    # post the refresh token somewhere it does not belong.
    "kimi": {"usage": _kimi_usage,
             "refresh": lambda c: _no_refresh(
                 c, "refresh endpoint not verified")},
    "cline": {"usage": _cline_usage,
              "refresh": lambda c: _no_refresh(c, "token does not expire")},
}


# ── public dispatch ───────────────────────────────────────────────────────────
def fetch_usage(provider_id, cred):
    """Always a dict, never an exception.

    On failure the dict's ONLY key is 'error', so the UI can branch on
    presence rather than sniffing for sentinel values in the data. A planned
    provider answers 'not supported yet' -- honest, and impossible to mistake
    for a quota reading.
    """
    ad = _ADAPTERS.get(provider_id)
    if ad is None:
        return {"error": NOT_SUPPORTED if provider_id in _BY_ID
                else "unknown provider"}
    try:
        out = ad["usage"](cred)
        return out if isinstance(out, dict) else {"error": "bad adapter result"}
    except Exception as e:
        # Type name only: the credential is in scope of this call and some
        # exception strings echo request context back out.
        return {"error": "adapter error (%s)" % type(e).__name__}


def refresh_cred(provider_id, cred):
    """Returns (new_credential_or_None, reason_or_None).

    The new credential is the COMPLETE object in the shape it arrived in, not
    a delta and not just the token, because the caller persists it wholesale
    and both providers rotate the refresh token on every successful call.
    """
    ad = _ADAPTERS.get(provider_id)
    if ad is None:
        return None, (NOT_SUPPORTED if provider_id in _BY_ID
                      else "unknown provider")
    try:
        new, why = ad["refresh"](cred)
        return new, why
    except Exception as e:
        return None, "adapter error (%s)" % type(e).__name__
