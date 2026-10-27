// Regression for app/src/passThrough.js: which pixels of the widget window
// let clicks through, in all three layouts (tab, compact, full).
// Run: node tests/pass_through_regression.mjs
import { overNothing } from '../app/src/passThrough.js'

// A minimal element: enough of closest() / querySelector('[data-card]').
function el(name, attrs = {}, kids = []) {
  const e = { name, attrs, kids, parent: null }
  for (const k of kids) k.parent = e
  e.closest = (sel) => {
    const a = sel.slice(1, -1)
    for (let n = e; n; n = n.parent) if (a in n.attrs) return n
    return null
  }
  e.querySelector = (sel) => {
    const a = sel.slice(1, -1)
    const walk = (n) => n.kids.some((k) => a in k.attrs || walk(k))
    return walk(e) ? {} : null
  }
  return e
}
const card = (id, ...kids) => el('card:' + id, { 'data-card': id }, kids)

let pass = 0, fail = 0
const is = (got, want, name) => { got === want ? pass++ : fail++; console.log(`${got === want ? 'PASS' : 'FAIL'}  ${name}`) }

// ── full / compact: shell > column > (title, ModeBox > Pair > cards, footer)
const ipBtn = el('button')
const ring = el('ring')
const grip = el('grip')
const ip = card('ip', ipBtn)
const pair = el('pair', {}, [ip, card('rf')])
const modeBox = el('modebox', {}, [pair, card('ai', ring)])
const foot = card('foot', grip)
const column = el('column', {}, [card('title'), modeBox, foot])
const shell = el('shell', {}, [column])
const root = el('root', {}, [shell])
const pet = el('pet')
const petLayer = el('petlayer', {}, [pet])
const body = el('body', {}, [root, petLayer])
const html = el('html', {}, [body])
const doc = { documentElement: html, body }

is(overNothing(html, doc), true, 'page background passes through')
is(overNothing(body, doc), true, 'body passes through')
is(overNothing(root, doc), true, '#root passes through')
is(overNothing(shell, doc), true, 'gutter beside the cards (the black-line band) passes through')
is(overNothing(column, doc), true, 'gap between title / body / footer passes through')
is(overNothing(modeBox, doc), true, 'gap between cards passes through')
is(overNothing(pair, doc), true, 'gap inside a two-card row passes through')
is(overNothing(ip, doc), false, 'a card keeps the mouse')
is(overNothing(ipBtn, doc), false, 'a control inside a card keeps the mouse')
is(overNothing(ring, doc), false, 'usage meter keeps the mouse (tooltip)')
is(overNothing(grip, doc), false, 'resize grip keeps the mouse')
is(overNothing(pet, doc), false, 'a pet keeps the mouse')
is(overNothing(null, doc), true, 'nothing under the pointer passes through')

// ── tab (mini bar): shell > tab card > rings + picker
const picker = el('picker', { 'data-tab-ui': '' })
const tabRing = el('ring')
const tab = card('tab', tabRing, picker)
const tShell = el('shell', {}, [tab])
const tRoot = el('root', {}, [tShell])
const tBody = el('body', {}, [tRoot]); const tHtml = el('html', {}, [tBody])
const tDoc = { documentElement: tHtml, body: tBody }
is(overNothing(tShell, tDoc), true, 'tab: space around the tab passes through')
is(overNothing(tab, tDoc), false, 'tab: the tab keeps the mouse (click, drag)')
is(overNothing(tabRing, tDoc), false, 'tab: rings keep the mouse (tooltip)')
is(overNothing(picker, tDoc), false, 'tab: AI picker keeps the mouse')

console.log(`${pass} passed, ${fail} failed, ${pass + fail} total`)
process.exit(fail ? 1 : 0)
