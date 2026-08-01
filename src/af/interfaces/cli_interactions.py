"""Testable CLI interaction contracts for provider-contributed commands."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import typer

from af.framework.errors import AFError


class InteractiveInputUnavailable(AFError):
    """Raised when a CLI command requests prompting without an installed adapter."""

    def __init__(self) -> None:
        super().__init__("interactive input is unavailable for this CLI invocation")


@dataclass(frozen=True, slots=True)
class PromptEvent:
    kind: str
    message: str


@runtime_checkable
class CliInteraction(Protocol):
    """Minimal prompt contract shared by CLI command contributors."""

    def text(self, *, message: str) -> str: ...

    def confirm(self, *, message: str, default: bool = False) -> bool: ...


@dataclass(slots=True)
class NonInteractiveCliInteraction:
    """Fail closed when a command tries to prompt in a noninteractive invocation."""

    def text(self, *, message: str) -> str:
        del message
        _fail_interaction(InteractiveInputUnavailable())

    def confirm(self, *, message: str, default: bool = False) -> bool:
        del message, default
        _fail_interaction(InteractiveInputUnavailable())


@dataclass(slots=True)
class ScriptedCliInteraction:
    """Headless scripted prompt adapter for deterministic CLI interaction tests."""

    text_responses: Sequence[str] = ()
    confirm_responses: Sequence[bool] = ()
    events: list[PromptEvent] = field(default_factory=list)
    _text_index: int = 0
    _confirm_index: int = 0

    def text(self, *, message: str) -> str:
        self.events.append(PromptEvent(kind="text", message=message))
        if self._text_index >= len(self.text_responses):
            _fail_interaction(InteractiveInputUnavailable())
        response = self.text_responses[self._text_index]
        self._text_index += 1
        return response

    def confirm(self, *, message: str, default: bool = False) -> bool:
        self.events.append(PromptEvent(kind="confirm", message=message))
        if self._confirm_index >= len(self.confirm_responses):
            _fail_interaction(InteractiveInputUnavailable())
        response = self.confirm_responses[self._confirm_index]
        self._confirm_index += 1
        return response


def _fail_interaction(error: AFError) -> None:
    typer.echo(str(error), err=True)
    raise typer.Exit(code=2)
