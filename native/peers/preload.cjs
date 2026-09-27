'use strict'
const { contextBridge, ipcRenderer } = require('electron')
contextBridge.exposeInMainWorld('afPeers', Object.freeze({
  request: value => ipcRenderer.invoke('af-peer-request', value)
}))
