---
applyTo: "src/af/**/*.py,src/af/*.py"
---

# Python source requirements

- Target Python 3.12 and follow the Ruff and strict-mypy configuration in
  `pyproject.toml`.
- Prefer small cohesive modules, modern built-in generic types, explicit return
  types, immutable value objects where practical, and `Protocol` for ports.
- Raise AF/domain exceptions from inner layers; map them to transport errors
  only in interface code.
- Preserve the dependency direction
  `interfaces/infrastructure -> application/domain/framework`. Only
  `bootstrap.py` composes across all layers.
- Keep I/O, environment reads, ORM sessions, and web framework objects outside
  framework and domain logic.
- Prefer constructor/factory injection to module-level state. An application
  created by `create_application()` must be isolated and replaceable in tests.
- Add a regression test for every behavior change or bug fix.

Validate with:

```bash
python scripts/af_dev.py --action check --execute
```

