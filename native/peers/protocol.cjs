'use strict'

const MAX_FRAME = 1024 * 1024
const KEY = /^[0-9a-f]{64}$/

function object(value, fields) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).sort().join(',') !== fields.slice().sort().join(',')) {
    throw new Error('schema_invalid')
  }
}

function encode(value) {
  const body = Buffer.from(JSON.stringify(value), 'utf8')
  if (!body.length || body.length > MAX_FRAME) throw new Error('frame_limit')
  const frame = Buffer.alloc(4 + body.length)
  frame.writeUInt32BE(body.length, 0)
  body.copy(frame, 4)
  return frame
}

class Decoder {
  constructor(onMessage) {
    this.onMessage = onMessage
    this.buffer = Buffer.alloc(0)
  }

  push(chunk) {
    // A single burst is bounded too; no unbounded concatenation before checking.
    if (this.buffer.length + chunk.length > MAX_FRAME + 4) throw new Error('frame_limit')
    this.buffer = Buffer.concat([this.buffer, chunk])
    while (this.buffer.length >= 4) {
      const length = this.buffer.readUInt32BE(0)
      if (!length || length > MAX_FRAME) throw new Error('frame_limit')
      if (this.buffer.length < length + 4) return
      const value = JSON.parse(this.buffer.subarray(4, length + 4).toString('utf8'))
      this.buffer = this.buffer.subarray(length + 4)
      this.onMessage(value)
    }
  }
}

function rendererRequest(request) {
  object(request, ['version', 'id', 'method', 'payload'])
  if (request.version !== '1.0.0' || typeof request.id !== 'string' ||
      !/^[a-zA-Z0-9_-]{1,64}$/.test(request.id)) {
    throw new Error('schema_invalid')
  }
  if (request.method === 'status' || request.method === 'stop') {
    object(request.payload, [])
  } else if (request.method === 'peer.send') {
    object(request.payload, ['transport_key', 'envelope'])
    if (typeof request.payload.transport_key !== 'string' || !KEY.test(request.payload.transport_key)) {
      throw new Error('transport_key_invalid')
    }
    envelope(request.payload.envelope)
  } else throw new Error('method_denied')
  return request
}

function envelope(value) {
  object(value, ['domain', 'payload_json', 'schema_version', 'signatures'])
  if (value.schema_version !== '1.0.0' || value.domain !== 'af.peer.request.v1' ||
      typeof value.payload_json !== 'string' || value.payload_json.length > MAX_FRAME / 2 ||
      !Array.isArray(value.signatures) || value.signatures.length !== 1) {
    throw new Error('envelope_invalid')
  }
  object(value.signatures[0], ['key_id', 'signature'])
  if (typeof value.signatures[0].key_id !== 'string' || !/^sha256:[0-9a-f]{64}$/.test(value.signatures[0].key_id) ||
      typeof value.signatures[0].signature !== 'string' || value.signatures[0].signature.length > 128) {
    throw new Error('envelope_invalid')
  }
  // Python validates canonicalization, signatures, exact lease, expiry and replay.
  return value
}

module.exports = { MAX_FRAME, KEY, object, encode, Decoder, rendererRequest, envelope }
