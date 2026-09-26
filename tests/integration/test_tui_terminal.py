"""Real POSIX terminal input, resize, signals and original mode restoration."""

import os
import signal
import struct

import pytest

from af.application.tui import TuiKernel
from af.framework.tui import (
    KeyEvent,
    MouseEvent,
    ResizeEvent,
    TextEvent,
    TuiApplicationDescriptor,
    TuiError,
    TuiRegistry,
    TuiView,
    TuiWidget,
    WidgetKind,
)
from af.infrastructure.tui import TerminalTuiDriver, render_lines, safe_terminal_line

pytestmark = pytest.mark.skipif(os.name != "posix", reason="real POSIX PTY conformance")


@pytest.fixture
def terminal():
    import fcntl
    import termios

    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    with os.fdopen(os.dup(slave), "r") as reader, os.fdopen(os.dup(slave), "w") as writer:
        try:
            yield master, slave, reader, writer
        finally:
            os.close(master)
            os.close(slave)


def test_real_terminal_keys_mouse_resize_and_close_restore_original_modes(terminal):
    import fcntl
    import termios

    master, slave, reader, writer = terminal
    attributes = termios.tcgetattr(slave)
    old_signals = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    driver = TerminalTuiDriver(reader, writer)
    try:
        assert driver.open() == ResizeEvent(80, 24)
        assert termios.tcgetattr(slave) != attributes
        view = TuiView(
            "Title",
            (
                TuiWidget("input", WidgetKind.INPUT, "Prompt"),
                TuiWidget("exit", WidgetKind.BUTTON, "Exit"),
            ),
            focused_id="input",
        )
        driver.render(view)
        os.write(master, b"hello\t\x1b[B\r\x1b[<0;2;3M\x7f")
        assert driver.read_event(0.1) == TextEvent("hello")
        assert driver.read_event(0.1) == KeyEvent("tab")
        assert driver.read_event(0.1) == KeyEvent("down")
        assert driver.read_event(0.1) == KeyEvent("enter")
        assert driver.read_event(0.1) == MouseEvent("exit")
        assert driver.read_event(0.1) == KeyEvent("backspace")
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 40, 0, 0))
        assert driver.read_event(0.1) == ResizeEvent(40, 12)
        os.write(master, b"\x1b")
        assert driver.read_event(0.01) is None
        assert driver.read_event(0.01) == KeyEvent("escape")
    finally:
        driver.close()
    assert termios.tcgetattr(slave) == attributes
    assert all(signal.getsignal(number) == original for number, original in old_signals.items())
    assert not reader.closed and not writer.closed
    driver.close()


def test_sigterm_during_kernel_turn_restores_terminal_and_has_signal_exit_code(
    application, terminal
):
    import termios

    _, slave, reader, writer = terminal
    attributes = termios.tcgetattr(slave)
    original = signal.getsignal(signal.SIGTERM)

    class InterruptingDriver(TerminalTuiDriver):
        def read_event(self, timeout_seconds):
            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("signal handler did not interrupt")

    class App:
        closed = False

        def start(self, turn):
            return TuiView("Test", ())

        def handle(self, event, turn):
            raise AssertionError("cancelled turn ran")

        def close(self):
            self.closed = True

    app, registry = App(), TuiRegistry()
    registry.register(TuiApplicationDescriptor("test", "Test"), lambda: app)
    result = TuiKernel(application.container, registry).run(
        "test", InterruptingDriver(reader, writer)
    )
    assert result.exit_code == 143 and result.reason == "tui_cancelled"
    assert app.closed
    assert termios.tcgetattr(slave) == attributes
    assert signal.getsignal(signal.SIGTERM) == original


def test_terminal_renderer_does_not_execute_user_control_sequences_or_overflow_width():
    malicious = "\x1b]52;c;clipboard\x07\n\x1b[31m雪雪a\u202e"
    rendered = safe_terminal_line(malicious, 12)
    assert not any(ord(char) < 32 or char == "\u202e" for char in rendered)
    assert safe_terminal_line("雪雪abc", 5) == "雪雪a"
    view = TuiView(
        "Title",
        (TuiWidget("disabled", WidgetKind.BUTTON, "Danger", disabled=True),),
        rows=5,
        columns=40,
    )
    assert "unavailable" in "\n".join(render_lines(view))


def test_terminal_session_lease_rejects_overlap_and_releases_after_close(terminal):
    import termios

    _, slave, reader, writer = terminal
    attributes = termios.tcgetattr(slave)
    original = signal.getsignal(signal.SIGTERM)
    first = TerminalTuiDriver(reader, writer)
    second = TerminalTuiDriver(reader, writer)
    first.open()
    active = termios.tcgetattr(slave)
    active_handler = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(TuiError, match="tui_terminal_in_use"):
            second.open()
        second.close()
        assert termios.tcgetattr(slave) == active
        assert signal.getsignal(signal.SIGTERM) == active_handler
        with pytest.raises(TuiError, match="tui_terminal_already_open"):
            first.open()
    finally:
        first.close()
    assert termios.tcgetattr(slave) == attributes
    assert signal.getsignal(signal.SIGTERM) == original


def test_broken_terminal_output_still_restores_modes_signals_and_releases_lease(
    application, terminal
):
    import termios

    _, slave, reader, writer = terminal
    attributes = termios.tcgetattr(slave)
    original = signal.getsignal(signal.SIGTERM)

    class BrokenOutput:
        def fileno(self):
            return writer.fileno()

        def isatty(self):
            return True

        def write(self, text):
            raise OSError("secret output device details")

    class App:
        closed = False

        def start(self, turn):
            raise AssertionError("application starts after failed terminal open")

        def handle(self, event, turn):
            raise AssertionError("application receives input after failed terminal open")

        def close(self):
            self.closed = True

    app, registry = App(), TuiRegistry()
    registry.register(TuiApplicationDescriptor("test", "Test"), lambda: app)
    result = TuiKernel(application.container, registry).run(
        "test", TerminalTuiDriver(reader, BrokenOutput())
    )
    assert result.exit_code == 1 and result.reason == "tui_restore_failed"
    assert app.closed
    assert termios.tcgetattr(slave) == attributes
    assert signal.getsignal(signal.SIGTERM) == original
    replacement = TerminalTuiDriver(reader, writer)
    try:
        replacement.open()
    finally:
        replacement.close()
    assert termios.tcgetattr(slave) == attributes
    assert signal.getsignal(signal.SIGTERM) == original
    try:
        assert second.open() == ResizeEvent(80, 24)
    finally:
        second.close()
    assert termios.tcgetattr(slave) == attributes
    assert signal.getsignal(signal.SIGTERM) == original
