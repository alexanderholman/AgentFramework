"""Bounded terminal interaction contracts; no terminal or application IO."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from af.framework.container import Container, ProviderRegistrar, Scope
from af.framework.context import ExecutionContext
from af.framework.errors import AFError

TUI_APPLICATIONS_TAG = "af.tui.applications"


class TuiError(AFError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class TuiInterrupted(BaseException):
    def __init__(self, signal_number: int) -> None:
        self.signal_number = signal_number


def _identifier(value: str) -> None:
    if type(value) is not str or re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", value) is None:
        raise ValueError("invalid TUI identifier")


def _text(value: str, maximum: int) -> None:
    if type(value) is not str or len(value) > maximum:
        raise ValueError("invalid bounded TUI text")


@dataclass(frozen=True, slots=True)
class KeyEvent:
    key: str

    def __post_init__(self) -> None:
        if self.key not in {
            "enter",
            "tab",
            "backtab",
            "backspace",
            "up",
            "down",
            "escape",
            "ctrl-c",
            "ctrl-d",
        }:
            raise ValueError("unsupported TUI key")


@dataclass(frozen=True, slots=True)
class TextEvent:
    text: str

    def __post_init__(self) -> None:
        _text(self.text, 10_000)
        if any(ord(char) < 32 or 127 <= ord(char) < 160 for char in self.text):
            raise ValueError("TUI text cannot contain terminal controls")


@dataclass(frozen=True, slots=True)
class ResizeEvent:
    columns: int
    rows: int

    def __post_init__(self) -> None:
        if any(
            type(size) is not int or not 1 <= size <= 1000 for size in (self.columns, self.rows)
        ):
            raise ValueError("invalid TUI dimensions")


@dataclass(frozen=True, slots=True)
class MouseEvent:
    """An adapter hit-test identifies one visible widget; coordinates carry no authority."""

    widget_id: str

    def __post_init__(self) -> None:
        _identifier(self.widget_id)


type TuiEvent = KeyEvent | TextEvent | ResizeEvent | MouseEvent


class WidgetKind(StrEnum):
    TEXT = "text"
    INPUT = "input"
    BUTTON = "button"


@dataclass(frozen=True, slots=True)
class TuiWidget:
    id: str
    kind: WidgetKind
    label: str
    value: str = ""
    disabled: bool = False

    def __post_init__(self) -> None:
        _identifier(self.id)
        _text(self.label, 512)
        _text(self.value, 32768)
        if not isinstance(self.kind, WidgetKind) or type(self.disabled) is not bool:
            raise ValueError("invalid TUI widget")

    @property
    def focusable(self) -> bool:
        return self.kind is not WidgetKind.TEXT and not self.disabled


@dataclass(frozen=True, slots=True)
class TuiView:
    title: str
    widgets: tuple[TuiWidget, ...]
    status: str = ""
    focused_id: str | None = None
    columns: int = 80
    rows: int = 24
    done: bool = False

    def __post_init__(self) -> None:
        _text(self.title, 256)
        _text(self.status, 512)
        ResizeEvent(self.columns, self.rows)
        if (
            type(self.widgets) is not tuple
            or len(self.widgets) > 128
            or type(self.done) is not bool
        ):
            raise ValueError("invalid TUI view")
        if any(not isinstance(widget, TuiWidget) for widget in self.widgets):
            raise ValueError("invalid TUI widgets")
        if len({widget.id for widget in self.widgets}) != len(self.widgets):
            raise ValueError("duplicate TUI widget")
        if self.focused_id is not None and self.focused_id not in self.focusable_ids:
            raise ValueError("TUI focus must identify an enabled interactive widget")

    @property
    def focusable_ids(self) -> tuple[str, ...]:
        return tuple(widget.id for widget in self.widgets if widget.focusable)

    @property
    def visible_widgets(self) -> tuple[TuiWidget, ...]:
        count = max(0, self.rows - 3)
        focused = next(
            (index for index, widget in enumerate(self.widgets) if widget.id == self.focused_id), 0
        )
        start = max(0, focused - count + 1)
        return self.widgets[start : start + count]


@dataclass(frozen=True, slots=True)
class TuiTurn:
    container: Container
    context: ExecutionContext
    focused_id: str | None
    columns: int
    rows: int


class TuiApplication(Protocol):
    def start(self, turn: TuiTurn) -> TuiView: ...
    def handle(self, event: TuiEvent, turn: TuiTurn) -> TuiView: ...
    def close(self) -> None: ...


class TuiDriver(Protocol):
    @property
    def can_interact(self) -> bool: ...
    def open(self) -> ResizeEvent: ...
    def read_event(self, timeout_seconds: float) -> TuiEvent | None: ...
    def render(self, view: TuiView) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TuiApplicationDescriptor:
    id: str
    title: str
    version: str = "1.0.0"

    def __post_init__(self) -> None:
        _identifier(self.id)
        _text(self.title, 256)
        if not self.title or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.version) is None:
            raise ValueError("invalid TUI application descriptor")


@dataclass(frozen=True, slots=True)
class TuiContribution:
    descriptor: TuiApplicationDescriptor
    factory: Callable[[], TuiApplication]


def bind_tui_application(registrar: ProviderRegistrar, contribution: TuiContribution) -> None:
    token = object()
    registrar.bind(token, lambda _: contribution, scope=Scope.APPLICATION, owned=False)
    registrar.tag(TUI_APPLICATIONS_TAG, token)


class TuiRegistry:
    def __init__(self) -> None:
        self._applications: dict[str, TuiContribution] = {}
        self._frozen = False

    def register(
        self, descriptor: TuiApplicationDescriptor, factory: Callable[[], TuiApplication]
    ) -> None:
        if self._frozen or descriptor.id in self._applications or not callable(factory):
            raise ValueError("TUI application registration is frozen, duplicate or invalid")
        self._applications[descriptor.id] = TuiContribution(descriptor, factory)

    def catalogue(self) -> tuple[TuiApplicationDescriptor, ...]:
        return tuple(self._applications[key].descriptor for key in sorted(self._applications))

    def create(self, application_id: str) -> TuiApplication:
        self._frozen = True
        item = self._applications.get(application_id)
        if item is None:
            raise TuiError("tui_application_unavailable")
        return item.factory()


@dataclass(frozen=True, slots=True)
class TuiRunResult:
    exit_code: int
    reason: str
    turns: int
    final_view: TuiView | None
