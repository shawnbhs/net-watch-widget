// A diagnostic log that holds one day and starts over every morning.
//
// The day does not run midnight to midnight. It starts at a fixed local time
// (05:30 by default, NET_WATCH_LOG_ROTATE=HH:MM to change it), because the
// question this log answers is "what happened to the widget since I last sat
// down", and a midnight cut would split a late evening in two.
//
// The cut is decided by the clock, not by a timer firing on time. A laptop
// that is asleep or off at 05:30 cannot rotate at 05:30, so every write, every
// minute tick, start-up, resume and unlock all ask the same question -- "was
// the current file started before the most recent boundary?" -- and rotate the
// first time the answer is yes. A missed boundary is therefore caught at the
// first moment the widget is running again, never skipped and never doubled.
//
// Rotation keeps exactly one previous day: today's file becomes
// net-watch.yesterday.log (replacing the one before), and a fresh file starts.
// That is enough to read back what went wrong overnight, and bounded by
// construction.
//
// Nothing in here may throw. A log that can take the app down is worse than no
// log at all.

const fs = require('node:fs')
const path = require('node:path')

const MAX_BYTES = 8 * 1024 * 1024   // per day; a runaway loop must not fill a disk
const HEADER = '# net-watch log, day started '

function parseRotate(spec) {
  const m = /^\s*(\d{1,2}):(\d{2})\s*$/.exec(spec || '')
  if (!m) return { h: 5, m: 30 }
  const h = Number(m[1]); const mm = Number(m[2])
  if (h > 23 || mm > 59) return { h: 5, m: 30 }
  return { h, m: mm }
}

/** The most recent boundary at or before `now`, as a Date in local time. */
function boundaryBefore(now, at) {
  const b = new Date(now.getFullYear(), now.getMonth(), now.getDate(), at.h, at.m, 0, 0)
  if (b.getTime() > now.getTime()) b.setDate(b.getDate() - 1)
  return b
}

function pad(n, w = 2) { return String(n).padStart(w, '0') }

function stamp(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
    + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** Read the day a file belongs to from its header line, or null. */
function dayOfFile(file) {
  try {
    const fd = fs.openSync(file, 'r')
    try {
      const buf = Buffer.alloc(120)
      const n = fs.readSync(fd, buf, 0, buf.length, 0)
      const first = buf.toString('utf8', 0, n).split('\n')[0]
      if (!first.startsWith(HEADER)) return null
      const t = Date.parse(first.slice(HEADER.length).trim().replace(' ', 'T'))
      return Number.isFinite(t) ? t : null
    } finally { fs.closeSync(fd) }
  } catch { return null }
}

function createDayLog({ dir, rotate = process.env.NET_WATCH_LOG_ROTATE, now = () => new Date() } = {}) {
  const at = parseRotate(rotate)
  const file = path.join(dir, 'net-watch.log')
  const prev = path.join(dir, 'net-watch.yesterday.log')
  let dayStart = null   // ms of the boundary the current file belongs to
  let size = 0
  let full = false

  function startFile(boundary) {
    fs.mkdirSync(dir, { recursive: true })
    const head = `${HEADER}${stamp(boundary)}\n`
    fs.writeFileSync(file, head)
    dayStart = boundary.getTime()
    size = Buffer.byteLength(head)
    full = false
  }

  /** Rotate if the current file predates the latest boundary. Returns true if it did. */
  function check() {
    try {
      const b = boundaryBefore(now(), at)
      if (dayStart === null) {
        // First call in this process: adopt the file on disk if it is today's.
        const onDisk = fs.existsSync(file) ? dayOfFile(file) : null
        if (onDisk !== null && onDisk >= b.getTime()) {
          dayStart = onDisk
          size = fs.statSync(file).size
          full = size >= MAX_BYTES
          return false
        }
        if (fs.existsSync(file)) fs.renameSync(file, prev)
        startFile(b)
        return true
      }
      if (dayStart < b.getTime()) {
        if (fs.existsSync(file)) fs.renameSync(file, prev)
        startFile(b)
        return true
      }
    } catch { /* see header: never throw */ }
    return false
  }

  function write(...parts) {
    try {
      check()
      if (full) return
      const text = parts.map((p) => (typeof p === 'string' ? p
        : p instanceof Error ? (p.stack || p.message) : safeJson(p))).join(' ')
      let line = `[${stamp(now())}] ${text}\n`
      if (size + Buffer.byteLength(line) > MAX_BYTES) {
        line = `[${stamp(now())}] log full for today -- further lines dropped until the next rotation\n`
        full = true
      }
      fs.appendFileSync(file, line)
      size += Buffer.byteLength(line)
    } catch { /* never throw */ }
  }

  return { write, check, file, prev, rotateAt: `${pad(at.h)}:${pad(at.m)}` }
}

function safeJson(v) {
  try { return JSON.stringify(v) } catch { return String(v) }
}

module.exports = { createDayLog, boundaryBefore, parseRotate }
