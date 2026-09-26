"""Scripted and POSIX terminal TUI adapters with strict restoration ownership."""

from __future__ import annotations

import codecs
import os
import re
import select
import signal
import sys
import threading
import unicodedata
from collections import deque
from collections.abc import Callable, Iterable
from itertools import islice
from types import FrameType
from typing import TextIO

from af.framework.tui import (
    KeyEvent,
    MouseEvent,
    ResizeEvent,
    TextEvent,
    TuiError,
    TuiEvent,
    TuiInterrupted,
    TuiView,
    WidgetKind,
)


class ScriptedTuiDriver:
    def __init__(self, events: Iterable[TuiEvent], *, columns: int = 80, rows: int = 24) -> None:
        self._size = ResizeEvent(columns, rows)
        self._events = deque(islice(events, 100_001))
        if len(self._events) > 100_000:
            raise ValueError("TUI script exceeds event limit")
        if any(
            not isinstance(item, (KeyEvent, TextEvent, ResizeEvent, MouseEvent))
            for item in self._events
        ):
            raise ValueError("TUI script contains an invalid event")
        self.frames: list[TuiView] = []
        self.closed = False

    @property
    def can_interact(self) -> bool:
        return True

    def open(self) -> ResizeEvent:
        return self._size

    def read_event(self, timeout_seconds: float) -> TuiEvent:
        del timeout_seconds
        return self._events.popleft() if self._events else KeyEvent("ctrl-d")

    def render(self, view: TuiView) -> None:
        if len(self.frames) == 1024:
            del self.frames[0]
        self.frames.append(view)

    def close(self) -> None:
        self.closed = True


def safe_terminal_line(text: str, columns: int) -> str:
    """Drop control sequences and conservatively bound display cells, including wide text."""
    result: list[str] = []
    width = 0
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cf"}:
            continue
        cells = (
            0
            if unicodedata.combining(character)
            else (2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1)
        )
        if width + cells > columns:
            break
        result.append(character)
        width += cells
    return "".join(result)


def render_lines(view: TuiView) -> tuple[str, ...]:
    lines = [view.title]
    for widget in view.visible_widgets:
        prefix = "> " if widget.id == view.focused_id else "  "
        text = widget.label + (": " + widget.value if widget.value else "")
        if widget.kind is WidgetKind.BUTTON:
            text = "[" + text + "]"
        if widget.disabled:
            text += " (unavailable)"
        lines.append(prefix + text)
    lines += [view.status, "Tab/Shift-Tab: focus | Enter: activate | Esc: back | Ctrl-C: exit"]
    return tuple(safe_terminal_line(line, view.columns) for line in lines[: view.rows])


class TerminalTuiDriver:
    """POSIX cbreak terminal; other hosts have an explicit plain snapshot fallback.

    Input/output belong to the caller and are never closed. Raw user strings never
    become escape sequences. Signals and terminal flags are restored even if a
    renderer, application, or output stream fails.
    """

    def __init__(
        self,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
        *,
        plain: bool = False,
    ) -> None:
        self.input = input_stream if input_stream is not None else sys.stdin
        self.output = output_stream if output_stream is not None else sys.stdout
        self._plain = plain
        self._active = False
        self._lease_descriptor: int | None = None
        self._attributes: list[int | list[bytes | int]] | None = None
        self._handlers: dict[
            signal.Signals, int | Callable[[int, FrameType | None], None] | None
        ] = {}
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._size = ResizeEvent(80, 24)
        self._view: TuiView | None = None

    @property
    def can_interact(self) -> bool:
        return (
            not self._plain and os.name == "posix" and self.input.isatty() and self.output.isatty()
        )

    def _dimensions(self) -> ResizeEvent:
        try:
            size = os.get_terminal_size(self.output.fileno())
            return ResizeEvent(max(1, min(size.columns, 1000)), max(1, min(size.lines, 1000)))
        except (OSError, ValueError):
            return ResizeEvent(80, 24)

    def open(self) -> ResizeEvent:
        if self._lease_descriptor is not None:
            raise TuiError("tui_terminal_already_open")
        self._size = self._dimensions()
        if self.can_interact:
            import termios
            import tty

            self._acquire_lease()
            self._attributes = list(termios.tcgetattr(self.input.fileno()))
            if threading.current_thread() is threading.main_thread():
                for number in (signal.SIGINT, signal.SIGTERM):
                    self._handlers[number] = signal.getsignal(number)
                    signal.signal(number, self._interrupted)
            tty.setcbreak(self.input.fileno())
            self._active = True
            self.output.write("\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h")
            self.output.flush()
        return self._size

    def _acquire_lease(self) -> None:
        import fcntl

        descriptor: int | None = None
        try:
            descriptor = os.open(
                os.ttyname(self.input.fileno()), os.O_RDONLY | os.O_NOCTTY | os.O_CLOEXEC
            )
            expected, actual = os.fstat(self.input.fileno()), os.fstat(descriptor)
            if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                raise TuiError("tui_terminal_lock_unavailable")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lease_descriptor = descriptor
        except BlockingIOError:
            raise TuiError("tui_terminal_in_use") from None
        except OSError:
            raise TuiError("tui_terminal_lock_unavailable") from None
        finally:
            if descriptor is not None and self._lease_descriptor != descriptor:
                os.close(descriptor)

    @staticmethod
    def _interrupted(number: int, frame: FrameType | None) -> None:
        del frame
        raise TuiInterrupted(number)

    def read_event(self, timeout_seconds: float) -> TuiEvent | None:
        if not self._active:
            raise TuiError("tui_terminal_not_open")
        dimensions = self._dimensions()
        if dimensions != self._size:
            self._size = dimensions
            return dimensions
        event = self._next_event()
        if event is not None:
            return event
        readable, _, _ = select.select([self.input.fileno()], [], [], timeout_seconds)
        if not readable:
            if self._buffer.startswith("\x1b"):
                self._buffer = ""
                return KeyEvent("escape")
            return None
        raw = os.read(self.input.fileno(), 4096)
        if not raw:
            return KeyEvent("ctrl-d")
        self._buffer += self._decoder.decode(raw)
        if len(self._buffer) > 10_000:
            self._buffer = ""
            raise TuiError("tui_input_exceeds_limit")
        return self._next_event()

    def _next_event(self) -> TuiEvent | None:
        if not self._buffer:
            return None
        for sequence, key in (("\x1b[A", "up"), ("\x1b[B", "down"), ("\x1b[Z", "backtab")):
            if self._buffer.startswith(sequence):
                self._buffer = self._buffer[len(sequence) :]
                return KeyEvent(key)
        mouse = re.match(r"^\x1b\[<([0-9]{1,3});([0-9]{1,4});([0-9]{1,4})([Mm])", self._buffer)
        if mouse is not None:
            self._buffer = self._buffer[mouse.end() :]
            row = int(mouse[3]) - 2
            if (
                mouse[1] == "0"
                and mouse[4] == "M"
                and self._view is not None
                and 0 <= row < len(self._view.visible_widgets)
            ):
                widget = self._view.visible_widgets[row]
                if widget.focusable:
                    return MouseEvent(widget.id)
            return None
        first, rest = self._buffer[0], self._buffer[1:]
        keys = {
            "\r": "enter",
            "\n": "enter",
            "\t": "tab",
            "\x7f": "backspace",
            "\b": "backspace",
            "\x03": "ctrl-c",
            "\x04": "ctrl-d",
        }
        if first in keys:
            self._buffer = rest
            return KeyEvent(keys[first])
        if first == "\x1b":
            # Wait for a split bounded terminal sequence; an isolated Escape
            # becomes an event when the read timeout expires.
            if len(self._buffer) > 64:
                self._buffer = ""
            return None
        end = next(
            (
                index
                for index, char in enumerate(self._buffer)
                if ord(char) < 32 or 127 <= ord(char) < 160
            ),
            len(self._buffer),
        )
        text, self._buffer = self._buffer[:end], self._buffer[end:]
        if not text:
            self._buffer = self._buffer[1:]
            return None
        return TextEvent(text)

    def render(self, view: TuiView) -> None:
        self._view = view
        if self._active:
            self.output.write("\x1b[H\x1b[2J")
        self.output.write("\n".join(render_lines(view)) + "\n")
        self.output.flush()

    def close(self) -> None:
        try:
            if self._active:
                self.output.write("\x1b[?1006l\x1b[?1000l\x1b[?25h\x1b[?1049l")
                self.output.flush()
        finally:
            self._active = False
            try:
                if self._attributes is not None:
                    import termios

                    attributes, self._attributes = self._attributes, None
                    termios.tcsetattr(self.input.fileno(), termios.TCSANOW, attributes)
            finally:
                try:
                    for number, handler in self._handlers.items():
                        signal.signal(number, handler)
                    self._handlers.clear()
                finally:
                    if self._lease_descriptor is not None:
                        descriptor, self._lease_descriptor = self._lease_descriptor, None
                        os.close(descriptor)
