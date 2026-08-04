"""In-memory flag store for testing and lightweight deployments."""

from __future__ import annotations

from .contracts import FlagDefinition, FlagKey, FlagStore


class InMemoryFlagStore(FlagStore):
    """Thread-unsafe in-memory implementation of FlagStore."""

    def __init__(self) -> None:
        self._definitions: dict[FlagKey, FlagDefinition] = {}

    def get_definition(self, key: FlagKey) -> FlagDefinition | None:
        return self._definitions.get(key)

    def set_definition(self, definition: FlagDefinition) -> None:
        self._definitions[definition.key] = definition

    def delete_definition(self, key: FlagKey) -> bool:
        if key in self._definitions:
            del self._definitions[key]
            return True
        return False

    def list_definitions(self) -> list[FlagDefinition]:
        return list(self._definitions.values())
