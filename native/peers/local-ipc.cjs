'use strict'

const { KEY, object, encode, Decoder } = require('./protocol.cjs')
let sequence = 0

function requestLocal(net, config, method, payload, { signal = null } = {}) {
  object(config, ['host', 'port', 'token'])
  if (config.host !== '127.0.0.1' || !Number.isInteger(config.port) ||
      config.port < 1 || config.port > 65535 || typeof config.token !== 'string' || !KEY.test(config.token) ||
      !['status', 'peer.execute'].includes(method)) throw new Error('ipc_config_invalid')
  const id = `native-${++sequence}`
  const frame = encode({ version: '1.0.0', id, token: config.token, method, payload })
  return new Promise((resolve, reject) => {
    if (signal && signal.cancelled) return reject(new Error('ipc_cancelled'))
    const connection = net.connect({ host: config.host, port: config.port })
    let done = false
    let unsubscribe = () => {}
    const finish = (error, result) => {
      if (done) return
      done = true
      clearTimeout(timer)
      unsubscribe()
      connection.destroy()
      if (error) reject(new Error(error))
      else resolve(result)
    }
    const timer = setTimeout(() => finish('ipc_timeout'), 10000)
    if (signal) unsubscribe = signal.onCancel(() => finish('ipc_cancelled'))
    const decoder = new Decoder(value => {
      if (value.version !== '1.0.0' || value.id !== id || typeof value.ok !== 'boolean') {
        finish('ipc_response_invalid')
      } else if (!value.ok) finish('ipc_request_denied')
      else finish(null, value.result)
    })
    connection.once('connect', () => connection.write(frame))
    connection.on('data', chunk => {
      try { decoder.push(chunk) } catch { finish('ipc_response_invalid') }
    })
    connection.on('error', () => finish('ipc_unavailable'))
    connection.on('close', () => finish('ipc_disconnected'))
  })
}

module.exports = { requestLocal }
