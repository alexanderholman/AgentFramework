# Stateful terminal applications

`af tui list` discovers installed applications. `af tui run tasks` opens the
reference Task application in a POSIX terminal. Tab/Shift-Tab or up/down move
focus, text edits the focused prompt, Enter activates a control, mouse clicks
move focus, and Escape backs out or quits. The view scrolls with focus after a
resize. Create, read, update and confirmed deletion call the same TaskController
as the other transports; deleting a task first opens a separate confirmation.
The session keeps at most 50 recent task identities in memory.

Non-TTY invocation fails without prompting. `af tui run tasks --plain` explicitly
prints one accessible text snapshot, with no alternate screen, input read or ANSI
sequences. This fallback is also the current Windows profile; interactive native
Windows console support is not claimed. Ordinary task labels can use independent
`--language fr --territory CA` choices; critical confirmation wording remains
canonical source text. The renderer strips control characters, clips display
cells, labels focus textually, and does not rely on color or animation. Terminal
screen-reader compatibility requires separate user testing; the plain profile
avoids repeated screen clearing.

`af tui run tasks --script events.json` runs explicit bounded headless events and
returns a JSON run result and final view. It is intended for local test automation;
it can perform the selected application's normal actions. A script has exactly
an `events` array with at most 10,000 events. Supported shapes are:

```json
{"events":[
  {"type":"text","text":"math add a=1 b=2"},
  {"type":"key","key":"enter"},
  {"type":"mouse","widget_id":"quit"},
  {"type":"key","key":"enter"}
]}
```

Resize events contain `type`, `columns` and `rows`. Keys are an allowlist, and
scripts cannot name handlers, import code or invoke a shell. Shared bounded JSON
parsing rejects duplicate keys, excessive depth and nonfinite numbers. A script
that ends before the app finishes produces an explicit cancellation, not a
completed task claim. A plain snapshot is not task execution.

Providers register `TuiContribution(TuiApplicationDescriptor(...), factory)` via
`bind_tui_application(registrar, contribution)` from `af.framework.tui`. The factory
returns a fresh application exposing `start(turn)`, `handle(event, turn)` and
`close()`. Views contain immutable typed widgets, title, status and completion
state. Widgets have stable identifiers, semantic kinds, labels, values and enabled
state. `create_tui_kernel(application)` discovers contributions without a central
application switch. The generated TUI starter owns its application code.

Every initial view and interaction runs in a fresh container invocation scope with
an inherited principal/tenant and new correlation identity. Invocation resources
are disposed before rendering. Context cancellation, deadlines and turn budgets
stop further turns. Plugins are trusted installed host code, and must cooperate
with cancellation during their own synchronous work. They must not retain services
resolved from a previous turn. Output is a view, never an authorization grant.

The POSIX adapter owns cbreak flags, alternate-screen/cursor/mouse modes, and main
thread SIGINT/SIGTERM handlers for its session. It restores the original flags and
handlers and leaves caller streams open on success, signal, cancellation or
exception. Run results use 0 for completion or an explicit plain snapshot, 2 for
invalid configuration/input/budget, 124 for a deadline, 130 for cancellation and
128 plus the signal number for intercepted signals. Unexpected exceptions produce
stable sanitized reasons. A nonblocking advisory lease on the actual POSIX terminal
device prevents overlapping AF sessions from changing each other's terminal state.
The lease has its own descriptor and is released even when terminal output fails;
unrelated programs must also cooperate with the advisory lock.

Evidence: `tests/unit/test_tui.py`, `tests/feature/test_tui_journey.py`, and
`tests/integration/test_tui_terminal.py` cover per-turn isolation, task CRUD,
headless/native input, focus/resize, non-TTY behavior, ANSI injection rejection,
deadlines, cancellation, real POSIX PTY restoration, overlapping session denial,
output failure cleanup and an actual SIGTERM.
