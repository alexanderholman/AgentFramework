'use strict'

// Optional local desktop shell. It is deliberately separate from the Python
// installation and makes no OTA, public swarm or mobile-hosting claim.
const path = require('node:path')
const { app, BrowserWindow, ipcMain, session } = require('electron')
const PearRuntime = require('pear-runtime')
const { installElectronBridge } = require('./host.cjs')

const configPath = process.argv[2]
if (typeof configPath !== 'string' || !path.isAbsolute(configPath)) {
  app.exit(2)
} else {
  let bridge
  app.whenReady().then(async () => {
    const root = path.join(__dirname, 'renderer')
    // Every renderer network request is denied; peer egress belongs to Bare.
    session.defaultSession.webRequest.onBeforeRequest((details, callback) => {
      const allowed = details.url.startsWith('file:') &&
        ['index.html', 'renderer.js', 'style.css'].some(name =>
          details.url === require('node:url').pathToFileURL(path.join(root, name)).href)
      callback({ cancel: !allowed })
    })
    session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
    const window = new BrowserWindow({ width: 880, height: 760,
      webPreferences: { preload: path.join(__dirname, 'preload.cjs'),
        contextIsolation: true, sandbox: true, nodeIntegration: false } })
    window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    window.webContents.on('will-navigate', event => event.preventDefault())
    bridge = installElectronBridge(ipcMain, window.webContents, configPath, PearRuntime)
    await window.loadFile(path.join(root, 'index.html'))
  }).catch(() => app.exit(1))
  app.on('before-quit', () => { if (bridge) bridge.close() })
  app.on('window-all-closed', () => app.quit())
}
