'use strict'

const { KEY, encode, Decoder, envelope, object } = require('./protocol.cjs')
const { Cancellation } = require('./cancellation.cjs')

function createDHTTransport(DHT, config, keyPair, executeLocal) {
  object(config, ['bootstrap', 'paired'])
  if (!Array.isArray(config.bootstrap) || config.bootstrap.length > 16 ||
      config.bootstrap.some(item => typeof item !== 'string' || !/^[a-zA-Z0-9.-]+:[0-9]{1,5}$/.test(item)) ||
      !Array.isArray(config.paired) || config.paired.length > 256 ||
      config.paired.some(key => typeof key !== 'string' || !KEY.test(key))) throw new Error('transport_config_invalid')
  // An empty explicit array means an isolated DHT, never public default bootstrap.
  const dht = new DHT({ bootstrap: config.bootstrap, keyPair })
  const connectPeer = dht.connect.bind(dht)
  const approvedRelays = new Map()
  // HyperDHT's peer handshake can propose a relay key. Guard its internal
  // connect path too, so a remote proposal cannot acquire local relay consent.
  dht.connect = (key, options) => {
    const hex = typeof key === 'string' ? key : key.toString('hex')
    if (!approvedRelays.has(hex)) throw new Error('relay_not_authorized')
    return connectPeer(key, options)
  }
  const paired = new Set(config.paired)
  const sockets = new Set()
  const cancellations = new Map()
  let closed = false
  const server = dht.createServer({ firewall: remote => !paired.has(remote.toString('hex')) })
  server.on('connection', socket => {
    const peer = socket.remotePublicKey.toString('hex')
    if (closed || !paired.has(peer) || sockets.size >= 16) return socket.destroy()
    sockets.add(socket)
    const cancellation = new Cancellation()
    cancellations.set(socket, cancellation)
    let used = false
    const timer = setTimeout(() => socket.destroy(), 10000)
    socket.on('error', () => socket.destroy())
    socket.on('close', () => {
      cancellation.cancel()
      cancellations.delete(socket)
      clearTimeout(timer)
      sockets.delete(socket)
    })
    const decoder = new Decoder(value => {
      if (used) return socket.destroy()
      used = true
      try { envelope(value) } catch { return socket.destroy() }
      Promise.resolve().then(() => executeLocal(peer, value, cancellation)).then(result => {
        if (!socket.destroyed) socket.end(encode(result))
      }).catch(() => socket.destroy())
    })
    socket.on('data', chunk => { try { decoder.push(chunk) } catch { socket.destroy() } })
  })
  let readyTimer
  const ready = Promise.race([server.listen(keyPair), new Promise((resolve, reject) => {
    readyTimer = setTimeout(() => reject(new Error('listen_timeout')), 10000)
  })]).finally(() => clearTimeout(readyTimer))
  // Startup errors are observed by exchange/close; avoid unhandled rejection.
  ready.catch(() => {})
  return {
    ready,
    async exchange(publicKey, request, { relay }) {
      if (closed || !paired.has(publicKey) || (relay !== null && !KEY.test(relay))) {
        throw new Error('peer_not_authorized')
      }
      envelope(request)
      const requestFrame = encode(request)
      await ready
      if (closed) throw new Error('transport_closed')
      return new Promise((resolve, reject) => {
        const options = { keyPair, relayThrough: relay === null ? null : Buffer.from(relay, 'hex') }
        if (relay) approvedRelays.set(relay, (approvedRelays.get(relay) || 0) + 1)
        const releaseRelay = () => {
          if (!relay) return
          const count = approvedRelays.get(relay) - 1
          if (count) approvedRelays.set(relay, count)
          else approvedRelays.delete(relay)
        }
        let socket
        try { socket = connectPeer(Buffer.from(publicKey, 'hex'), options) }
        catch { releaseRelay(); reject(new Error('peer_transport_unavailable')); return }
        sockets.add(socket)
        let done = false
        const finish = (error, result) => {
          if (done) return
          done = true
          clearTimeout(timer)
          sockets.delete(socket)
          releaseRelay()
          socket.destroy()
          if (error) reject(new Error('peer_transport_unavailable'))
          else resolve(result)
        }
        const timer = setTimeout(() => finish(true), 10000)
        const decoder = new Decoder(result => finish(false, result))
        socket.once('open', () => {
          try {
            if (socket.remotePublicKey.toString('hex') !== publicKey) return finish(true)
            socket.write(requestFrame)
          } catch { finish(true) }
        })
        socket.on('data', chunk => { try { decoder.push(chunk) } catch { finish(true) } })
        socket.on('error', () => finish(true))
        socket.on('close', () => finish(true))
      })
    },
    async close() {
      closed = true
      for (const cancellation of cancellations.values()) cancellation.cancel()
      cancellations.clear()
      for (const socket of sockets) socket.destroy()
      sockets.clear()
      await dht.destroy({ force: true })
    }
  }
}

module.exports = { createDHTTransport }
