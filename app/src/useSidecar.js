import { useEffect, useRef, useState } from 'react'

/**
 * Collects the sidecar's JSON-line stream into one state object.
 *
 * Each message kind owns its own slice, so a slow AI poll never blanks the
 * network panel and a failed check never clears the hardware readout. That is
 * the same guarantee the Tk build made by writing into separate labels; here it
 * has to be explicit, because a naive `setState(msg)` would replace everything.
 */
const EMPTY = {
  hello: null,
  ready: false,
  net: null,
  hw: null,
  ai: null,
  checks: null,
  tz: null,
  netstate: { up: true, busy: false },
  tehran: null,
  hist: [],
  errors: [],
}

export function useSidecar() {
  const [state, setState] = useState(EMPTY)
  // The authoritative value between commits. Messages arrive from six
  // independent Python threads and several can land in one frame, so folding
  // them into `state` directly would read a value one render out of date.
  const live = useRef(EMPTY)
  const frame = useRef(0)

  useEffect(() => {
    if (!window.nw) return undefined

    const commit = () => {
      frame.current = 0
      setState(live.current)
    }

    const off = window.nw.onData((msg) => {
      live.current = reduce(live.current, msg)
      // One rAF-aligned render per frame, not one per message.
      if (!frame.current) frame.current = requestAnimationFrame(commit)
    })

    return () => {
      off()
      if (frame.current) {
        cancelAnimationFrame(frame.current)
        frame.current = 0
      }
    }
  }, [])

  return state
}

function reduce(s, msg) {
  const { t, ...rest } = msg
  switch (t) {
    case 'hello':
      return { ...s, hello: rest }
    case 'ready':
      return { ...s, ready: true }
    case 'net':
      return { ...s, net: rest }
    case 'hw':
      return { ...s, hw: rest }
    case 'tz':
      return { ...s, tz: rest }
    case 'tehran':
      return { ...s, tehran: rest.time }
    case 'netstate':
      return { ...s, netstate: rest }
    case 'hist':
      return { ...s, hist: rest.items ?? [] }
    case 'ai': {
      // 'polling' and 'wait' are transient status, not a new reading: merging
      // rather than replacing is what stops the bars emptying every 15 minutes
      // for the second and a half the poll takes.
      //
      // A *failed* poll keeps the numbers it already had, flagged stale. The
      // usage endpoints rate limit for an hour at a time, and replacing a real
      // percentage with a dash claims no data exists when the truth is that it
      // could not be asked for again yet.
      const keep = (prev, next) => {
        if (!next) return prev
        if (!next.err) return next
        return { ...(prev ?? {}), ...next, held: Boolean(prev && !prev.err) }
      }
      return {
        ...s,
        ai: {
          ...(s.ai ?? {}),
          ...rest,
          // `status` is momentary -- polling, waiting -- and must not outlive
          // the message that reports the result. Merged like everything else it
          // sticks: the reading that follows a poll carries no status, so the
          // 'polling' from a second earlier stayed set and the refresh button
          // span for the rest of the session.
          status: rest.status ?? null,
          claude: keep(s.ai?.claude, rest.claude),
          codex: keep(s.ai?.codex, rest.codex),
        },
      }
    }
    case 'checks':
      if (rest.loading) return { ...s, checks: { ...(s.checks ?? {}), loading: true } }
      return { ...s, checks: { ...rest, loading: false } }
    case 'error':
      return { ...s, errors: [...s.errors.slice(-9), rest] }
    default:
      return s
  }
}

export const send = (cmd) => window.nw?.send(cmd)
