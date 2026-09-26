"""Read-only application metadata for humans and heterogeneous agent workers."""

from __future__ import annotations

from af.framework.managers import DriverManifest
from af.framework.routing import ProcedureRegistry
from af.framework.types import ProcedureDescriptor


class IntrospectionService:
    def __init__(
        self,
        registry: ProcedureRegistry,
        providers: tuple[str, ...],
        drivers: tuple[DriverManifest, ...] = (),
    ) -> None:
        self._registry = registry
        self._providers = providers
        self._drivers = drivers

    def drivers(self) -> tuple[DriverManifest, ...]:
        return self._drivers

    def procedures(self) -> tuple[ProcedureDescriptor, ...]:
        return tuple(sorted(self._registry.catalogue(), key=lambda item: item.procedure_id))

    def providers(self) -> tuple[str, ...]:
        return self._providers

    def describe(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "lifecycle": ["classify", "route", "execute", "verify", "record", "test", "reuse"],
            "providers": list(self._providers),
            "procedures": [item.procedure_id for item in self.procedures()],
            "commands": {
                "install": "uv sync --locked --extra dev",
                "focused_test": "uv run pytest PATH",
                "check": "python3 scripts/af_dev.py --action check --execute",
                "package": "python3 scripts/af_dev.py --action package --execute",
                "schemas": "af schemas",
            },
            "source_map": {
                "contracts": "src/af/framework",
                "procedures": "src/af/domain",
                "use_cases": "src/af/application",
                "adapters": "src/af/infrastructure",
                "interfaces": "src/af/interfaces",
                "composition": "src/af/bootstrap.py",
                "tests": "tests",
            },
            "engineering_contract": "AGENTS.md",
            "authority": "Capability discovery does not authorize execution.",
        }
