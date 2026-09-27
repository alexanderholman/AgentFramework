'use strict'

const path = require('node:path')
const { encode, Decoder, rendererRequest } = require('./protocol.cjs')

function createHostBridge(configPath, PearRuntime) {
  if (!path.isAbsolute(configPath)) throw new Error('absolute_private_config_required')
  // Fixed local worker source, no pear:// remote source/OTA or renderer path.
  const ipc = PearRuntime.run(path.join(__dirname, 'worker.cjs'), [configPath])
  const pending = new Map()
  let closed = false
  const close = () => {
    closed = true
    for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error('worker_closed')) }
    pending.clear()
    ipc.destroy()
  }
  const decoder = new Decoder(response => {
    if (!response || response.version !== '1.0.0' || typeof response.ok !== 'boolean') return close()
    const item = pending.get(response.id)
    if (!item) return close()
    clearTimeout(item.timer)
    pending.delete(response.id)
    item.resolve(response)
  })
  ipc.on('data', chunk => { try { decoder.push(chunk) } catch { close() } })
  ipc.on('error', close)
  ipc.on('close', () => { if (!closed) close() })
  return {
    request(value) {
      const request = rendererRequest(value)
      if (closed || pending.size >= 16 || pending.has(request.id)) throw new Error('worker_busy')
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { pending.delete(request.id); reject(new Error('worker_timeout')) }, 25000)
        pending.set(request.id, { resolve, reject, timer })
        ipc.write(encode(request))
      })
    },
    close
  }
}

// Call from a local Electron main process with contextIsolation=true,
// sandbox=true, nodeIntegration=false. Never expose the IPC stream itself.
function installElectronBridge(ipcMain, webContents, configPath, PearRuntime) {
  const bridge = createHostBridge(configPath, PearRuntime)
  ipcMain.handle('af-peer-request', (event, request) => {
    if (event.sender !== webContents || event.senderFrame !== webContents.mainFrame) {
      throw new Error('renderer_not_authorized')
    }
    return bridge.request(request)
  })
  webContents.once('destroyed', () => { ipcMain.removeHandler('af-peer-request'); bridge.close() })
  return bridge
}

module.exports = { createHostBridge, installElectronBridge }
