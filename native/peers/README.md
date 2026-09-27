# AF native peer boundary

This optional package separates a plain HTML renderer, a narrow host bridge, a
native Bare worker, and AF's Python peer execution service. The worker owns its
Noise seed and network sockets. Python owns signing keys, procedure registration,
exact one-use grants, resource leases, verification, and the durable replay ledger.
Neither peer messages nor renderer messages can install or run source or commands.

## Run and embed

From this directory, `npm ci --ignore-scripts` installs the exact integrity-locked
optional packages. `npm test` runs protocol tests plus available native probes.
The repository pytest gate also invokes these tests when Node is available.
`npm start -- /absolute/path/to/private-worker.json` launches the optional local
Electron shell when its platform binary is installed. `--ignore-scripts` does not
download that binary; Electron desktop qualification remains separate.

Create `PeerIPCServer(service, bindings)` around the already configured
`PeerExecutionService`; its `address` and `token` belong only in a private native
configuration file. Bindings map each confirmed 64-character Noise public key to
one `sha256:...` Ed25519 signer fingerprint. `create_transport_invite` signs that
binding; `accept_transport_invite` checks the exact, previously confirmed signer,
signature and expiry before returning a binding for operator configuration.
Existing pairing helpers establish Ed25519 trust after out-of-band fingerprint
confirmation. No remote endpoint grants this trust or issues execution leases.

The worker takes one absolute configuration path from its host. Create it with
owner-only permissions (`0600`, parent directory `0700` on POSIX), with this shape:

```json
{
  "seed": "64 lowercase hex characters generated locally and kept private",
  "enabled": false,
  "paired": [],
  "bootstrap": [],
  "relay": null,
  "ipc": {"host": "127.0.0.1", "port": 12345, "token": "64 lowercase hex characters from PeerIPCServer"}
}
```

The example strings are placeholders and deliberately fail validation. Preserve
the seed securely across restarts. `enabled: false` constructs no DHT instance.
An explicit empty bootstrap list creates an isolated network instead of using
public defaults. A relay is enabled only by local configuration of
`{"allowed":true,"publicKey":"<confirmed 64-character relay key>"}`. The worker
first tries direct transport and only then attempts this exact relay. Its internal
HyperDHT connection guard also rejects remote-proposed relay keys without an
active local relay choice. This guard is bound to the pinned dependency and needs
real-network requalification before upgrades.

For an Electron host, configure `contextIsolation: true`, `sandbox: true`, and
`nodeIntegration: false`, use the fixed `preload.cjs`, and load
`renderer/index.html`. Call
`installElectronBridge(ipcMain, window.webContents, configPath, PearRuntime)` from
`host.cjs`. It accepts only requests from that window's main frame. The renderer
can read public status, stop connections, and send an already signed request to
an already paired peer. It cannot set paths, read identity keys, change bootstrap
or relay policy, pair unknown keys, or issue a lease. `createHostBridge` is also
usable by a different local shell. No OTA mechanism is enabled.

## Protocol and bounds

IPC and peer streams use a four-byte big-endian length plus UTF-8 JSON, bounded to
1 MiB before allocation. IPC requests have exactly `version`, `id`, `token`,
`method`, and `payload`; version is `1.0.0`. IPC allows only public `status` and
`peer.execute` with a transport key and existing signed request. Renderer IPC has
no token field and a separate closed method set. Python rejects duplicate fields,
unknown methods, malformed frames, incorrect credentials and signer bindings.

Native reads and requests are bounded; Python bounds each whole frame deadline,
not just gaps between individual bytes. Python accepts at most 16 concurrent
connections, the host at most 16 pending requests, and the worker one outbound
request. Disconnect, stop, or timeout closes the local execution socket, allowing
the peer service's existing cancellation check to fence acceptance and clean up.
No raw exception, seed, token, or private path is returned to the renderer.

HyperDHT uses authenticated Noise streams. Network discovery and hole punching
can expose IP addresses and timing, and punching is not guaranteed. Relay consent
does not imply successful delivery. The web view labels received signed envelopes
as **unverified**; independent Python signature/result verification is still
required before any completed-work claim. Native request delivery is not execution
authority.

## Qualification evidence and limits

Observed on Linux in this workspace on 2026-09-26:

| Boundary | Evidence |
| --- | --- |
| Python execution IPC | Real sockets, actual spawned installed character-count procedure, signed output `3`, rejection and bounds tests |
| Node to Python | Real framed TCP IPC, public-only status, cancellation socket closure |
| Bare worker | Real `PearRuntime.run` sidecar process, offline start and status, fixed worker path, no secrets in output |
| Relay policy | Protocol doubles cover direct failure, exact explicit fallback, remote relay rejection and synchronous-failure cleanup |
| HyperDHT live connections | Attempted isolated loopback test; native network-interface watcher denied with `EPERM`; explicitly skipped |
| Dependencies | Exact top-level pins, full integrity lock; `npm audit --omit=dev` reported zero vulnerabilities |

The Electron window, native DHT direct/relay exchange, actual NAT punching,
Windows/macOS hosting, and Android/iOS foreground shells remain unqualified here.
Mobile can reuse the bounded worker protocol, but no always-on mobile hosting is
claimed. This package is not a release certification or a public Pear deployment.

Primary API/version references consulted:

- [Pear worker boundary](https://docs.pears.com/pear/explanation/workers/)
- [Pear desktop architecture](https://docs.pears.com/pear/explanation/pear-desktop-architecture/)
- [Pear runtime API and package](https://github.com/holepunchto/pear-runtime)
- [HyperDHT API](https://github.com/holepunchto/hyperdht)
- [HyperDHT relay connection implementation](https://github.com/holepunchto/hyperdht/blob/main/lib/connect.js)
- [Bare filesystem](https://github.com/holepunchto/bare-fs)
- [Bare networking](https://github.com/holepunchto/bare-net)
- [Bare buffers](https://github.com/holepunchto/bare-buffer)
- [Electron security guidance](https://www.electronjs.org/docs/latest/tutorial/security)

Upstream APIs are subject to change. The shipped lock file, not those moving
branches, determines installed dependencies.
