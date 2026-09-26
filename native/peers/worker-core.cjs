'use strict'

const { KEY, object, rendererRequest } = require('./protocol.cjs')

class PeerWorker {
  constructor(config, createTransport) {
    const fields = ['enabled', 'publicKey', 'paired']
    if (config.relay !== undefined) fields.push('relay')
    object(config, fields)
    if (typeof config.enabled !== 'boolean' || typeof config.publicKey !== 'string' || !KEY.test(config.publicKey) ||
        !Array.isArray(config.paired) || config.paired.length > 256 ||
        config.paired.some(key => typeof key !== 'string' || !KEY.test(key))) {
      throw new Error('worker_config_invalid')
    }
    if (config.relay) {
      object(config.relay, ['allowed', 'publicKey'])
      if (config.relay.allowed !== true || typeof config.relay.publicKey !== 'string' || !KEY.test(config.relay.publicKey)) {
        throw new Error('relay_not_authorized')
      }
    }
    this.enabled = config.enabled
    this.publicKey = config.publicKey
    this.paired = new Set(config.paired)
    this.relay = config.relay ? config.relay.publicKey : null
    this.createTransport = createTransport
    this.transport = null
    this.busy = false
  }

  async start() {
    if (this.enabled && !this.transport) this.transport = this.createTransport()
    if (this.transport && this.transport.ready) await this.transport.ready
  }

  async handle(value) {
    let response = { version: '1.0.0', id: 'invalid', ok: false }
    try {
      const request = rendererRequest(value)
      response.id = request.id
      if (request.method === 'status') {
        return { ...response, ok: true, result: { network_enabled: this.enabled,
          public_key: this.publicKey, paired_peers: this.paired.size,
          relay_authorized: this.relay !== null, qualification: 'native-unqualified' } }
      }
      if (request.method === 'stop') {
        this.enabled = false
        if (this.transport) await this.transport.close()
        return { ...response, ok: true, result: { stopped: true } }
      }
      if (this.busy) return { ...response, reason: 'worker_busy' }
      if (!this.enabled || !this.paired.has(request.payload.transport_key)) {
        return { ...response, reason: 'peer_not_authorized' }
      }
      this.busy = true
      try {
        await this.start()
        let route = 'direct'
        let envelope
        try {
          envelope = await this.transport.exchange(request.payload.transport_key,
            request.payload.envelope, { relay: null })
        } catch {
          if (!this.enabled || !this.relay) throw new Error('direct_unavailable')
          route = 'relay'
          envelope = await this.transport.exchange(request.payload.transport_key,
            request.payload.envelope, { relay: this.relay })
        }
        if (!this.enabled) throw new Error('worker_stopped')
        return { ...response, ok: true, result: { route, envelope, verified: false } }
      } finally { this.busy = false }
    } catch { return { ...response, reason: 'request_rejected' } }
  }
}

module.exports = { PeerWorker }
