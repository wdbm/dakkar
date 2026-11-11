"""
Track file-operation activity independently of browser tabs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import time

from gi.repository import Gio, GLib, GObject

from .services import _idle_call


PROGRESS_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class OperationStatus:
    title: str
    description: str = "Preparing…"
    fraction: float | None = None


class OperationMonitor(GObject.GObject):
    """
    Retain queued activity and the latest result on the GTK main thread.
    """

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_LAST, None, ())}

    def __init__(self) -> None:
        super().__init__()
        self._jobs: dict[Gio.Cancellable, OperationStatus] = {}
        self._result: OperationStatus | None = None

    @property
    def busy(self) -> bool:
        return bool(self._jobs)

    @property
    def queued_count(self) -> int:
        return max(0, len(self._jobs) - 1)

    @property
    def cancelling(self) -> bool:
        cancellable = next(iter(self._jobs), None)
        return cancellable is not None and cancellable.is_cancelled()

    @property
    def status(self) -> OperationStatus:
        return next(iter(self._jobs.values()), None) or self._result or OperationStatus(
            "No file operations in progress", "",
        )

    @property
    def has_result(self) -> bool:
        return self._result is not None and not self.busy

    def add(self, cancellable: Gio.Cancellable, title: str) -> None:
        self._jobs[cancellable] = OperationStatus(title)
        self._result = None
        self.emit("changed")

    def update(self, cancellable: Gio.Cancellable, status: OperationStatus) -> None:
        if cancellable in self._jobs:
            if cancellable.is_cancelled():
                status = replace(status, description="Cancelling…", fraction=None)
            self._jobs[cancellable] = status
            self.emit("changed")

    def finish(
        self,
        cancellable: Gio.Cancellable,
        error: Exception | None,
        cancelled: bool,
        summary: str,
        failed: bool,
    ) -> None:
        status = self._jobs.pop(cancellable)
        outcome = "cancelled" if cancelled else "failed" if error or failed else "complete"
        self._result = OperationStatus(
            f"{status.title} — {outcome}",
            str(error) if error and not cancelled else summary,
        )
        self.emit("changed")

    def cancel_current(self) -> None:
        """
        Cancel the displayed operation while retaining queued requests.
        """

        cancellable = next(iter(self._jobs), None)
        if cancellable is not None:
            cancellable.cancel()
            self.update(cancellable, replace(self.status, description="Cancelling…"))

    def clear(self) -> None:
        if not self.busy:
            self._result = None
            self.emit("changed")


class OperationReporter:
    """
    Deliver throttled worker progress without changing item-count callbacks.
    """

    def __init__(
        self,
        monitor: OperationMonitor,
        cancellable: Gio.Cancellable,
        title: str,
        callback: Callable[[str, int, int], None] | None,
        destination: Gio.File | None = None,
    ) -> None:
        self.monitor = monitor
        self.cancellable = cancellable
        self.title = title
        self.summary_title = f"{title} → {destination.get_parse_name()}" if destination else title
        self.callback = callback
        self.summary = ""
        self.failed = False
        self._last_update_seconds = 0.0

    def publish(self, status: OperationStatus) -> None:
        now = time.monotonic()
        if now - self._last_update_seconds >= PROGRESS_INTERVAL_SECONDS:
            self._last_update_seconds = now
            GLib.idle_add(_idle_call, self.monitor.update, self.cancellable, status)

    def __call__(self, label: str, completed: int, total: int) -> None:
        self.publish(OperationStatus(
            f"{self.summary_title}: {label}",
            f"{completed} of {total} items processed",
            completed / total if total else None,
        ))
        if self.callback is not None:
            GLib.idle_add(_idle_call, self.callback, label, completed, total)

    def transfer(self, source: Gio.File, target: Gio.File) -> Callable:
        """
        Return a GIO byte callback with an estimate for the current file only.
        """

        title = f"{self.title}: {source.get_basename()} → {target.get_parse_name()}"
        started_seconds = time.monotonic()
        self.publish(OperationStatus(title))

        def progress(completed_bytes: int, total_bytes: int, *_data) -> None:
            elapsed_seconds = time.monotonic() - started_seconds
            description = (
                f"Current file: {GLib.format_size(completed_bytes)}"
                f" of {GLib.format_size(total_bytes)}"
            )
            if elapsed_seconds >= 1 and 0 < completed_bytes < total_bytes:
                rate = completed_bytes / elapsed_seconds
                remaining_seconds = max(1, round((total_bytes - completed_bytes) / rate))
                description += (
                    f" · {GLib.format_size(round(rate))}/s"
                    f" · about {remaining_seconds // 60}m {remaining_seconds % 60}s left"
                )
            fraction = min(1.0, completed_bytes / total_bytes) if total_bytes > 0 else None
            if fraction is not None:
                description = f"{fraction:.0%} · {description}"
            self.publish(OperationStatus(title, description, fraction))

        return progress
