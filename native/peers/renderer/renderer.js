'use strict'
let nextId = 0
async function request(method, payload, target) {
  try {
    if (!window.afPeers) throw new Error('Native peer worker unavailable')
    const response = await window.afPeers.request({ version: '1.0.0', id: `view-${++nextId}`, method, payload })
    target.textContent = JSON.stringify(response, null, 2)
  } catch { target.textContent = 'The native worker is unavailable or rejected this request.' }
}
const status = document.getElementById('status')
document.getElementById('refresh').addEventListener('click', () => request('status', {}, status))
document.getElementById('stop').addEventListener('click', () => request('stop', {}, status))
document.getElementById('send').addEventListener('submit', event => {
  event.preventDefault()
  const target = document.getElementById('result')
  try {
    const envelope = JSON.parse(document.getElementById('envelope').value)
    request('peer.send', { transport_key: document.getElementById('peer').value, envelope }, target)
  } catch { target.textContent = 'Enter a valid signed JSON request.' }
})
