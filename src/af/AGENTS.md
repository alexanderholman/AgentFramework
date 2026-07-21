# AF Python agent guidance

This directory is the Python 3.12 AF application. Preserve its layered dependency
direction:

```text
interfaces, infrastructure -> application, domain, framework
bootstrap -> all layers
```

`framework` owns generic execution mechanics and contracts. `domain` owns AF's
classifiers, procedures, verifier, and Task port. `application` owns use cases.
`infrastructure` implements persistence and tracing ports. `interfaces` only
translate transports. Do not move transport or SQLAlchemy concepts inward.

Develop test-first and keep repeated behavior in the innermost appropriate
layer. Use constructor/factory dependency injection, strict typing, AF-specific
exceptions, and isolated application instances. The deterministic strawberry
example must generalize to any one-character/text pair.

Before finishing Python changes, run:

```bash
python scripts/af_dev.py --action check --execute
```

