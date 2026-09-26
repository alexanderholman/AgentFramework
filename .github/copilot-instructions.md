# AF agent adapter

Read the canonical repository engineering contract in `AGENTS.md` and the nearest
applicable local `AGENTS.md` before editing. These contracts also govern OpenCode,
Qwen and other workers; do not maintain a separate architecture or duplicate logic.

Use the committed lockfile and `python3 scripts/af_dev.py --action check --execute`
for the full gate. Preserve the append-only records and runtime authorization.
Continuation state is in `tasks/autonomous-completion.md`.
