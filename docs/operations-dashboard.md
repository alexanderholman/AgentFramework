# Operations dashboard

AF exposes one bounded, read-only operations projection through its shared application
boundary. The same projection powers server-rendered HTML, a versioned REST endpoint, and
terminal output; presentation adapters do not query persistence or derive health independently.

Start the development server:

```bash
uv run af serve
```

Then use:

- HTML dashboard: `http://127.0.0.1:8000/operations`
- REST snapshot: `GET http://127.0.0.1:8000/api/v1/operations`
- terminal view: `uv run af operations`
- machine-readable terminal view: `uv run af operations --json`

The snapshot contains application lifecycle state and a bounded window of operational Task
metadata. It deliberately excludes prompts, outputs, trace identifiers, configuration values,
credentials, and raw telemetry payloads. The activity count describes only the displayed
window; it is not an unbounded historical total.

Repositories may provide the optional bounded recent-Task reader. Applications injecting an
older Task repository remain compatible: the dashboard marks Task activity as unavailable rather
than loading unbounded history or failing the request.

Readiness is conservative. The initial projection reports component readiness as `unknown`
until fresh component evidence is connected to the shared health model. Unknown health is
never converted to healthy merely because no failure has been observed.

## Security boundary

The HTTP dashboard is a loopback development/operator surface. AF does not yet provide the
non-bypassable HTTP authentication and authorization boundary required for production use.
Trusted-host checks, CSRF protection, and signed flash sessions are not authentication. Do not
bind this dashboard to an untrusted network or place sensitive fields into dashboard DTOs.

The terminal view is intentionally a stable, headless-friendly first view. A stateful terminal
refresh loop must wait for the TUI kernel's cancellation, restoration, focus, resize, and
accessibility contracts; this slice does not claim those contracts are complete.

