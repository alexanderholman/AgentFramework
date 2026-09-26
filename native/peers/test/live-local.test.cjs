'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const { createDHTTransport } = require('../dht-transport.cjs')

let DHT, createTestnet
try { DHT = require('hyperdht'); createTestnet = require('hyperdht/testnet') } catch {}

test('real isolated HyperDHT authenticates two local Noise peers', {
  skip: !DHT && 'optional native dependencies unavailable', timeout: 25000
}, async context => {
  let network
  try { network = await createTestnet(3) } catch (error) {
    if (error.code === 'EPERM') {
      context.skip('environment denied native network-interface watcher; no native DHT qualification')
      return
    }
    throw error
  }
  const keyA = DHT.keyPair()
  const keyB = DHT.keyPair()
  const bootstrap = network.bootstrap.map(item => `${item.host}:${item.port}`)
  // Keep qualification traffic strictly bound to loopback.
  class LocalDHT extends DHT {
    constructor(options) { super({ ...options, host: '127.0.0.1', firewalled: false, port: 0 }) }
  }
  const envelope = { domain: 'af.peer.request.v1', payload_json: '{}', schema_version: '1.0.0',
    signatures: [{ key_id: 'sha256:' + 'd'.repeat(64), signature: 'fixture-only' }] }
  const seen = []
  const a = createDHTTransport(LocalDHT, { bootstrap, paired: [keyB.publicKey.toString('hex')] }, keyA,
    async () => { throw new Error('unexpected_inbound') })
  const b = createDHTTransport(LocalDHT, { bootstrap, paired: [keyA.publicKey.toString('hex')] }, keyB,
    async (peer, value) => { seen.push(peer); return { received: value.domain } })
  try {
    await Promise.all([a.ready, b.ready])
    const reply = await a.exchange(keyB.publicKey.toString('hex'), envelope, { relay: null })
    assert.deepEqual(reply, { received: 'af.peer.request.v1' })
    assert.deepEqual(seen, [keyA.publicKey.toString('hex')])
  } finally {
    await Promise.all([a.close(), b.close()])
    await network.destroy()
  }
})

let PearRuntime
try { PearRuntime = require('pear-runtime') } catch {}
test('real Bare worker starts offline behind fixed host IPC', {
  skip: !PearRuntime && 'optional Bare sidecar unavailable', timeout: 10000
}, async () => {
  const fs = require('node:fs')
  const os = require('node:os')
  const path = require('node:path')
  const { createHostBridge } = require('../host.cjs')
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'af-bare-test-'))
  const config = path.join(directory, 'worker.json')
  fs.writeFileSync(config, JSON.stringify({ seed: 'a'.repeat(64), enabled: false,
    paired: [], bootstrap: [], relay: null,
    ipc: { host: '127.0.0.1', port: 1, token: 'b'.repeat(64) } }), { mode: 0o600 })
  let bridge
  try {
    bridge = createHostBridge(config, PearRuntime)
    const result = await bridge.request({ version: '1.0.0', id: 'offline', method: 'status', payload: {} })
    assert.equal(result.ok, true)
    assert.equal(result.result.network_enabled, false)
    assert.match(result.result.public_key, /^[0-9a-f]{64}$/)
    assert.equal(JSON.stringify(result).includes('seed'), false)
  } finally {
    if (bridge) bridge.close()
    fs.rmSync(directory, { recursive: true, force: true })
  }
})
