"""Application-owned task terminal view model using shared TaskController actions."""

from __future__ import annotations

from af.application.locales import LocaleSelection, Localizer, Message
from af.application.tasks import TaskController
from af.domain.tasks import Task
from af.framework.errors import AFError
from af.framework.tui import KeyEvent, TextEvent, TuiEvent, TuiTurn, TuiView, TuiWidget, WidgetKind
from af.framework.types import PROMPT_MAX_LENGTH


class TaskTuiApplication:
    def __init__(self, tasks: TaskController, localizer: Localizer) -> None:
        self._tasks = tasks
        self._localizer = localizer
        self._items: tuple[Task, ...] = ()
        self._prompt = ""
        self._selected: str | None = None
        self._confirm_delete: str | None = None
        self._output = ""
        self._status = ""
        self.history: tuple[str, ...] = ()

    def _label(self, message_id: str, turn: TuiTurn) -> str:
        selected = LocaleSelection.parse(language=turn.context.locale or "en")
        return self._localizer.render(Message(message_id, {}), selected).text

    def _refresh(self) -> None:
        self._items = tuple(self._tasks.list(limit=25))

    def start(self, turn: TuiTurn) -> TuiView:
        self._refresh()
        return self._view(turn)

    def handle(self, event: TuiEvent, turn: TuiTurn) -> TuiView:
        focus = turn.focused_id
        if isinstance(event, TextEvent) and focus == "prompt":
            if len(self._prompt) + len(event.text) <= PROMPT_MAX_LENGTH:
                self._prompt += event.text
            else:
                self._status = "Prompt exceeds the 10000-character limit."
        if isinstance(event, KeyEvent):
            if event.key == "backspace" and focus == "prompt":
                self._prompt = self._prompt[:-1]
            elif event.key == "escape":
                if self._confirm_delete is not None:
                    self._confirm_delete = None
                elif self._selected is not None:
                    self._selected, self._prompt, self._output = None, "", ""
                else:
                    return self._view(turn, done=True)
            elif event.key == "enter":
                if focus == "quit":
                    return self._view(turn, done=True)
                try:
                    self._activate(focus)
                except AFError:
                    self._status = "Task operation denied or unsupported."
        return self._view(turn)

    def _activate(self, focus: str | None) -> None:
        if focus in {"prompt", "save"}:
            task = (
                self._tasks.create(self._prompt)
                if self._selected is None
                else self._tasks.update(self._selected, self._prompt)
            )
            self._selected, self._output = task.id, task.output
            self._status = "Task saved; verified=" + str(task.verified).lower()
            self.history = (*self.history[-49:], task.id)
            self._refresh()
        elif focus == "new":
            self._selected, self._prompt, self._output, self._status = None, "", "", ""
        elif focus == "refresh":
            self._refresh()
        elif focus == "delete" and self._selected is not None:
            self._confirm_delete = self._selected
        elif focus == "cancel-delete":
            self._confirm_delete = None
        elif focus == "confirm-delete" and self._confirm_delete is not None:
            self._tasks.delete(self._confirm_delete)
            self._confirm_delete, self._selected, self._prompt, self._output = None, None, "", ""
            self._status = "Task deleted."
            self._refresh()
        elif focus is not None and focus.startswith("task."):
            task = self._tasks.get(focus[5:])
            self._selected, self._prompt, self._output = task.id, task.prompt, task.output
            self._status = "Task selected; edit its prompt to update."

    def _view(self, turn: TuiTurn, *, done: bool = False) -> TuiView:
        widgets: tuple[TuiWidget, ...]
        if self._confirm_delete is not None:
            widgets = (
                TuiWidget(
                    "delete-warning",
                    WidgetKind.TEXT,
                    "Delete the selected saved task? This cannot be undone.",
                ),
                TuiWidget("confirm-delete", WidgetKind.BUTTON, "Confirm deletion"),
                TuiWidget("cancel-delete", WidgetKind.BUTTON, "Cancel"),
            )
        else:
            widgets = (
                TuiWidget("prompt", WidgetKind.INPUT, self._label("web.ask", turn), self._prompt),
                TuiWidget(
                    "save",
                    WidgetKind.BUTTON,
                    self._label("web.update" if self._selected else "web.execute", turn),
                ),
                TuiWidget("new", WidgetKind.BUTTON, "New task"),
                TuiWidget("refresh", WidgetKind.BUTTON, "Refresh"),
                TuiWidget(
                    "delete",
                    WidgetKind.BUTTON,
                    self._label("web.delete", turn),
                    disabled=self._selected is None,
                ),
                TuiWidget("quit", WidgetKind.BUTTON, "Quit"),
                TuiWidget(
                    "result",
                    WidgetKind.TEXT,
                    "Result",
                    self._output[:32700]
                    + (" [display truncated]" if len(self._output) > 32700 else ""),
                ),
                *(
                    TuiWidget("task." + task.id, WidgetKind.BUTTON, task.prompt[:500])
                    for task in self._items
                ),
            )
        return TuiView(self._label("web.tasks", turn), widgets, self._status, done=done)

    def close(self) -> None:
        self._prompt = self._output = ""
        self._items = ()
