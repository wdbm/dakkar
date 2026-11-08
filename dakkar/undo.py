"""
Coordinate session-only undo and redo for safely-reversible operations.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import Gio


UndoCallback = Callable[[Exception | None, list[Gio.File]], None]
UndoOperation = Callable[[UndoCallback], None]


class CannotUndoError(RuntimeError):
    """
    Report that the latest operation has no safe inverse.
    """


@dataclass(frozen=True, slots=True)
class UndoableAction:
    label: str
    undo: UndoOperation
    redo: UndoOperation


class UndoManager:
    def __init__(self) -> None:
        self._undo: list[UndoableAction] = []
        self._redo: list[UndoableAction] = []
        self._busy = False

    @property
    def can_undo(self) -> bool:
        return bool(self._undo) and not self._busy

    @property
    def can_redo(self) -> bool:
        return bool(self._redo) and not self._busy

    @property
    def undo_label(self) -> str | None:
        return self._undo[-1].label if self._undo else None

    @property
    def redo_label(self) -> str | None:
        return self._redo[-1].label if self._redo else None

    def push(self, action: UndoableAction) -> None:
        if self._busy:
            raise RuntimeError("An undo operation is already running.")
        self._undo.append(action)
        self._redo.clear()

    def block(self, label: str) -> None:
        """
        Replace older history with an explicit non-undoable operation barrier.
        """

        def cannot_undo(callback: UndoCallback) -> None:
            callback(CannotUndoError(f"{label} cannot be undone safely."), [])

        self._undo = [UndoableAction(label, cannot_undo, cannot_undo)]
        self._redo.clear()

    def undo(self, callback: UndoCallback) -> bool:
        if not self.can_undo:
            return False
        action = self._undo.pop()
        self._busy = True
        action.undo(lambda error, changed: self._finish(action, False, error, changed, callback))
        return True

    def redo(self, callback: UndoCallback) -> bool:
        if not self.can_redo:
            return False
        action = self._redo.pop()
        self._busy = True
        action.redo(lambda error, changed: self._finish(action, True, error, changed, callback))
        return True

    def _finish(
        self,
        action: UndoableAction,
        was_redo: bool,
        error: Exception | None,
        changed: list[Gio.File],
        callback: UndoCallback,
    ) -> None:
        self._busy = False
        if error is None:
            (self._undo if was_redo else self._redo).append(action)
        else:
            (self._redo if was_redo else self._undo).append(action)
        callback(error, changed)
