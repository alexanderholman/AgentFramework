"""Stateful terminal loop with fresh policy context and scope for every turn."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from math import isfinite
from uuid import uuid4

from af.framework.container import Container, InvocationKind
from af.framework.context import AcceptanceScope, ExecutionContext
from af.framework.tui import (
    KeyEvent,
    MouseEvent,
    ResizeEvent,
    TuiApplication,
    TuiDriver,
    TuiError,
    TuiEvent,
    TuiInterrupted,
    TuiRegistry,
    TuiRunResult,
    TuiTurn,
    TuiView,
)


class TuiKernel:
    def __init__(
        self,
        container: Container,
        registry: TuiRegistry,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.container = container
        self.registry = registry
        self._clock = clock

    def run(
        self,
        application_id: str,
        driver: TuiDriver,
        *,
        context: ExecutionContext | None = None,
        plain_fallback: bool = False,
        max_turns: int = 10_000,
        max_seconds: float = 3600,
    ) -> TuiRunResult:
        if type(max_turns) is not int or not 1 <= max_turns <= 100_000:
            raise ValueError("invalid TUI turn budget")
        if (
            type(max_seconds) not in (int, float)
            or not isfinite(max_seconds)
            or not 0 < max_seconds <= 86_400
        ):
            raise ValueError("invalid TUI time budget")
        base = context or ExecutionContext(
            requested_scope=AcceptanceScope.DETERMINISTIC,
            effective_scope=AcceptanceScope.DETERMINISTIC,
        )
        instance: TuiApplication | None = None
        view: TuiView | None = None
        turns = 0
        exit_code, reason = 0, "completed"
        started = self._clock()

        def cancelled() -> bool:
            return base.cancellation.cancelled

        def expired() -> bool:
            return self._clock() - started >= max_seconds or (
                base.deadline is not None and datetime.now(UTC) >= base.deadline
            )

        def invoke(event: TuiEvent | None, size: ResizeEvent) -> TuiView:
            assert instance is not None
            current = replace(base, correlation_id=uuid4().hex, causation_id=base.correlation_id)
            with self.container.invocation_scope(
                context=current, kind=InvocationKind.INTERACTION
            ) as scope:
                turn = TuiTurn(
                    scope, current, view.focused_id if view else None, size.columns, size.rows
                )
                rendered = instance.start(turn) if event is None else instance.handle(event, turn)
                if not isinstance(rendered, TuiView):
                    raise TuiError("tui_invalid_view")
                focus = rendered.focused_id or (view.focused_id if view else None)
                if focus not in rendered.focusable_ids:
                    focus = next(iter(rendered.focusable_ids), None)
                return replace(rendered, columns=size.columns, rows=size.rows, focused_id=focus)

        try:
            if not driver.can_interact and not plain_fallback:
                raise TuiError("tui_requires_terminal_or_explicit_plain_fallback")
            if cancelled():
                raise TuiError("tui_cancelled")
            if expired():
                raise TuiError("tui_deadline")
            instance = self.registry.create(application_id)
            size = driver.open()
            view = invoke(None, size)
            driver.render(view)
            if not driver.can_interact:
                reason = "plain_snapshot"
            else:
                while not view.done:
                    if cancelled():
                        exit_code, reason = 130, "tui_cancelled"
                        break
                    if expired():
                        exit_code, reason = 124, "tui_deadline"
                        break
                    if turns >= max_turns:
                        exit_code, reason = 2, "tui_turn_budget_exceeded"
                        break
                    event = driver.read_event(0.1)
                    if event is None:
                        continue
                    turns += 1
                    if isinstance(event, KeyEvent) and event.key in {"ctrl-c", "ctrl-d"}:
                        base.cancellation.cancel()
                        exit_code, reason = 130, "tui_cancelled"
                        break
                    if isinstance(event, ResizeEvent):
                        size = event
                    elif isinstance(event, KeyEvent) and event.key in {
                        "tab",
                        "backtab",
                        "up",
                        "down",
                    }:
                        ids = view.focusable_ids
                        if ids:
                            current = ids.index(view.focused_id) if view.focused_id in ids else 0
                            offset = -1 if event.key in {"backtab", "up"} else 1
                            view = replace(view, focused_id=ids[(current + offset) % len(ids)])
                    elif isinstance(event, MouseEvent):
                        if event.widget_id not in {
                            widget.id for widget in view.visible_widgets if widget.focusable
                        }:
                            continue
                        view = replace(view, focused_id=event.widget_id)
                    view = invoke(event, size)
                    driver.render(view)
        except TuiInterrupted as error:
            base.cancellation.cancel()
            exit_code, reason = 128 + error.signal_number, "tui_cancelled"
        except KeyboardInterrupt:
            base.cancellation.cancel()
            exit_code, reason = 130, "tui_cancelled"
        except TuiError as error:
            exit_code = {"tui_cancelled": 130, "tui_deadline": 124}.get(error.reason, 2)
            reason = error.reason
        except Exception:
            exit_code, reason = 1, "tui_application_failed"
        finally:
            try:
                if instance is not None:
                    instance.close()
            except Exception:
                exit_code, reason = 1, "tui_cleanup_failed"
            try:
                driver.close()
            except Exception:
                exit_code, reason = 1, "tui_restore_failed"
        return TuiRunResult(exit_code, reason, turns, view)
