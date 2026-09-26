'use strict'
class Cancellation {
  constructor() { this.cancelled = false; this.listeners = new Set() }
  onCancel(callback) {
    if (this.cancelled) callback()
    else this.listeners.add(callback)
    return () => this.listeners.delete(callback)
  }
  cancel() {
    if (this.cancelled) return
    this.cancelled = true
    for (const listener of this.listeners) listener()
    this.listeners.clear()
  }
}
module.exports = { Cancellation }
