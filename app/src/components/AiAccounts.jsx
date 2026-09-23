import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { Bar, pctTone } from './Bar.jsx'
import ProviderIcon, { providerAccent } from './ProviderIcons.jsx'

/**
 * The multi-account half of the AI usage card.
 *
 * One CLI, one credential: logging a second Claude account into the Claude CLI
 * destroys the first one's token. A vault on the Python side now keeps a copy
 * of each account's credentials so several can coexist, and this is the face of
 * it -- a list of registered accounts on the left, the selected account's
 * quotas on the right.
 *
 * Two things this file refuses to do, because both would be worse than showing
 * nothing. It never draws a percentage or a bar for a provider whose usage
 * endpoint does not exist (ten of the twelve), because a zeroed bar reads as
 * "no usage" rather than as "not implemented". And it never says "login
 * expired" for a failure that says nothing about the credential -- being
 * geo-blocked or offline is not a dead token, and sending the user back through
 * a re-login they did not need is the exact annoyance this feature exists to
 * end.
 */

/**
 * The twelve vendors, used only until the real registry arrives.
 *
 * The backend owns this list; this copy exists so the picker is not empty on
 * the first frame, and it carries the same honest statuses the backend does --
 * `live` for the two with verified, tested usage endpoints, `planned` for the
 * ten that are registered and not yet wired to anything.
 */
const FALLBACK_PROVIDERS = [
  { id: 'claude', name: 'Claude Code', status: 'live' },
  { id: 'codex', name: 'OpenAI Codex / ChatGPT', status: 'live' },
  { id: 'cursor', name: 'Cursor', status: 'live' },
  { id: 'copilot', name: 'GitHub Copilot', status: 'live' },
  { id: 'windsurf', name: 'Windsurf', status: 'live' },
  { id: 'devin', name: 'Devin', status: 'live' },
  { id: 'replit', name: 'Replit', status: 'planned' },
  { id: 'kimi', name: 'Kimi Code', status: 'live' },
  { id: 'glm', name: 'GLM Coding Plan', status: 'planned' },
  { id: 'cline', name: 'Cline', status: 'live' },
  { id: 'antigravity', name: 'Google Antigravity', status: 'planned' },
  { id: 'railway', name: 'Railway', status: 'planned' },
]

/** Every bridge call is optional: the UI has to render before the IPC lands. */
const bridge = {
  accounts: () => window.nw?.aiAccounts?.(),
  providers: () => window.nw?.aiProviders?.(),
  add: (provider, label) => window.nw?.aiAccountAdd?.(provider, label),
  remove: (id) => window.nw?.aiAccountRemove?.(id),
  rename: (id, label) => window.nw?.aiAccountRename?.(id, label),
  login: (id) => window.nw?.aiAccountLogin?.(id),
  refresh: (id) => window.nw?.aiAccountRefresh?.(id),
}

/** Resolve a call that may be a promise, may be undefined, and may throw. */
function settle(result, onValue) {
  if (result && typeof result.then === 'function') {
    result.then((v) => { if (v) onValue(v) }).catch(() => {})
  } else if (result) {
    onValue(result)
  }
}

/* ── refusals, pending commands, and one notice line ───────────────────────── */

/**
 * How long a command may stay pending before the row re-arms itself.
 *
 * Nothing here knows whether the sidecar is slow, wedged or gone, and there is
 * no reply that says "still working". A row that can never re-enable is worse
 * than one that re-enables early, so the clock is the backstop under both of
 * the other two ways a pending state clears.
 */
const PENDING_MS = 12000
/**
 * The backstop for a command that is a human in a browser, not a function call.
 *
 * A sign-in sends exactly two progress beats (`sandbox`, then `console`, from
 * sidecar.py:1723 and :1734) and then goes quiet for as long as the person
 * takes. Under PENDING_MS the add line re-armed itself twelve seconds in and
 * announced "got no answer -- nothing changed" while the sign-in window was
 * still open and waiting, which is both wrong and the most discouraging
 * possible moment to say it. Every exit from a sign-in -- completed, refused,
 * cancelled, timed out, vault write failed -- sends a terminal `ai_accounts`
 * message that clears the row, so this longer clock is the backstop for the
 * sidecar dying mid-login, not the ordinary path.
 */
const LOGIN_PENDING_MS = 300000
/** How long a notice stays fully legible before it begins to fade. */
const NOTICE_HOLD_MS = 3800
/** The fade itself. Must match the duration class on the notice element. */
const NOTICE_FADE_MS = 700
/**
 * Historical clip length. No longer used to cut text -- the notice line now
 * always wraps in full (see `clip` below) -- kept only as the width past
 * which the caller may want to reconsider layout. Raised well past the
 * sidecar's longest known message (the ~175-character duplicate-account
 * verdict) so nothing near today's real traffic is anywhere close to it.
 */
const NOTICE_MAX = 320

/**
 * `t: 'error'` messages this pane is the right surface for, and how to say so.
 *
 * Deliberately not every `where` the sidecar uses: one that is always followed
 * by an `ai_accounts` message carrying the same failure (`ai_account_login`,
 * sidecar.py:1680) would announce twice and overwrite its own better-worded
 * second line.
 */
const ERROR_LABEL = {
  ai_accounts: 'Account poll failed',
  ai_migrate: 'Adopting existing logins failed',
  ai_login_teardown: 'Sign-in cleanup failed — a sandbox may be left on disk',
}
const OWNED_ERRORS = new Set(Object.keys(ERROR_LABEL))

/** The key the add-account command is tracked under: it has no account yet. */
const ADD_KEY = 'nw:add'

/** Command names as the main process refuses them, in words a user recognises. */
const CMD_LABEL = {
  ai_account_add: 'Add account',
  ai_account_remove: 'Remove account',
  ai_account_rename: 'Rename',
  ai_account_login: 'Sign-in',
  ai_account_refresh: 'Refresh',
  ai_accounts_list: 'Account list',
  ai_providers: 'Provider list',
}

/**
 * Normalise a message that came from outside this file. Never shortens it.
 *
 * The error text is produced by the main process and could in principle be any
 * length. It used to be clipped with an ellipsis past `NOTICE_MAX`, but a
 * string that ends mid-word with no mark reads as complete when it is not, and
 * silently dropping the tail of an error is how the informative half of a
 * message disappears without anyone noticing. The notice element wraps
 * (`break-words`) instead, so the full text is always the one shown.
 */
function clip(text) {
  return String(text ?? '').replace(/\s+/g, ' ').trim()
}

/**
 * The mark a row wears while its command is in flight.
 *
 * Three characters of animated punctuation rather than a spinner: at this size
 * a rotating glyph is a smudge, and this sits on the existing baseline without
 * changing any row's height. `aria-label` carries the meaning the dots do not.
 */
function Busy({ className = '' }) {
  return (
    <span
      role="status"
      aria-label="working"
      title="Sent — waiting for the sidecar"
      className={'glass-text shrink-0 animate-pulse text-[9px] leading-snug text-accent-2 ' + className}
    >
      &#8226;&#8226;&#8226;
    </span>
  )
}

/**
 * Everything about one account that a command could plausibly change.
 *
 * Compared before and after each `ai` message to decide whether the command a
 * row is waiting on has landed. Identity is no use -- the whole array is rebuilt
 * every poll -- and a plain "clear on any message" rule would drop the pending
 * mark on the unrelated fifteen-minute poll that happened to arrive first.
 */
function accountSig(a) {
  if (!a) return ''
  return [
    a.status ?? '',
    a.error ?? '',
    a.label ?? '',
    a.needs_login ? 1 : 0,
    a.held ? 1 : 0,
    JSON.stringify(a.usage ?? null),
  ].join('|')
}

/** The roster as a whole, for add and remove: those change the membership. */
function rosterSig(list) {
  return list.map((a) => a.id).join(',')
}

/**
 * Pending commands, and the one line that reports a refusal.
 *
 * A command goes to the sidecar and the answer comes back later on a broadcast
 * nobody can correlate to the click, so the row marks itself busy and waits.
 * There are exactly three ways that wait ends, and all three are wired here,
 * because a control that is disabled forever is the worst outcome available:
 *
 *   1. the account (or the roster) changes -- the command landed;
 *   2. an `ai_error` arrives -- the main process refused the command, and said
 *      so precisely so the row could re-arm itself;
 *   3. PENDING_MS elapses -- nothing came back at all.
 *
 * The notice is deliberately singular. Ten stale refusals stacked up in a
 * 372px-wide widget is not a log, it is a wall; one line that fades is read.
 */
function useAiCommands(accounts) {
  const [pending, setPending] = useState({})
  const [notice, setNotice] = useState(null)
  // The sidecar's last login `stage`, or null when no sign-in is running. Read
  // from the same envelope as everything else here, so it cannot disagree with
  // the pending table the way a second subscription eventually would.
  const [stage, setStage] = useState(null)
  const timers = useRef({})
  const fade = useRef({ hold: 0, drop: 0 })
  // The array as of this render, and the pending table as of this render, both
  // readable from inside a callback that was created during an earlier one.
  const live = useRef(accounts)
  const held = useRef(pending)
  live.current = accounts
  held.current = pending

  const forget = (keys) => {
    for (const k of keys) {
      const t = timers.current[k]
      if (t) clearTimeout(t)
      delete timers.current[k]
    }
    setPending((p) => {
      const next = { ...p }
      let changed = false
      for (const k of keys) {
        if (k in next) { delete next[k]; changed = true }
      }
      return changed ? next : p
    })
  }

  /**
   * Show one line. `bad` is the difference between a failure and a progress
   * beat, and it is carried rather than assumed: this line is now the only
   * place a sign-in reports itself, and rendering "a sign-in window is open"
   * in the same alarm red as "vault write failed" teaches the user to read
   * every notice as a fault.
   */
  const announce = (text, bad = true) => {
    clearTimeout(fade.current.hold)
    clearTimeout(fade.current.drop)
    const body = clip(text)
    setNotice({ text: body, bad, fading: false })
    // A short progress beat has been read by 3.8s. A refusal has not: the
    // duplicate verdict and the wrong-account explanation are multi-sentence
    // instructions about what to do in the browser, and they are the only
    // record of why the sign-in the user just sat through changed nothing.
    // Fading those out from under someone mid-sentence is how the explanation
    // gets lost and the whole attempt reads as a silent failure again.
    const hold = NOTICE_HOLD_MS * (body.length > 90 ? 4 : 1)
    fade.current.hold = setTimeout(
      () => setNotice((n) => (n ? { ...n, fading: true } : n)), hold)
    fade.current.drop = setTimeout(() => setNotice(null), hold + NOTICE_FADE_MS)
  }

  /** (Re)start one key's backstop clock. The only place that timer is built. */
  const arm = (key, cmd, ms) => {
    clearTimeout(timers.current[key])
    timers.current[key] = setTimeout(() => {
      forget([key])
      announce(`${CMD_LABEL[cmd] ?? 'Command'} got no answer — nothing changed, try again.`)
    }, ms)
  }

  /** Push every named row's backstop out, for work that legitimately runs long. */
  const extend = (keys, ms) => {
    for (const k of keys) {
      const row = held.current[k]
      if (row) arm(k, row.cmd, ms)
    }
  }

  /** Mark a row busy, start its timeout, then send the command. */
  const begin = (key, cmd, run) => {
    const sig = key === ADD_KEY
      ? rosterSig(live.current)
      : accountSig(live.current.find((a) => a.id === key))
    setPending((p) => ({ ...p, [key]: { cmd, sig } }))
    arm(key, cmd, PENDING_MS)
    run()
  }

  // (1) the command landed: the thing it was going to change has changed.
  useEffect(() => {
    setPending((p) => {
      const keys = Object.keys(p)
      if (!keys.length) return p
      const next = {}
      let changed = false
      for (const k of keys) {
        const now = k === ADD_KEY
          ? rosterSig(accounts)
          : accountSig(accounts.find((a) => a.id === k))
        if (now !== p[k].sig) {
          changed = true
          const t = timers.current[k]
          if (t) clearTimeout(t)
          delete timers.current[k]
        } else {
          next[k] = p[k]
        }
      }
      return changed ? next : p
    })
  }, [accounts])

  // (2) the main process refused it. Subscribed through `window.nw.onData` --
  // the same single stream useSidecar.js reduces, and the only message path the
  // preload exposes. `onData` is an ipcRenderer.on underneath, so a second
  // subscriber is additive rather than a second mechanism, and it hands back
  // its own unsubscribe to call on unmount.
  //
  // The subscription is made once and reads the pending table through a ref:
  // re-subscribing whenever a row goes busy would leave a gap between the
  // unsubscribe and the subscribe, and the refusal that matters arrives in
  // exactly that window.
  useEffect(() => {
    const off = window.nw?.onData?.((msg) => {
      if (!msg) return
      if (msg.t === 'ai_error') {
        setStage(null)
        const cmd = typeof msg.cmd === 'string' ? msg.cmd : ''
        const keys = Object.keys(held.current)
        // `ai_error` names the command, not the account, so every row waiting on
        // that command is released. When the name matches nothing -- an unknown
        // command, or a refusal for something this pane never sent -- everything
        // pending is released anyway: an extra re-enable costs one wasted click,
        // a missed one costs the button for the rest of the session.
        const hit = keys.filter((k) => held.current[k]?.cmd === cmd)
        if (keys.length) forget(hit.length ? hit : keys)
        announce(`${CMD_LABEL[cmd] ?? 'Command'} refused — ${msg.error ?? 'invalid arguments'}`)
        return
      }
      if (msg.t === 'ai_accounts') {
        // This pane used to hear nothing between sending `ai_account_add` and
        // either the roster changing or the 12s PENDING_MS timeout, so a
        // duplicate-account verdict or a login-in-progress notice from the
        // sidecar never reached the user -- they just watched the timeout fire.
        // Field names are the sidecar's, read from its emitter rather than
        // guessed: `err` for a failure, `note` for a duplicate verdict or a
        // login progress beat. An earlier draft read `notice`/`message`,
        // which no emitter ever sets, so every one of these was silently
        // dropped -- the same blackout this branch exists to end.
        //
        // `err` is checked first because a message carrying both is a
        // failure that happens to explain itself, and the failure is the
        // part the user must not miss.
        const failed = typeof msg.err === 'string' && msg.err !== ''
        const text = failed ? msg.err
          : ((typeof msg.note === 'string' && msg.note) || null)
        if (text) announce(text, failed)

        if (msg.status === 'login') {
          // Still running. This is the ONLY non-terminal value `status` ever
          // takes on this envelope (sidecar.py:1337), so it is the only one
          // that may leave a row pending -- and it must push that row's clock
          // out, because what follows a beat is a human in a browser.
          setStage(typeof msg.stage === 'string' ? msg.stage : null)
          extend(Object.keys(held.current), LOGIN_PENDING_MS)
          return
        }

        // Everything else on this envelope is a finished command. The sidecar
        // emits `ai_accounts` for exactly two reasons -- a login progress beat
        // (handled above) or the answer to a command -- so "not a beat" is a
        // complete and checkable definition of terminal, and it covers every
        // outcome by construction rather than by a list of strings that the
        // next sidecar change would silently fall off the end of.
        //
        // This replaces a check for `status === 'refused'` alone, which read
        // only the ADD_KEY row. A re-auth refused for an account mix-up
        // (sidecar.py:1797) is sent while the pending key is that ACCOUNT's
        // id, never ADD_KEY, so its row kept spinning for the full 12s and
        // then claimed it "got no answer" -- the same self-contradiction the
        // refused branch was added to remove, surviving on the other path.
        // Every plain `err` exit -- "sign-in cancelled", "login not
        // completed", "vault write failed", "no such account" -- carries no
        // status at all and was left spinning by that check too.
        //
        // Releasing every pending row rather than a guessed subset follows the
        // rule the `ai_error` branch above already states: this envelope names
        // no command, an extra re-enable costs one wasted click, and a missed
        // one costs the button for the rest of the session.
        // The sign-in is over however it ended, so the stage it reached must
        // not outlive it -- a stale 'console' would keep claiming a sign-in
        // window is open after the one that was open has closed.
        setStage(null)
        const done = Object.keys(held.current)
        if (done.length) forget(done)
        return
      }

      // The sidecar's own `t: 'error'` envelope, for the failures that have no
      // `ai_accounts` message behind them. `ai_accounts` (sidecar.py:459) is a
      // whole-roster poll that threw; `ai_migrate` (:732) is first-run
      // adoption; `ai_login_teardown` (:1845) means a sandbox holding a live
      // credential may still be on disk. useSidecar.js collects these into
      // `state.errors`, which nothing renders, so without this they reached a
      // user-visible surface nowhere at all.
      if (msg.t === 'error') {
        const where = typeof msg.where === 'string' ? msg.where : ''
        if (OWNED_ERRORS.has(where)) {
          announce(`${ERROR_LABEL[where]} — ${msg.err ?? 'failed'}`)
          return
        }
        // A command this build sends that this sidecar does not implement
        // (sidecar.py:1975). Nothing else answers it, so the row would spin to
        // its timeout and blame the network for a version mismatch.
        if (where === 'stdin' && typeof msg.err === 'string'
            && msg.err.startsWith('unknown command:')) {
          const name = msg.err.slice('unknown command:'.length).trim()
          if (name in CMD_LABEL) {
            const keys = Object.keys(held.current)
            const hit = keys.filter((k) => held.current[k]?.cmd === name)
            if (hit.length) forget(hit)
            announce(`${CMD_LABEL[name]} is not available in this sidecar build.`)
          }
        }
      }
    })
    return () => { if (typeof off === 'function') off() }
  }, [])

  // Timers outlive React's own bookkeeping unless they are told not to.
  useEffect(() => () => {
    for (const t of Object.values(timers.current)) clearTimeout(t)
    timers.current = {}
    clearTimeout(fade.current.hold)
    clearTimeout(fade.current.drop)
  }, [])

  return { pending, notice, stage, begin, announce }
}

/* ── the data, made uniform ────────────────────────────────────────────────── */

/**
 * The accounts array, or a stand-in built from the legacy two keys.
 *
 * `s.ai.claude` and `s.ai.codex` stay in the payload for the compact and tab
 * views, so while an older sidecar is running -- or before the vault has been
 * populated -- this card still has something true to show rather than an empty
 * list that looks like the feature broke.
 */
function accountsOf(ai) {
  if (Array.isArray(ai?.accounts) && ai.accounts.length) return ai.accounts
  const out = []
  for (const [id, provider, label, d] of [
    ['legacy:claude', 'claude', 'Claude', ai?.claude],
    ['legacy:codex', 'codex', 'GPT', ai?.codex],
  ]) {
    if (!d) continue
    const expired = /LOGIN_EXPIRED/.test(d.err ?? '')
    out.push({
      id,
      provider,
      label,
      status: d.err && !d.held ? 'error' : 'ok',
      usage: d,
      error: d.err ?? null,
      expires_at: null,
      needs_login: expired || Boolean(d.setup),
      held: Boolean(d.held),
    })
  }
  return out
}

function providersOf(list) {
  return Array.isArray(list) && list.length ? list : FALLBACK_PROVIDERS
}

function providerName(providers, id) {
  return providers.find((p) => p.id === id)?.name ?? id ?? 'unknown'
}

/** A provider with no verified usage endpoint. Never rendered as a reading. */
function isPlanned(account, providers) {
  if (account?.status === 'planned') return true
  const p = providers.find((q) => q.id === account?.provider)
  return p ? p.status !== 'live' : false
}

/* ── failures, kept apart ──────────────────────────────────────────────────── */

/**
 * Which of the four things went wrong, as a kind rather than a sentence.
 *
 * The backend distinguishes them and so must the UI: collapsing them into one
 * warning is how a user ends up re-authenticating a perfectly good account
 * because their VPN was down. `expired` is the only kind that arms a sign-in
 * click; `offline` says nothing at all about the credential; `http` is the
 * provider's own answer and carries its status code so it can be looked up;
 * `planned` is not a failure, it is an absence of an implementation.
 *
 * An error string that matches none of these is passed through verbatim rather
 * than being forced into the nearest bucket -- an unrecognised message is still
 * information, and guessing at it is how the wrong message gets shown.
 */
function classify(account, planned) {
  const raw = (account?.error ?? '').trim()
  if (planned) {
    return { kind: 'planned', short: 'not wired up yet', full: 'No usage endpoint for this provider yet' }
  }
  if (account?.needs_login || /login[_\s-]?expired|unauthori[sz]ed|not logged in|no credential/i.test(raw)) {
    return {
      kind: 'expired',
      short: 'login expired',
      full: raw && !/login[_\s-]?expired/i.test(raw) ? `Login expired — ${raw}` : 'Login expired — click to sign in',
    }
  }
  if (/offline|geo|vpn|blocked|unreachable|dns|timed? ?out|network|connection/i.test(raw)) {
    return { kind: 'offline', short: 'offline', full: `Offline — ${raw}. Your login is untouched.` }
  }
  const code = /\b(\d{3})\b/.exec(raw)
  if (raw) {
    return {
      kind: 'http',
      short: code ? `refresh failed · ${code[1]}` : 'refresh failed',
      full: code ? `Refresh failed (HTTP ${code[1]}) — ${raw}` : `Refresh failed — ${raw}`,
    }
  }
  return null
}

/* ── small formatters ──────────────────────────────────────────────────────── */

/**
 * A reset marker, however the backend phrased it, as time remaining.
 *
 * The sidecar already converts these (see `_usage_relative`), so in practice
 * this passes a ready-made `2h 27m` straight through. It converts anyway
 * because a provider adapter can be added without touching the sidecar, and
 * the failure is silent and ugly: an unconverted marker renders as
 * `resets 2026-09-20T13:00:00Z`, and a wall-clock time is barely better --
 * the question a quota row answers is how long is left, not when.
 *
 * Anything that is neither epoch seconds nor an ISO instant is left alone;
 * some adapters answer with a phrase, and mangling one would lose it.
 */
function resetLabel(v) {
  if (v == null || v === '') return ''
  const s = String(v)
  let ms = null
  if (typeof v === 'number' || /^\d{9,}$/.test(s)) ms = Number(v) * 1000
  else if (!Number.isNaN(Date.parse(s))) ms = Date.parse(s)
  if (ms == null || Number.isNaN(ms)) return s

  const left = ms - Date.now()
  if (left <= 0) return 'now'
  const mins = Math.floor(left / 60000)
  const d = Math.floor(mins / 1440)
  const h = Math.floor((mins % 1440) / 60)
  const m = mins % 60
  if (d) return `${d}d ${h}h`
  if (h) return `${h}h ${m}m`
  return `${m}m`
}

/** How long this account's stored credential has left, when that is known. */
function expiryLabel(seconds) {
  if (!seconds) return ''
  const left = Number(seconds) * 1000 - Date.now()
  if (left <= 0) return 'token expired'
  const days = Math.floor(left / 86400000)
  if (days >= 1) return `token good for ${days}d`
  const hours = Math.floor(left / 3600000)
  if (hours >= 1) return `token good for ${hours}h`
  return `token good for ${Math.max(1, Math.round(left / 60000))}m`
}

/**
 * The quota rows an account's usage object actually contains.
 *
 * Claude and Codex name their fields differently, and a future provider will
 * name them differently again, so the known pairs are listed and anything else
 * is discovered by looking for a `*_pct` key. A usage object that yields no
 * rows produces none -- the caller says so in words instead of drawing an empty
 * bar, which would be indistinguishable from zero usage.
 */
const KNOWN_ROWS = {
  claude: [
    ['5h', 'session_pct', 'session_reset'],
    ['week', 'week_pct', 'week_reset'],
    ['model', 'model_pct', 'model_reset'],
  ],
  codex: [
    ['5h', 'sess_pct', 'sess_reset_ts'],
    ['week', 'week_pct', 'week_reset_ts'],
  ],
}

function usageRows(account) {
  const u = account?.usage
  if (!u || typeof u !== 'object') return []
  const known = KNOWN_ROWS[account.provider]
  const rows = []
  if (known) {
    for (const [name, pctKey, resetKey] of known) {
      if (u[pctKey] == null) continue
      const label = pctKey === 'model_pct' && u.model_name ? u.model_name : name
      rows.push({ key: pctKey, label, pct: u[pctKey], reset: resetLabel(u[resetKey]) })
    }
    if (rows.length) return rows
  }
  for (const [k, v] of Object.entries(u)) {
    if (!k.endsWith('_pct') || typeof v !== 'number') continue
    const stem = k.slice(0, -4)
    const reset = u[`${stem}_reset`] ?? u[`${stem}_reset_ts`]
    rows.push({ key: k, label: stem.replace(/_/g, ' '), pct: v, reset: resetLabel(reset) })
  }
  return rows
}

/* ── pieces ────────────────────────────────────────────────────────────────── */

/**
 * A status word small enough for the list column.
 *
 * Colour carries the same information as the word, because at this size the
 * word is read as a shape first.
 */
function statusChip(account, planned, fault) {
  if (planned) return { text: 'planned', tone: 'text-faint' }
  if (fault?.kind === 'expired') return { text: 'sign in', tone: 'text-warn' }
  if (fault?.kind === 'offline') return { text: 'offline', tone: 'text-muted' }
  // A short word, like every other branch here. `fault.short` is only short
  // by comparison with `fault.full` -- `refresh failed · 429` is twenty
  // characters, and in a 118px roster column that is most of the row. The
  // reason belongs in the title and in the detail pane, both of which carry
  // `fault.full`; the roster only has to say that something is wrong.
  if (fault) return { text: 'failed', tone: 'text-bad' }
  if (account?.held) return { text: 'stale', tone: 'text-muted' }
  return { text: 'ok', tone: 'text-good' }
}

/* ── scroll plumbing shared by the two lists ─────────────────────────── */

/**
 * How many account rows the roster shows before it begins to scroll.
 *
 * Four, because the previous height showed two and a half and cut the third
 * row through the middle. A half-row is the worst height a list can have: it
 * is neither a complete list nor an obvious invitation to scroll, and the user
 * cannot tell whether the sliced row is the last one.
 */
const VISIBLE_ACCOUNT_ROWS = 4

/**
 * The exact height of the first `count` rows, so a scrolling list can cut
 * between rows instead of through one.
 *
 * It is measured rather than written down because an account row has no fixed
 * size: a long label wraps onto a second line, and the whole shell is scaled
 * with CSS `zoom` over a very wide range, so any pixel constant here would be
 * wrong the moment either changed.
 *
 * The one subtlety is which pixels are being counted. `getBoundingClientRect`
 * reports zoomed pixels, while an inline `maxHeight` is interpreted in the
 * element's own unzoomed pixels, so the two cannot be mixed directly. The
 * container's own rect-to-offsetHeight ratio is the zoom factor currently in
 * force, measured from the DOM rather than read from a setting this component
 * has no access to, and dividing by it converts the measurement back into the
 * space the style property expects.
 */
function useWholeRowHeight(ref, count, signature) {
  const [height, setHeight] = useState(null)

  const measure = useCallback(() => {
    const el = ref.current
    if (!el) return
    const rows = Array.from(el.children).filter((n) => n.dataset && n.dataset.row === 'account')
    if (rows.length === 0) {
      setHeight(null)
      return
    }
    const zoom = el.offsetHeight > 0 ? el.getBoundingClientRect().height / el.offsetHeight : 1
    const factor = zoom > 0.01 ? zoom : 1
    const first = rows[0].getBoundingClientRect().top
    const last = rows[Math.min(count, rows.length) - 1].getBoundingClientRect().bottom
    // Rounded down to a hundredth: rounding up could expose a one-pixel sliver
    // of the row below, which is the exact defect this measurement exists to
    // remove.
    const next = Math.floor(((last - first) / factor) * 100) / 100
    if (!Number.isFinite(next) || next <= 0) return
    setHeight((prev) => (prev !== null && Math.abs(prev - next) < 0.5 ? prev : next))
  }, [ref, count])

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return undefined
    measure()
    // A row that rewraps changes height without resizing the container, so the
    // children are observed too; `signature` covers the case where the set of
    // children changes entirely and there is nothing left to observe.
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    for (const child of el.children) ro.observe(child)
    return () => ro.disconnect()
  }, [ref, measure, signature])

  return height
}

/**
 * Drive the shared `.nw-fade-edges` contract declared in `index.css`.
 *
 * That class feathers the top and bottom of a scrolling box with a mask, and
 * it reads `data-scroll-top` / `data-scroll-bottom` to know which end still has
 * content hidden past it. Neither a content change nor a size change emits a
 * scroll event, so the element and its children are observed as well as
 * listened to; both attributes absent is the correct state for a list short
 * enough not to scroll at all.
 */
function useScrollFade(ref, signature) {
  const [edges, setEdges] = useState({ top: false, bottom: false })

  const measure = useCallback(() => {
    const el = ref.current
    if (!el) return
    const top = el.scrollTop > 0
    // The pixel of slack is load-bearing: under `zoom` these are fractional,
    // and a list scrolled hard to the bottom lands a hair short of its own
    // height, which would leave the bottom fade on forever.
    const bottom = el.scrollTop + el.clientHeight < el.scrollHeight - 1
    setEdges((prev) => (prev.top === top && prev.bottom === bottom ? prev : { top, bottom }))
  }, [ref])

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return undefined
    measure()
    el.addEventListener('scroll', measure, { passive: true })
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    for (const child of el.children) ro.observe(child)
    return () => {
      el.removeEventListener('scroll', measure)
      ro.disconnect()
    }
  }, [ref, measure, signature])

  return edges
}

/**
 * The ring every interactive element in this pane wears on keyboard focus.
 *
 * `focus-visible` rather than `focus` so a mouse click does not leave a ring
 * behind, and it is kept in one constant so the roster, the picker and the
 * detail buttons cannot drift apart from each other.
 */
const FOCUS_RING = 'outline-none focus-visible:ring-1 focus-visible:ring-white/45 '

/** The search field shared by the account list and the provider picker. */
function SearchField({ value, onChange, placeholder, onEscape, autoFocus }) {
  return (
    <input
      type="text"
      value={value}
      spellCheck={false}
      autoFocus={autoFocus}
      placeholder={placeholder}
      aria-label={placeholder}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        // Escape clears a query first and only closes the picker once the box
        // is already empty, so the key never destroys more than the user meant.
        if (e.key !== 'Escape') return
        if (value) {
          e.stopPropagation()
          onChange('')
        } else if (onEscape) {
          e.stopPropagation()
          onEscape()
        }
      }}
      className={'glass-text min-w-0 flex-1 rounded-[6px] border border-edge-soft bg-white/[0.06] '
        + 'px-1.5 py-[2px] text-[9.5px] text-ink transition '
        + 'placeholder:text-faint hover:border-white/25 focus:border-white/35 focus:bg-white/12 '
        + FOCUS_RING}
    />
  )
}

/**
 * The line a list shows when it has nothing to show.
 *
 * A query that matches nothing offers the way back out of it, because the
 * alternative is a user staring at an empty box wondering whether the pane
 * broke or the search did.
 */
function EmptyNote({ children, onClear }) {
  return (
    <div className="glass-text px-1.5 py-[3px] text-[9.5px] leading-snug text-faint">
      {children}
      {onClear && (
        <button
          type="button"
          onClick={onClear}
          className={'ml-1 rounded-[4px] px-1 text-[9.5px] leading-snug text-muted underline '
            + 'decoration-dotted transition hover:text-ink ' + FOCUS_RING}
        >
          clear
        </button>
      )}
    </div>
  )
}

/**
 * One row in the account list.
 *
 * The label wraps rather than being cut. Three Claude accounts are told apart
 * only by what the user called them, so losing the tail of "work — billing"
 * would lose the distinction the row exists to make.
 */
function AccountRow({ account, providers, selected, onSelect, busy }) {
  const planned = isPlanned(account, providers)
  const fault = classify(account, planned)
  const chip = statusChip(account, planned, fault)
  const name = providerName(providers, account.provider)

  return (
    <button
      type="button"
      data-row="account"
      aria-current={selected ? 'true' : undefined}
      onClick={() => onSelect(account.id)}
      title={`${name}${account.label ? ` — ${account.label}` : ''}\n${fault ? fault.full : 'Healthy'}`}
      className={'flex w-full items-start gap-1 rounded-[6px] px-1.5 py-[3px] text-left transition '
        + FOCUS_RING
        + (selected ? 'bg-white/14 ' : 'hover:bg-white/[0.07] ')}
    >
      {/* The mark does the provider-identification work the second line used to
          do alone: twelve near-identical text rows are hard to scan, one glyph
          is not. It is muted for a provider with no endpoint, so the row still
          admits what it is. */}
      <ProviderIcon id={account.provider} size={12} muted={planned} className="mt-[1px]" />
      {/* Both lines truncate rather than wrap. `break-words` here was breaking
          inside words, which is only ever reached when a line has almost no
          width left -- and then it does not wrap, it shatters: `OpenAI Codex /
          ChatGPT` came out as eight lines of one or two characters, eight rows
          tall, which dragged the whole widget from 877px to 1183px. The full
          text is on the row's `title`, so an ellipsis costs nothing. */}
      <span className="min-w-0 flex-1">
        <span className="glass-text block truncate text-[10px] leading-snug text-ink-2">
          {account.label || name}
        </span>
        <span className="glass-text block truncate text-[9px] leading-snug text-faint">
          {name}
        </span>
      </span>
      {busy ? (
        /* The status word is replaced rather than crowded out: the roster column
           is 118px and two indicators side by side would wrap the row. While a
           command is in flight the old status is stale anyway. */
        <Busy className="mt-[1px]" />
      ) : (
        /* Shrinkable, not `shrink-0`. `fault.short` is not always short --
           `refresh failed · 429` is twenty characters -- and an unshrinkable
           chip of that width in a 118px column leaves the name nothing to
           live in. The name is what identifies the row, so it wins the space
           and the chip gives way; the full reason is in the row's title. */
        <span
          className={'glass-text min-w-0 max-w-[52px] shrink truncate text-right '
            + 'text-[9px] leading-snug ' + chip.tone}
        >
          {chip.text}
        </span>
      )}
    </button>
  )
}

/**
 * The add-account picker: all twelve vendors, searchable, honestly labelled.
 *
 * It is drawn as a sheet laid over this pane's own box rather than as a panel
 * appended below it, and that is a hard requirement rather than a preference.
 * Every card in this widget is frosted by its own borderless window sized to
 * the card's rectangle, so anything that renders past the card's bottom edge
 * has no frost behind it and floats over the bare desktop. Absolute
 * positioning against the pane root, with `max-h-full` and `overflow-hidden`,
 * makes escaping the card geometrically impossible: the sheet can be no taller
 * than the box it is positioned inside, and its list scrolls instead.
 *
 * A provider with no usage endpoint is no longer selectable. Registering one
 * would create an account row that can never show a number, and the four that
 * remain unwired are unwired precisely because their response fields could not
 * be verified -- so the row is dimmed, marked, and inert rather than inviting.
 */
function ProviderPicker({ providers, onPick, onClose, loading, busy }) {
  const [q, setQ] = useState('')
  const listRef = useRef(null)
  const needle = q.trim().toLowerCase()
  const shown = providers.filter((p) => !needle
    || p.name.toLowerCase().includes(needle)
    || p.id.toLowerCase().includes(needle))
  const fade = useScrollFade(listRef, `${shown.length}:${loading ? 1 : 0}`)

  // Escape closes the sheet from anywhere inside it. The search box stops the
  // event first while it still has a query to clear, so the two never fight.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Add an AI account"
      className="absolute inset-x-0 top-0 z-10 flex max-h-full flex-col overflow-hidden
                 rounded-[8px] border border-edge-soft bg-black/70 p-1"
    >
      <div className="flex shrink-0 items-center gap-1">
        <SearchField
          autoFocus
          value={q}
          onChange={setQ}
          onEscape={onClose}
          placeholder="Find a provider…"
        />
        <button
          type="button"
          onClick={onClose}
          title="Close the provider list"
          aria-label="Close the provider list"
          className={'shrink-0 rounded-[5px] px-1 py-[2px] text-[10px] leading-none text-muted '
            + 'transition hover:bg-white/10 hover:text-accent ' + FOCUS_RING}
        >
          &#10005;
        </button>
      </div>
      <div
        ref={listRef}
        data-scroll-top={fade.top}
        data-scroll-bottom={fade.bottom}
        className="nw-fade-edges mt-1 min-h-0 flex-1 overflow-y-auto"
      >
        {/* The registry legitimately arrives empty on the first frame -- the
            sidecar emits providers=[] with err="unavailable" before it has read
            anything. An empty box reads as a broken picker, so it says which of
            the two it is. */}
        {loading && (
          <EmptyNote>Loading providers… showing the built-in list meanwhile.</EmptyNote>
        )}
        {!loading && shown.length === 0 && (
          <EmptyNote onClear={() => setQ('')}>No provider matches “{q}”</EmptyNote>
        )}
        {shown.map((p) => {
          const live = p.status === 'live'
          const blocked = busy || !live
          return (
            <button
              key={p.id}
              type="button"
              disabled={blocked}
              aria-disabled={blocked || undefined}
              onClick={() => { if (!blocked) onPick(p) }}
              title={p.hint || (live
                ? `Sign in to ${p.name}`
                : `${p.name} is registered, but its usage endpoint is not wired up yet, `
                  + 'so an account added for it could never show a number.')}
              className={'flex w-full items-start gap-1 rounded-[5px] px-1.5 py-[3px] text-left '
                + 'transition ' + FOCUS_RING
                + (live
                  ? (busy ? 'cursor-default opacity-50 ' : 'hover:bg-white/12 ')
                  : 'cursor-not-allowed select-none opacity-55 ')}
            >
              <ProviderIcon id={p.id} size={12} muted={!live} className="mt-[1px]" />
              {/* Name and status sit in one wrapping line rather than at
                  opposite ends of a justified row. Pushed apart they read as
                  two unrelated columns; side by side the label is obviously
                  about the provider it follows. */}
              <span className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-1 gap-y-[1px]">
                {/* The accent only tints the name of a provider that actually
                    works. Tinting all twelve would turn the list into a colour
                    chart and cost the live/planned distinction its only cue. */}
                <span
                  className={'glass-text min-w-0 break-words text-[9.5px] leading-snug '
                    + (live ? '' : 'text-muted')}
                  style={live ? { color: providerAccent(p.id) } : undefined}
                >
                  {p.name}
                </span>
                {!live && (
                  <span
                    className="glass-text shrink-0 rounded-full border border-edge-soft
                               bg-white/[0.06] px-1 text-[9px] leading-snug text-faint"
                  >
                    not wired up yet
                  </span>
                )}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

/** One quota line in the detail pane. Only ever drawn for a real number. */
function QuotaRow({ label, pct, reset, dim }) {
  return (
    <div className={'flex items-center gap-1.5 py-[1.5px] ' + (dim ? 'opacity-55' : '')}>
      <span className="glass-text w-[44px] shrink-0 truncate text-[10px] text-ink-2" title={label}>
        {label}
      </span>
      <span className={'glass-text w-[28px] shrink-0 text-right text-[10.5px] font-medium '
        + 'tabular-nums ' + (dim ? 'text-muted' : pctTone(pct))}>
        {`${Math.round(pct)}%`}
      </span>
      <span className="w-[44px] shrink-0"><Bar value={pct} /></span>
      <span className="glass-text min-w-0 flex-1 truncate text-[9.5px] text-faint" title={reset || undefined}>
        {reset ? `resets ${reset}` : ''}
      </span>
    </div>
  )
}

/**
 * The selected account: what it is, what it has left, and what is wrong.
 *
 * The sign-in affordance is a button only when signing in is the actual remedy.
 * When the account is healthy, or merely offline, the same line is plain text
 * with no handler -- an armed control under ordinary status text gets pressed
 * by accident, and here that means a browser window and a re-auth nobody asked
 * for.
 */
function AccountDetail({ account, providers, busy, onCommand }) {
  const [renaming, setRenaming] = useState(false)
  const [draft, setDraft] = useState('')
  const [confirmingRemove, setConfirmingRemove] = useState(false)
  const input = useRef(null)

  useEffect(() => { setRenaming(false); setConfirmingRemove(false) }, [account?.id])
  useEffect(() => { if (renaming) input.current?.focus() }, [renaming])
  // A command in flight is exactly the moment a stale confirm prompt must not
  // survive: the click that follows it would resend a remove for whatever
  // account is selected by then, not the one the user meant.
  useEffect(() => { if (busy) setConfirmingRemove(false) }, [busy])

  if (!account) {
    return (
      <div className="glass-text px-1 py-2 text-[10px] leading-snug text-faint">
        No accounts in the vault yet. Use the <span className="text-ink-2">+</span> button
        above the roster to add one, then sign in — each account keeps its own
        credential, so adding a second does not evict the first.
      </div>
    )
  }

  const planned = isPlanned(account, providers)
  const fault = classify(account, planned)
  const rows = planned ? [] : usageRows(account)
  const name = providerName(providers, account.provider)
  const expiry = expiryLabel(account.expires_at)

  const commit = () => {
    const next = draft.trim()
    setRenaming(false)
    if (next && next !== account.label) {
      onCommand(account.id, 'ai_account_rename', () => bridge.rename(account.id, next))
    }
  }

  return (
    <div className="min-w-0">
      <div className="flex items-start gap-1">
        <ProviderIcon id={account.provider} size={13} muted={planned} className="mt-[2px]" />
        {renaming ? (
          <input
            ref={input}
            type="text"
            value={draft}
            spellCheck={false}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commit}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commit()
              if (e.key === 'Escape') setRenaming(false)
            }}
            className="glass-text min-w-0 flex-1 rounded-[6px] border border-edge-soft bg-white/[0.06]
                       px-1.5 py-[1px] text-[10.5px] text-ink outline-none focus:border-white/35"
          />
        ) : (
          <button
            type="button"
            disabled={busy}
            title={busy ? 'Waiting for the last command' : 'Click to rename this account'}
            onClick={() => { setDraft(account.label || ''); setRenaming(true) }}
            className={'glass-text min-w-0 flex-1 break-words text-left text-[10.5px] font-semibold '
              + 'leading-snug text-ink transition '
              + (busy ? 'cursor-default opacity-60' : 'hover:text-accent-2')}
          >
            {account.label || name}
          </button>
        )}
        {/* One indicator for the row, next to the controls it disables, so the
            reason a click does nothing is visible at the point of the click. */}
        {busy && <Busy className="mt-[2px]" />}
        <button
          type="button"
          disabled={busy}
          title={busy ? 'Waiting for the last command' : 'Poll this account now'}
          onClick={() => onCommand(account.id, 'ai_account_refresh', () => bridge.refresh(account.id))}
          className={'shrink-0 text-[10px] leading-none transition '
            + (busy ? 'cursor-default text-faint' : 'text-muted hover:text-accent')}
        >
          &#8635;
        </button>
        {/* This used to be a bare "×" glyph the same size and colour as the
            refresh "↻" beside it, one pixel apart, with no confirmation --
            indistinguishable at a glance and one click from permanently
            deleting the vault row. It now carries a word, so it reads
            differently from refresh without being read at all, and it never
            fires the delete on the first click: it only arms the confirm
            step below. */}
        <button
          type="button"
          disabled={busy}
          aria-haspopup="true"
          aria-expanded={confirmingRemove}
          title={busy ? 'Waiting for the last command' : 'Remove this account from the vault'}
          onClick={() => setConfirmingRemove(true)}
          className={'shrink-0 rounded-[4px] px-1 text-[9px] leading-none transition '
            + (busy ? 'cursor-default text-faint' : 'text-muted hover:bg-white/10 hover:text-bad')}
        >
          Remove
        </button>
      </div>

      {confirmingRemove && (
        /* The permanent, unrecoverable half of this action lives here, not on
           the button above: one click only ever arms this row, never deletes
           anything. `role="alertdialog"` and the autofocused "Remove" button
           make the choice reachable with a keyboard exactly the way the arm
           click was, and Escape backs out the same as clicking Cancel. */
        <div
          role="alertdialog"
          aria-label="Confirm account removal"
          className="glass-text mt-1 rounded-[6px] border border-edge-soft bg-white/[0.06] px-1.5 py-1"
        >
          {/* Precisely what the vault does, not a rounded-off version of it.
              Removal also writes a tombstone (aiaccounts.py:386) so the
              automatic import sweep cannot resurrect the row, and signing in
              again clears that tombstone (core.py:4038). "No undo" would be
              the wrong warning -- the credential really is gone, but the way
              back is a sign-in, and there is no button in this app that
              restores it without one. */}
          <div className="break-words text-[9.5px] leading-snug text-ink-2">
            Remove {account.label || name} from the vault? This deletes its stored
            credential, and nothing in this app puts it back: the only way to
            return this account is to sign in to it again.
          </div>
          <div className="mt-1 flex items-center gap-1">
            <button
              type="button"
              autoFocus
              onKeyDown={(e) => { if (e.key === 'Escape') setConfirmingRemove(false) }}
              onClick={() => {
                setConfirmingRemove(false)
                onCommand(account.id, 'ai_account_remove', () => bridge.remove(account.id))
              }}
              className={'rounded-[5px] bg-bad/20 px-1.5 py-[2px] text-[9.5px] leading-snug '
                + 'text-bad transition hover:bg-bad/30 ' + FOCUS_RING}
            >
              Remove permanently
            </button>
            <button
              type="button"
              onClick={() => setConfirmingRemove(false)}
              className={'rounded-[5px] px-1.5 py-[2px] text-[9.5px] leading-snug text-muted '
                + 'transition hover:bg-white/10 hover:text-ink ' + FOCUS_RING}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      <div className="glass-text break-words text-[9px] leading-snug text-faint">
        {name}{expiry ? ` · ${expiry}` : ''}
      </div>

      <div className="mt-1">
        {planned ? (
          /* No bar, no percentage, no empty row: there is no endpoint behind
             this provider yet, and any of those would be read as a reading. */
          <div className="glass-text break-words text-[10px] leading-snug text-muted">
            Not wired up yet — this provider is registered so accounts can be kept,
            but it has no usage endpoint to read.
          </div>
        ) : rows.length ? (
          rows.map((r) => (
            <QuotaRow key={r.key} label={r.label} pct={r.pct} reset={r.reset} dim={Boolean(account.held)} />
          ))
        ) : (
          <div className="glass-text break-words text-[10px] leading-snug text-faint">
            {fault ? 'No usage figures from the last poll.' : 'No usage figures reported yet.'}
          </div>
        )}
      </div>

      {fault && fault.kind !== 'planned' && (
        fault.kind === 'expired' ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => onCommand(account.id, 'ai_account_login', () => bridge.login(account.id))}
            title={busy
              ? 'Waiting for the last command'
              : 'Sign in again and store the new credential in the vault'}
            className={'glass-text mt-1 block w-full break-words text-left text-[10px] leading-snug '
              + 'underline decoration-dotted transition '
              + (busy ? 'cursor-default text-faint' : 'text-warn hover:text-accent-2')}
          >
            {fault.full}
          </button>
        ) : (
          /* Inert on purpose. Offline says nothing about the credential, and a
             provider-side 5xx is not something a re-login fixes. */
          <div className={'glass-text mt-1 break-words text-[10px] leading-snug '
            + (fault.kind === 'offline' ? 'text-muted' : 'text-bad')}>
            {fault.full}
          </div>
        )
      )}
    </div>
  )
}

/* ── the panel ─────────────────────────────────────────────────────────────── */

/**
 * The screen between picking a provider and actually starting its sign-in.
 *
 * The thing that decides which account gets connected is not this app and
 * not the CLI sandbox -- it is whichever account the system browser already
 * has a session for. A perfectly clean CLI still hands the OAuth flow to a
 * browser that is signed in as account #1, and the flow completes as #1
 * without ever asking, which is exactly the silent-reconnect bug this screen
 * exists to prevent. That fact used to live only in a tooltip on the picker
 * row, and a tooltip nobody reads before clicking is not a warning, it is
 * decoration. This is a click of its own, so the user has to see it -- and
 * it is asked every time, not once ever: it is cheap to click through if the
 * browser is already right (a sign-out or a private window), and skipping it
 * "for good" would put the very first user who forgets right back where this
 * bug started.
 */
function SignInWarning({ provider, onConfirm, onCancel }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onCancel() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])

  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-label={`Before signing in to ${provider.name}`}
      className="absolute inset-x-0 top-0 z-20 flex max-h-full flex-col overflow-hidden
                 rounded-[8px] border border-edge-soft bg-black/80 p-1.5"
    >
      <div className="glass-text break-words text-[10px] leading-snug text-ink-2">
        Your web browser decides which account you sign in as here, not this app.
      </div>
      <div className="glass-text mt-1 break-words text-[9.5px] leading-snug text-faint">
        If your browser is already signed in to {provider.name} as an account you
        do not want to add, this will connect that account again instead of asking.
        To add a different one, sign out on {provider.name}'s own site first, or
        open the sign-in in a private / incognito window.
      </div>
      <div className="mt-1.5 flex items-center gap-1">
        <button
          type="button"
          autoFocus
          onClick={onConfirm}
          className={'rounded-[5px] border border-edge-soft bg-white/[0.08] px-1.5 py-[2px] '
            + 'text-[9.5px] leading-snug text-ink transition hover:bg-white/20 ' + FOCUS_RING}
        >
          My browser is ready — continue
        </button>
        <button
          type="button"
          onClick={onCancel}
          className={'rounded-[5px] px-1.5 py-[2px] text-[9.5px] leading-snug text-muted '
            + 'transition hover:bg-white/10 hover:text-ink ' + FOCUS_RING}
        >
          Cancel
        </button>
      </div>
    </div>
  )
}

export function AiAccounts({ ai, providers: registry }) {
  const [query, setQuery] = useState('')
  const [picking, setPicking] = useState(false)
  // The provider a sign-in was requested for, held here only until the user
  // acknowledges the browser-session warning above or backs out of it.
  const [pendingProvider, setPendingProvider] = useState(null)
  // The id, never the position. The accounts array reorders as each account
  // finishes its own poll, so a remembered index would quietly start pointing
  // at a different account -- and the whole point of this pane is knowing which
  // of three same-provider accounts you are looking at.
  const [selectedId, setSelectedId] = useState(null)
  const [fetched, setFetched] = useState(null)

  // Ask once on mount. Both answers may also arrive on the ordinary data
  // stream; whichever lands first is used, and neither is required.
  useEffect(() => {
    settle(bridge.providers(), (v) => setFetched(Array.isArray(v) ? v : v.providers))
    bridge.accounts()
  }, [])

  const providers = providersOf(registry ?? fetched)
  // The registry has its own arrival; `providers` above is never empty because
  // it falls back, so emptiness cannot be the signal. This is.
  const registryArrived = Boolean((registry ?? fetched)?.length)
  const accounts = useMemo(() => accountsOf(ai), [ai])
  const { pending, notice, stage, begin } = useAiCommands(accounts)

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return accounts
    return accounts.filter((a) => (a.label ?? '').toLowerCase().includes(needle)
      || providerName(providers, a.provider).toLowerCase().includes(needle)
      || (a.provider ?? '').toLowerCase().includes(needle))
  }, [accounts, providers, query])

  // Resolved by identity every render, so a refresh that reorders or rewrites
  // the array cannot move the selection. Falling back to the first row keeps
  // the detail pane populated when the selected account is removed.
  const selected = accounts.find((a) => a.id === selectedId) ?? accounts[0] ?? null

  // Both hooks watch the same element, and both need to re-run when the set of
  // rows changes rather than only when the box does, so they are handed a
  // signature of what is currently listed.
  const listRef = useRef(null)
  const rosterSignature = `${shown.map((a) => a.id).join(',')}|${query}`
  const rosterHeight = useWholeRowHeight(listRef, VISIBLE_ACCOUNT_ROWS, rosterSignature)
  const rosterFade = useScrollFade(listRef, `${rosterSignature}|${rosterHeight ?? ''}`)

  return (
    // `relative` is what keeps the provider sheet inside the card: it is the
    // containing block the sheet is positioned and size-capped against, and
    // this element is itself wholly inside the card's frosted rectangle.
    <div className="relative px-3">
      <div className="flex items-start gap-2">
        {/* Left: the roster. A fixed column, because the detail pane's bars
            need a predictable width and a roster that grew with its longest
            label would take it from them. */}
        <div className="w-[118px] shrink-0">
          <div className="flex items-center gap-1">
            <SearchField value={query} onChange={setQuery} placeholder="Search…" />
            <button
              type="button"
              onClick={() => setPicking((v) => !v)}
              title="Add an account"
              aria-label="Add an account"
              aria-expanded={picking}
              className={'shrink-0 rounded-[6px] border border-edge-soft px-1.5 py-[1px] '
                + 'text-[10px] leading-[1.35] text-ink-2 transition '
                + 'hover:border-white/35 hover:bg-white/20 hover:text-ink ' + FOCUS_RING
                + (picking ? 'border-white/35 bg-white/20 text-ink' : 'bg-white/[0.08]')}
            >
              +
            </button>
          </div>

          {/* The height is whatever four rows actually measure, so the list
              always cuts between rows and never through one, and it follows a
              row that wraps or a shell that is zoomed instead of pretending
              every row is the same fixed number of pixels. */}
          <div
            ref={listRef}
            data-scroll-top={rosterFade.top}
            data-scroll-bottom={rosterFade.bottom}
            style={rosterHeight ? { maxHeight: `${rosterHeight}px` } : undefined}
            className="nw-fade-edges mt-1 overflow-y-auto"
          >
            {accounts.length === 0 && (
              <EmptyNote>No accounts yet — add one with +</EmptyNote>
            )}
            {accounts.length > 0 && shown.length === 0 && (
              <EmptyNote onClear={() => setQuery('')}>
                Nothing matches “{query}”
              </EmptyNote>
            )}
            {shown.map((a) => (
              <AccountRow
                key={a.id}
                account={a}
                providers={providers}
                selected={selected?.id === a.id}
                onSelect={setSelectedId}
                busy={Boolean(pending[a.id])}
              />
            ))}
          </div>
        </div>

        <span className="w-px shrink-0 self-stretch bg-white/15" />

        {/* Right: the selected account in full. */}
        <div className="min-w-0 flex-1">
          <AccountDetail
            account={selected}
            providers={providers}
            busy={Boolean(selected && pending[selected.id])}
            onCommand={begin}
          />
        </div>
      </div>

      {picking && (
        <ProviderPicker
          providers={providers}
          loading={!registryArrived}
          busy={Boolean(pending[ADD_KEY])}
          onPick={(p) => {
            setPicking(false)
            // Sign-in does not start here any more -- it starts once the
            // browser-session warning below is acknowledged.
            setPendingProvider(p)
          }}
          onClose={() => setPicking(false)}
        />
      )}

      {pendingProvider && (
        <SignInWarning
          provider={pendingProvider}
          onConfirm={() => {
            const p = pendingProvider
            setPendingProvider(null)
            // The label is the user's to change; a default that names the
            // provider is better than an empty row while the login runs.
            begin(ADD_KEY, 'ai_account_add', () => bridge.add(p.id, p.name))
          }}
          onCancel={() => setPendingProvider(null)}
        />
      )}

      {/* The add command has no row of its own to wear a pending mark, so it
          says so here, on the line the notice already occupies. */}
      {/* "Adding the account…" is true for the second before the sandbox is
          built and false for the several minutes after it, when what is
          actually happening is a person signing in at a browser window. The
          line follows the sidecar's own stage so it never contradicts the
          notice printed directly beneath it. */}
      {pending[ADD_KEY] && (
        <div className="mt-1 flex items-start gap-1">
          <Busy className="mt-[1px]" />
          <span className="glass-text min-w-0 flex-1 break-words text-[9.5px] leading-snug text-faint">
            {stage === 'console'
              ? 'Waiting for you to finish signing in — the sign-in window is open.'
              : 'Adding the account…'}
          </span>
        </div>
      )}

      {/* `break-words` and no width cap: this line carries the sidecar's own
          sentences, which include the ~175-character duplicate verdict and the
          longer wrong-account explanation, and both are only useful whole. It
          wraps to as many lines as it needs and is never shortened. */}
      {notice && (
        <div
          role="status"
          className={'glass-text mt-1 break-words text-[9.5px] leading-snug '
            + 'transition-opacity duration-700 '
            + (notice.bad ? 'text-bad ' : 'text-muted ')
            + (notice.fading ? 'opacity-0' : 'opacity-100')}
        >
          {notice.text}
        </div>
      )}
    </div>
  )
}
