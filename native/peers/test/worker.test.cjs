'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const { encode, Decoder, rendererRequest } = require('../protocol.cjs')
const { PeerWorker } = require('../worker-core.cjs')

const PEER = 'b'.repeat(64)
const RELAY = 'c'.repeat(64)
const envelope = { domain: 'af.peer.request.v1', payload_json: '{}', schema_version: '1.0.0',
  signatures: [{ key_id: 'sha256:' + 'd'.repeat(64), signature: 'signature' }] }
const request = (method, payload = {}) => ({ version: '1.0.0', id: 'one', method, payload })

test('framing handles fragments and rejects oversized allocation', () => {
  const received = []
  const decoder = new Decoder(value => received.push(value))
  const frame = encode({ hello: '世界' })
  for (const byte of frame) decoder.push(Buffer.from([byte]))
  assert.deepEqual(received, [{ hello: '世界' }])
  assert.throws(() => decoder.push(Buffer.from([255, 255, 255, 255])))
  assert.throws(() => encode({ huge: 'x'.repeat(2 * 1024 * 1024) }))
})

test('renderer cannot issue grants, supply config, or select executable paths', () => {
  for (const method of ['lease.issue', 'shell', 'configure', 'identity.export']) {
    assert.throws(() => rendererRequest(request(method)))
  }
  assert.throws(() => rendererRequest({ ...request('status'), config: {} }))
  assert.throws(() => rendererRequest(request('peer.send', { transport_key: PEER,
    envelope: { ...envelope, source: 'run()' } })))
})

function fakeTransport(failDirect = false) {
  const calls = []
  return { calls, async exchange(key, value, options) {
    calls.push({ key, value, options })
    if (failDirect && !options.relay) throw new Error('private socket details')
    return { signed: true }
  }, async close() { calls.push({ closed: true }) } }
}

test('offline worker never constructs transport and exports no native credentials', async () => {
  let created = 0
  const worker = new PeerWorker({ enabled: false, publicKey: PEER, paired: [PEER] },
    () => { created++; return fakeTransport() })
  const status = await worker.handle(request('status'))
  assert.equal(status.result.network_enabled, false)
  assert.equal(created, 0)
  assert.equal((await worker.handle(request('peer.send', { transport_key: PEER, envelope }))).ok, false)
  assert.equal(created, 0)
})

test('direct failure has no silent relay and configured fallback is explicit', async () => {
  const transport = fakeTransport(true)
  const direct = new PeerWorker({ enabled: true, publicKey: PEER, paired: [PEER] }, () => transport)
  const denied = await direct.handle(request('peer.send', { transport_key: PEER, envelope }))
  assert.equal(denied.ok, false)
  assert.equal(transport.calls.length, 1)
  assert.equal(JSON.stringify(denied).includes('private'), false)
  const relayed = new PeerWorker({ enabled: true, publicKey: PEER, paired: [PEER],
    relay: { allowed: true, publicKey: RELAY } }, () => transport)
  const result = await relayed.handle(request('peer.send', { transport_key: PEER, envelope }))
  assert.equal(result.result.route, 'relay')
  assert.equal(result.result.verified, false)
  assert.equal(transport.calls.at(-1).options.relay, RELAY)
  await relayed.handle(request('stop'))
  assert.deepEqual(transport.calls.at(-1), { closed: true })
})

test('unpaired peer and unauthorized relay are refused before transport', async () => {
  assert.throws(() => new PeerWorker({ enabled: true, publicKey: PEER, paired: [],
    relay: { allowed: false, publicKey: RELAY } }, () => fakeTransport()))
  let created = 0
  const worker = new PeerWorker({ enabled: true, publicKey: PEER, paired: [] },
    () => { created++; return fakeTransport() })
  assert.equal((await worker.handle(request('peer.send', { transport_key: PEER, envelope }))).ok, false)
  assert.equal(created, 0)
})

test('worker serializes requests and rejects excess pending work', async () => {
  const gate = new EventEmitter()
  const worker = new PeerWorker({ enabled: true, publicKey: PEER, paired: [PEER] },
    () => ({ exchange: () => new Promise(resolve => gate.once('done', resolve)), close: async () => {} }))
  const pending = worker.handle(request('peer.send', { transport_key: PEER, envelope }))
  const rejected = await worker.handle(request('peer.send', { transport_key: PEER, envelope }))
  assert.equal(rejected.reason, 'worker_busy')
  gate.emit('done', { result: 'signed' })
  assert.equal((await pending).ok, true)
})
