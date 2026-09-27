'use strict'

// Native entrypoint. The renderer cannot choose this file or configuration path.
require('bare-buffer/global')
const fs = require('bare-fs')
const net = require('bare-net')
const DHT = require('hyperdht')
const { PeerWorker } = require('./worker-core.cjs')
const { createDHTTransport } = require('./dht-transport.cjs')
const { requestLocal } = require('./local-ipc.cjs')
const { KEY, object, encode, Decoder } = require('./protocol.cjs')

async function start() {
  const configPath = Bare.argv[2]
  const stat = fs.statSync(configPath)
  if (!stat.isFile() || stat.size > 65536 || (stat.mode & 0o077)) throw new Error('private_config_required')
  const config = JSON.parse(fs.readFileSync(configPath, 'utf8'))
  object(config, ['seed', 'enabled', 'paired', 'bootstrap', 'relay', 'ipc'])
  if (typeof config.seed !== 'string' || !KEY.test(config.seed)) throw new Error('identity_invalid')
  // Secret seed is supplied by the local host in its private file. It never
  // appears in IPC responses, renderer state, logs, or network request bodies.
  const keyPair = DHT.keyPair(Buffer.from(config.seed, 'hex'))
  const options = { enabled: config.enabled, paired: config.paired,
    publicKey: keyPair.publicKey.toString('hex') }
  if (config.relay !== null) options.relay = config.relay
  const worker = new PeerWorker(options, () => createDHTTransport(DHT,
    { paired: config.paired, bootstrap: config.bootstrap }, keyPair,
    (transport_key, envelope, signal) => requestLocal(net, config.ipc, 'peer.execute',
      { transport_key, envelope }, { signal })))
  await worker.start()
  const decoder = new Decoder(value => {
    worker.handle(value).then(response => Bare.IPC.write(encode(response))).catch(() => Bare.exit(1))
  })
  Bare.IPC.on('data', chunk => {
    try { decoder.push(chunk) } catch { Bare.exit(1) }
  })
  Bare.IPC.on('close', () => worker.handle({ version: '1.0.0', id: 'shutdown', method: 'stop', payload: {} }))
}

start().catch(() => Bare.exit(1))
