# Isolated AF-Routed OpenCode

This integration installs a pinned OpenCode instance with its own home, XDG directories,
configuration, cache, state, temporary directory, provider authentication, agents, and
plugins. It does not load MMPR, PR Universe, AgentFactory, project OpenCode configuration,
default plugins, or external skills.

The only primary agent is `dispatcher`. Every actionable request must pass through the
plugin's `af_route` tool, which invokes AF's deterministic `route-agent` application
boundary. A routed result receives a short-lived, single-use ticket. The plugin permits
delegation only to the selected specialist and blocks sensitive specialist tools until the
child session validates that ticket. Ambiguous AF decisions do not receive tickets.

## Install

Preview first:

```bash
python3 scripts/install_isolated_opencode.py \
  --root ~/.local/opencode-af \
  --source-binary ~/.opencode/bin/opencode \
  --source-root . \
  --uv-binary /path/to/uv \
  --auth-source ~/.local/share/opencode/auth.json \
  --launcher-link ~/bin/opencode-af
```

Repeat with `--execute` to install. Authentication is copied only when the isolated target
does not already have `auth.json`.

Launch from any desired workspace:

```bash
opencode-af
```

## Verification

```bash
opencode-af --version
opencode-af debug paths
opencode-af debug config
opencode-af agent list
```

All reported mutable paths must remain below the chosen isolated root. The effective config
must select `dispatcher`, list only the five AF specialist roles, and contain no MMPR or PR
Universe assets.

Run the deterministic route gate tests with the registered development action:

```bash
python3 scripts/af_dev.py --action opencode-integration --execute
```

## Rollback

Remove the launcher symlink and isolated root. No existing OpenCode configuration is
modified:

```bash
rm ~/bin/opencode-af
rm -rf ~/.local/opencode-af
```
