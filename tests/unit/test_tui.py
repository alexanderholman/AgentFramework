"""Typed TUI turns restore resources and never reuse invocation authority."""

from dataclasses import dataclass

from af.application.tui import TuiKernel
from af.framework.context import ExecutionContext, Principal, current_context, maybe_current_context
from af.framework.tui import (
    KeyEvent,
    MouseEvent,
    ResizeEvent,
    TextEvent,
    TuiApplicationDescriptor,
    TuiRegistry,
    TuiView,
    TuiWidget,
    WidgetKind,
)
from af.infrastructure.tui import ScriptedTuiDriver


def test_tui_turns_preserve_principal_but_have_fresh_scopes(application):
    contexts, containers = [], []

    @dataclass
    class App:
        closed: bool = False

        def start(self, turn):
            contexts.append(current_context())
            containers.append(turn.container)
            return TuiView(
                "Example",
                (
                    TuiWidget("input", WidgetKind.INPUT, "Input"),
                    TuiWidget("exit", WidgetKind.BUTTON, "Exit"),
                ),
            )

        def handle(self, event, turn):
            contexts.append(current_context())
            containers.append(turn.container)
            return TuiView(
                "Example",
                (
                    TuiWidget("input", WidgetKind.INPUT, "Input"),
                    TuiWidget("exit", WidgetKind.BUTTON, "Exit"),
                ),
                done=isinstance(event, KeyEvent) and event.key == "enter",
            )

        def close(self):
            self.closed = True

    app = App()
    registry = TuiRegistry()
    registry.register(TuiApplicationDescriptor("example", "Example"), lambda: app)
    driver = ScriptedTuiDriver(
        (
            TextEvent("x"),
            KeyEvent("tab"),
            ResizeEvent(40, 12),
            MouseEvent("exit"),
            KeyEvent("enter"),
        )
    )
    context = ExecutionContext(principal=Principal("operator", "user"), tenant_id="tenant")
    result = TuiKernel(application.container, registry).run("example", driver, context=context)
    assert result.exit_code == 0 and result.reason == "completed"
    assert driver.closed and app.closed
    assert len({id(item) for item in containers}) == len(containers)
    assert len({item.correlation_id for item in contexts}) == len(contexts)
    assert all(
        item.principal == context.principal and item.tenant_id == "tenant" for item in contexts
    )
    assert maybe_current_context() is None
    assert driver.frames[-1].columns == 40 and driver.frames[-1].focused_id == "exit"


def simple_application():
    class App:
        closed = False
        starts = 0

        def __init__(self):
            self.events = []

        def start(self, turn):
            self.starts += 1
            return TuiView(
                "Example",
                (
                    TuiWidget("input", WidgetKind.INPUT, "Input"),
                    TuiWidget("exit", WidgetKind.BUTTON, "Exit"),
                ),
            )

        def handle(self, event, turn):
            self.events.append(event)
            return TuiView(
                "Example",
                (
                    TuiWidget("input", WidgetKind.INPUT, "Input"),
                    TuiWidget("exit", WidgetKind.BUTTON, "Exit"),
                ),
                done=isinstance(event, KeyEvent) and event.key == "enter",
            )

        def close(self):
            self.closed = True

    return App()


def installed_kernel(application, app, **options):
    registry = TuiRegistry()
    registry.register(TuiApplicationDescriptor("example", "Example"), lambda: app)
    return TuiKernel(application.container, registry, **options)


def test_noninteractive_requires_explicit_snapshot_and_never_reads_input(application):
    from io import StringIO

    from af.infrastructure.tui import TerminalTuiDriver

    stream = StringIO()
    app = simple_application()
    result = installed_kernel(application, app).run(
        "example", TerminalTuiDriver(StringIO("secret"), stream)
    )
    assert result.exit_code == 2 and app.starts == 0
    result = installed_kernel(application, app).run(
        "example", TerminalTuiDriver(StringIO("secret"), stream), plain_fallback=True
    )
    assert result.exit_code == 0 and result.reason == "plain_snapshot"
    assert "Example" in stream.getvalue() and "secret" not in stream.getvalue()
    assert "\x1b" not in stream.getvalue()


def test_budget_cancellation_and_deadline_stop_terminal_loop(application):
    from datetime import UTC, datetime, timedelta

    app = simple_application()
    result = installed_kernel(application, app).run(
        "example", ScriptedTuiDriver((TextEvent("x"),)), max_turns=1
    )
    assert result.reason == "tui_turn_budget_exceeded" and result.turns == 1
    cancelled = ExecutionContext()
    cancelled.cancellation.cancel()
    app = simple_application()
    result = installed_kernel(application, app).run(
        "example", ScriptedTuiDriver(()), context=cancelled
    )
    assert result.exit_code == 130 and app.starts == 0
    app = simple_application()
    result = installed_kernel(application, app).run(
        "example",
        ScriptedTuiDriver(()),
        context=ExecutionContext(deadline=datetime.now(UTC) - timedelta(seconds=1)),
    )
    assert result.exit_code == 124 and app.starts == 0


def test_focus_navigation_scroll_and_invisible_mouse_targets(application):
    app = simple_application()
    driver = ScriptedTuiDriver(
        (
            KeyEvent("backtab"),
            KeyEvent("up"),
            KeyEvent("down"),
            MouseEvent("unknown"),
            ResizeEvent(12, 5),
            KeyEvent("enter"),
        )
    )
    result = installed_kernel(application, app).run("example", driver)
    assert result.exit_code == 0
    assert result.final_view.focused_id == "exit"
    assert all(not isinstance(event, MouseEvent) for event in app.events)


def test_driver_and_application_failures_always_cleanup_without_exception_text(application):
    class FailingDriver(ScriptedTuiDriver):
        def read_event(self, timeout_seconds):
            raise OSError("database-password-is-secret")

    app = simple_application()
    driver = FailingDriver(())
    result = installed_kernel(application, app).run("example", driver)
    assert result.exit_code == 1 and result.reason == "tui_application_failed"
    assert app.closed and driver.closed
    app = simple_application()
    app.close = lambda: (_ for _ in ()).throw(ValueError("secret"))
    driver = ScriptedTuiDriver((KeyEvent("enter"),))
    result = installed_kernel(application, app).run("example", driver)
    assert result.reason == "tui_cleanup_failed" and driver.closed


def test_registry_and_event_bounds_fail_before_application_creation(application):
    import pytest

    from af.framework.tui import TuiError

    registry = TuiRegistry()
    app = simple_application()
    descriptor = TuiApplicationDescriptor("example", "Example")
    registry.register(descriptor, lambda: app)
    with pytest.raises(ValueError):
        registry.register(descriptor, lambda: app)
    assert registry.catalogue() == (descriptor,)
    with pytest.raises(TuiError):
        registry.create("missing")
    with pytest.raises(ValueError):
        registry.register(TuiApplicationDescriptor("other", "Other"), lambda: app)
    for make in (
        lambda: KeyEvent("shell"),
        lambda: TextEvent("x" * 10001),
        lambda: TextEvent("\x1b[31m"),
        lambda: ResizeEvent(0, 24),
        lambda: MouseEvent("../bad"),
        lambda: TuiView("x", (TuiWidget("x", WidgetKind.BUTTON, "x"),) * 2),
        lambda: TuiView("x", (), focused_id="missing"),
        lambda: TuiApplicationDescriptor("x", "", "v1"),
        lambda: TuiWidget("x", "button", "x"),
        lambda: TuiWidget("x", WidgetKind.TEXT, "x" * 513),
        lambda: ScriptedTuiDriver(("bad-event",)),
    ):
        with pytest.raises(ValueError):
            make()
    kernel = installed_kernel(application, simple_application())
    for options in ({"max_turns": 0}, {"max_seconds": float("nan")}, {"max_seconds": 0}):
        with pytest.raises(ValueError):
            kernel.run("example", ScriptedTuiDriver(()), **options)
