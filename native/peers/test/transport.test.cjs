'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const net = require('node:net')
const { createDHTTransport } = require('../dht-transport.cjs')
const { requestLocal } = require('../local-ipc.cjs')
const { Cancellation } = require('../cancellation.cjs')
const { encode } = require('../protocol.cjs')
const PEER = 'b'.repeat(64)
const RELAY = 'c'.repeat(64)
const request = { domain: 'af.peer.request.v1', payload_json: '{}', schema_version: '1.0.0',
  signatures: [{ key_id: 'sha256:' + 'd'.repeat(64), signature: 'signature' }] }

class Socket extends EventEmitter {
  constructor(key = PEER) { super(); this.remotePublicKey = Buffer.from(key, 'hex'); this.destroyed = false }
  write(value) { this.written = value }
  end(value) { this.write(value); this.destroy() }
  destroy() { if (!this.destroyed) { this.destroyed = true; this.emit('close') } }
}
class DHT {
  constructor(options) { this.options = options; DHT.latest = this; this.calls = [] }
  createServer(options) {
    this.firewall = options.firewall
    this.server = new EventEmitter()
    this.server.listen = async () => {}
    return this.server
  }
  connect(key, options) {
    this.calls.push({ key, options })
    this.socket = new Socket(key.toString('hex'))
    queueMicrotask(() => this.socket.emit('open'))
    return this.socket
  }
  async destroy() { this.closed = true }
}

test('idle configured listener refuses unpaired peers and cancels on disconnect', async () => {
  let cancellation
  let invoked = false
  const transport = createDHTTransport(DHT, { bootstrap: [], paired: [PEER] }, {},
    async (key, value, signal) => {
      invoked = true
      cancellation = signal
      assert.equal(key, PEER)
      assert.deepEqual(value, request)
      return new Promise(resolve => signal.onCancel(() => resolve({ cancelled: true })))
    })
  await transport.ready
  assert.deepEqual(DHT.latest.options.bootstrap, [])
  assert.equal(DHT.latest.firewall(Buffer.from(RELAY, 'hex')), true)
  assert.equal(DHT.latest.firewall(Buffer.from(PEER, 'hex')), false)
  const socket = new Socket()
  DHT.latest.server.emit('connection', socket)
  socket.emit('data', encode(request))
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(invoked, true)
  socket.destroy()
  assert.equal(cancellation.cancelled, true)
  await transport.close()
  assert.equal(DHT.latest.closed, true)
})

test('upstream relay proposals cannot create unapproved connections', async () => {
  const transport = createDHTTransport(DHT, { bootstrap: [], paired: [PEER] }, {}, async () => ({}))
  await transport.ready
  const dht = DHT.latest
  assert.throws(() => dht.connect(Buffer.from(RELAY, 'hex')))
  const direct = transport.exchange(PEER, request, { relay: null })
  await new Promise(resolve => setImmediate(resolve))
  assert.throws(() => dht.connect(Buffer.from(RELAY, 'hex')))
  dht.socket.emit('data', encode({ signed: true }))
  assert.deepEqual(await direct, { signed: true })
  const relayed = transport.exchange(PEER, request, { relay: RELAY })
  await new Promise(resolve => setImmediate(resolve))
  const peerSocket = dht.socket
  const relaySocket = dht.connect(Buffer.from(RELAY, 'hex'))
  relaySocket.destroy()
  peerSocket.emit('data', encode({ signed: true }))
  await relayed
  assert.throws(() => dht.connect(Buffer.from(RELAY, 'hex')))
  await transport.close()
})

test('native cancellation closes actual local IPC before timeout', async () => {
  let accepted
  const gotConnection = new Promise(resolve => { accepted = resolve })
  const server = net.createServer(socket => accepted(socket))
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const signal = new Cancellation()
  const pending = requestLocal(net, { host: '127.0.0.1', port: server.address().port,
    token: 'a'.repeat(64) }, 'status', {}, { signal })
  const socket = await gotConnection
  socket.resume()
  const ended = new Promise(resolve => socket.once('end', resolve))
  signal.cancel()
  await assert.rejects(pending, /ipc_cancelled/)
  await ended
  socket.destroy()
  await new Promise(resolve => server.close(resolve))
})

test('synchronous connection failure releases relay consent', async () => {
  class FailingDHT extends DHT {
    connect() { throw new Error('connect_failed') }
  }
  const transport = createDHTTransport(FailingDHT, { bootstrap: [], paired: [PEER] }, {}, async () => ({}))
  await transport.ready
  await assert.rejects(transport.exchange(PEER, request, { relay: RELAY }), /peer_transport_unavailable/)
  assert.throws(() => DHT.latest.connect(Buffer.from(RELAY, 'hex')), /relay_not_authorized/)
  await transport.close()
})

test('oversized multibyte request opens no connection and write failure releases consent', async () => {
  class WriteFailDHT extends DHT {
    connect(key, options) {
      const socket = super.connect(key, options)
      socket.write = () => { throw new Error('closing_stream') }
      return socket
    }
  }
  const transport = createDHTTransport(WriteFailDHT, { bootstrap: [], paired: [PEER] }, {}, async () => ({}))
  await transport.ready
  await assert.rejects(transport.exchange(PEER, { ...request, payload_json: '界'.repeat(500000) }, { relay: null }), /frame_limit/)
  assert.equal(DHT.latest.calls.length, 0)
  await assert.rejects(transport.exchange(PEER, request, { relay: RELAY }), /peer_transport_unavailable/)
  assert.throws(() => DHT.latest.connect(Buffer.from(RELAY, 'hex')), /relay_not_authorized/)
  await assert.rejects(transport.exchange(RELAY, request, { relay: null }), /peer_not_authorized/)
  await transport.close()
  await assert.rejects(transport.exchange(PEER, request, { relay: null }), /peer_not_authorized/)
})

test('malformed inbound frames and oversized results close sockets', async () => {
  const transport = createDHTTransport(DHT, { bootstrap: [], paired: [PEER] }, {},
    async () => ({ large: 'x'.repeat(2 * 1024 * 1024) }))
  await transport.ready
  for (const frame of [Buffer.from([255, 255, 255, 255]), encode({ invalid: true }), encode(request)]) {
    const socket = new Socket()
    DHT.latest.server.emit('connection', socket)
    socket.emit('data', frame)
    await new Promise(resolve => setImmediate(resolve))
    assert.equal(socket.destroyed, true)
  }
  const unknown = new Socket(RELAY)
  DHT.latest.server.emit('connection', unknown)
  assert.equal(unknown.destroyed, true)
  await transport.close()
})
