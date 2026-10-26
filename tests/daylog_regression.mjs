// Regression for app/electron/daylog.js: the 05:30 cut, a missed cut, restarts.
// Run: node tests/daylog_regression.mjs
import { createRequire } from 'node:module'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const require = createRequire(import.meta.url)
const { createDayLog, boundaryBefore, parseRotate } = require('../app/electron/daylog.js')

let pass = 0, fail = 0
const ok = (cond, name) => { cond ? pass++ : fail++; console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}`) }
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'daylog-'))
const read = (f) => (fs.existsSync(f) ? fs.readFileSync(f, 'utf8') : '')
const at = (s) => new Date(s)   // local time, no offset

// boundaries
const r = { h: 5, m: 30 }
ok(boundaryBefore(at('2026-10-26T06:00:00'), r).getTime() === at('2026-10-26T05:30:00').getTime(), 'after 05:30 -> today 05:30')
ok(boundaryBefore(at('2026-10-26T03:00:00'), r).getTime() === at('2026-10-25T05:30:00').getTime(), 'before 05:30 -> yesterday 05:30')
ok(boundaryBefore(at('2026-10-26T05:30:00'), r).getTime() === at('2026-10-26T05:30:00').getTime(), 'exactly 05:30 -> today')
ok(JSON.stringify(parseRotate('nonsense')) === '{"h":5,"m":30}', 'bad spec falls back to 05:30')
ok(JSON.stringify(parseRotate('25:00')) === '{"h":5,"m":30}', 'out-of-range spec falls back')
ok(JSON.stringify(parseRotate('04:15')) === '{"h":4,"m":15}', 'custom spec parsed')

// one day, then the cut
{
  const dir = tmp(); let now = at('2026-10-26T09:00:00')
  const log = createDayLog({ dir, rotate: '05:30', now: () => now })
  log.write('first')
  ok(read(log.file).includes('day started 2026-10-26 05:30:00') && read(log.file).includes('first'), 'first write creates today file with header')
  now = at('2026-10-27T05:29:00'); log.write('late night')
  ok(read(log.file).includes('late night') && !fs.existsSync(log.prev), 'before the cut: same file')
  now = at('2026-10-27T05:31:00'); log.write('morning')
  ok(read(log.prev).includes('first') && read(log.prev).includes('late night'), 'after the cut: old day moved to yesterday')
  ok(read(log.file).includes('morning') && !read(log.file).includes('first'), 'after the cut: fresh file')
  // missed cut: asleep for three days
  now = at('2026-10-30T11:00:00'); log.write('woke up')
  ok(read(log.prev).includes('morning') && !read(log.prev).includes('first'), 'missed cuts: rotates once, keeps only the previous file')
  ok(read(log.file).includes('day started 2026-10-30 05:30:00'), 'missed cuts: new file belongs to today')
  // check() with nothing to write still cuts
  now = at('2026-10-31T06:00:00')
  ok(log.check() === true && read(log.prev).includes('woke up'), 'check() rotates on the clock alone')
  ok(log.check() === false, 'check() twice in a day rotates once')
}

// restart in the same day adopts; restart after the cut rotates
{
  const dir = tmp(); let now = at('2026-10-26T09:00:00')
  createDayLog({ dir, now: () => now }).write('run one')
  const again = createDayLog({ dir, now: () => now }); again.write('run two')
  ok(read(again.file).includes('run one') && read(again.file).includes('run two') && !fs.existsSync(again.prev), 'restart same day keeps the file')
  now = at('2026-10-28T08:00:00')
  const later = createDayLog({ dir, now: () => now }); later.write('run three')
  ok(read(later.prev).includes('run two') && read(later.file).includes('run three'), 'restart after the cut rotates first')
}

// a file with no header (foreign or corrupt) is treated as stale
{
  const dir = tmp(); fs.writeFileSync(path.join(dir, 'net-watch.log'), 'garbage\n')
  const log = createDayLog({ dir, now: () => at('2026-10-26T09:00:00') }); log.write('x')
  ok(read(log.prev) === 'garbage\n' && read(log.file).startsWith('# net-watch log'), 'headerless file rotated away')
}

// never throws
{
  const dir = tmp(); const blocker = path.join(dir, 'file'); fs.writeFileSync(blocker, '')
  let threw = false
  try { createDayLog({ dir: path.join(blocker, 'logs') }).write('x') } catch { threw = true }
  ok(!threw, 'unwritable dir does not throw')
}

console.log(`${pass} passed, ${fail} failed, ${pass + fail} total`)
process.exit(fail ? 1 : 0)
