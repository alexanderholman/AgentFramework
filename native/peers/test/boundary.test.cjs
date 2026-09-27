'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const { createHostBridge, installElectronBridge } = require('../host.cjs')
const { requestLocal } = require('../local-ipc.cjs')
const { encode, rendererRequest, envelope } = require('../protocol.cjs')
const { Cancellation } = require('../cancellation.cjs')
const { PeerWorker } = require('../worker-core.cjs')

const validConfig = { host: '127.0.0.1', port: 12345, token: 'a'.repeat(64) }
const status = id => ({ version: '1.0.0', id, method: 'status', payload: {} })
class Pipe extends EventEmitter {
  write(data) { this.last = data }
  destroy() { this.destroyed = true }
}
function runtime(pipe) { return { run(entry, args) { assert.match(entry, /worker\.cjs$/); assert.equal(args.length, 1); return pipe } } }

test('Electron bridge accepts only its own main frame and removes handler on close', async () => {
  const pipe = new Pipe()
  const contents = new EventEmitter()
  contents.mainFrame = {}
  let handler, removed
  const ipcMain = { handle(name, callback) { handler = callback }, removeHandler(name) { removed = name } }
  installElectronBridge(ipcMain, contents, '/private/config.json', runtime(pipe))
  assert.throws(() => handler({ sender: {}, senderFrame: contents.mainFrame }, status('bad')))
  assert.throws(() => handler({ sender: contents, senderFrame: {} }, status('frame')))
  const pending = handler({ sender: contents, senderFrame: contents.mainFrame }, status('valid'))
  pipe.emit('data', encode({ version: '1.0.0', id: 'valid', ok: true, result: {} }))
  assert.equal((await pending).ok, true)
  contents.emit('destroyed')
  assert.equal(removed, 'af-peer-request')
  assert.equal(pipe.destroyed, true)
})

test('host bounds outstanding IDs and rejects invalid worker replies', async () => {
  assert.throws(() => createHostBridge('relative.json', {}))
  const pipe = new Pipe()
  const bridge = createHostBridge('/private/config.json', runtime(pipe))
  const pending = []
  for (let index = 0; index < 16; index++) pending.push(bridge.request(status(`id-${index}`)).catch(error => error.message))
  assert.throws(() => bridge.request(status('id-0')))
  assert.throws(() => bridge.request(status('overflow')))
  pipe.emit('data', encode({ version: '1.0.0', id: 'unknown', ok: true }))
  assert.deepEqual(new Set(await Promise.all(pending)), new Set(['worker_closed']))
  assert.throws(() => bridge.request(status('closed')))
  const malformed = new Pipe()
  const second = createHostBridge('/private/config.json', runtime(malformed))
  malformed.emit('data', encode({ version: '99', ok: true }))
  assert.throws(() => second.request(status('closed')))
})

test('local IPC accepts only matching bounded response and hides errors', async () => {
  for (const mode of ['bad-id', 'denied', 'malformed', 'error', 'close', 'success']) {
    const pipe = new Pipe()
    const result = requestLocal({ connect: () => pipe }, validConfig, 'status', {})
    pipe.emit('connect')
    const sent = JSON.parse(pipe.last.subarray(4).toString())
    if (mode === 'error') pipe.emit('error', new Error('private address'))
    else if (mode === 'close') pipe.emit('close')
    else if (mode === 'malformed') pipe.emit('data', Buffer.from([255, 255, 255, 255]))
    else pipe.emit('data', encode({ version: '1.0.0', id: mode === 'bad-id' ? 'different' : sent.id,
      ok: mode !== 'denied', result: { public: true }, reason: 'private information' }))
    if (mode === 'success') assert.deepEqual(await result, { public: true })
    else await assert.rejects(result, error => !error.message.includes('private'))
  }
  for (const config of [{ ...validConfig, host: 'remote.example' }, { ...validConfig, token: [] }]) {
    assert.throws(() => requestLocal({}, config, 'status', {}))
  }
  const signal = new Cancellation()
  signal.cancel()
  await assert.rejects(requestLocal({}, validConfig, 'status', {}, { signal }), /cancelled/)
  let invoked = false
  signal.onCancel(() => { invoked = true })
  signal.cancel()
  assert.equal(invoked, true)
})

test('IPC and host deadlines reject and clean up without credentials', async context => {
  context.mock.method(globalThis, 'setTimeout', callback => { queueMicrotask(callback); return 1 })
  const pipe = new Pipe()
  await assert.rejects(requestLocal({ connect: () => pipe }, validConfig, 'status', {}), /ipc_timeout/)
  assert.equal(pipe.destroyed, true)
  const host = new Pipe()
  const bridge = createHostBridge('/private/config.json', runtime(host))
  await assert.rejects(bridge.request(status('deadline')), /worker_timeout/)
  bridge.close()
})

test('wire types reject coercion and invalid signed request forms', () => {
  assert.throws(() => rendererRequest({ ...status('valid'), id: 1 }))
  assert.throws(() => rendererRequest({ version: '1.0.0', id: 'a', method: 'peer.send',
    payload: { transport_key: [], envelope: {} } }))
  const valid = { domain: 'af.peer.request.v1', payload_json: '{}', schema_version: '1.0.0',
    signatures: [{ key_id: 'sha256:' + 'd'.repeat(64), signature: 'signature' }] }
  assert.throws(() => envelope({ ...valid, schema_version: '2' }))
  assert.throws(() => envelope({ ...valid, signatures: [{ ...valid.signatures[0], key_id: [] }] }))
  assert.throws(() => new PeerWorker({ enabled: 'yes', publicKey: 'a'.repeat(64), paired: [] }, () => {}))
})
