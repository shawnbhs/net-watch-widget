// Launches Electron with a clean environment.
//
// Some terminals (and any process started by an Electron-based tool) export
// ELECTRON_RUN_AS_NODE=1. Inherited, it makes electron.exe behave as a plain
// Node binary: `require('electron')` returns a path string instead of the API,
// and the app dies on `app is undefined` with a stack that points nowhere near
// the cause. Stripping it here costs nothing and removes a genuinely baffling
// failure mode.
const { spawn } = require('node:child_process')
const electron = require('electron')

const env = { ...process.env }
delete env.ELECTRON_RUN_AS_NODE

const child = spawn(electron, ['.', ...process.argv.slice(2)], {
  stdio: 'inherit',
  env,
})
child.on('close', (code) => process.exit(code ?? 0))
