"""
Review local duplicate copies before moving explicitly selected files to Trash.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import threading
from typing import TYPE_CHECKING

from gi.repository import Gio, GLib, GObject, Gtk

from .duplicates import (
    DuplicateCopy,
    DuplicateTrashResult,
    ScanIssue,
    ScanProgress,
    ScanResult,
    scan_duplicates,
)

if TYPE_CHECKING:
    from .window import DakkarWindow


SCAN_RESPONSE = 1
TRASH_RESPONSE = 2
STOP_RESPONSE = 3


class DuplicateRow(GObject.Object):
    """
    Hold a retained-file heading or a selectable copy for a virtualised list.
    """

    selected = GObject.Property(type=bool, default=False)

    def __init__(self, label: str, copy: DuplicateCopy | None = None) -> None:
        super().__init__()
        self.label = label
        self.copy = copy
        self.selected = copy is not None


class DuplicateDialog(Gtk.Dialog):
    """
    Own scan cancellation, a fixed directory scope and reviewed copy selections.
    """

    def __init__(self, parent: DakkarWindow, root: Path) -> None:
        super().__init__(title="Find duplicate copies", transient_for=parent, modal=True)
        if parent.has_css_class("dakkar-high-contrast"):
            self.add_css_class("dakkar-high-contrast")
        self.set_default_size(820, 600)
        self.set_size_request(480, 360)
        self.owner = parent
        self.root = root
        self._closed = False
        self._busy = False
        self._review_ready = False
        self._bulk_selection = False
        self._generation = 0
        self._scan_cancelled = threading.Event()
        self._operation_cancellable: Gio.Cancellable | None = None
        self._rows: list[DuplicateRow] = []
        self._issues: tuple[ScanIssue, ...] = ()

        self.close_button = self.add_button("_Close", Gtk.ResponseType.CLOSE)
        self.stop_button = self.add_button("_Stop", STOP_RESPONSE)
        self.scan_button = self.add_button("_Scan", SCAN_RESPONSE)
        self.trash_button = self.add_button("Move selected copies to _Trash", TRASH_RESPONSE)
        self.stop_button.set_sensitive(False)
        self.trash_button.set_sensitive(False)
        self.set_default_response(SCAN_RESPONSE)

        content = self.get_content_area()
        content.set_spacing(10)
        for side in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{side}")(12)
        location = Gtk.Label(label=f"Directory: {root}", xalign=0, wrap=True, selectable=True)
        content.append(location)
        content.append(Gtk.Label(
            label="Directory contents are scanned independently of filename searches and view filters.",
            xalign=0, wrap=True,
        ))
        self.scope_controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        options = Gtk.Box(spacing=18)
        self.recursive = Gtk.CheckButton(label="Include _subdirectories", use_underline=True)
        self.include_hidden = Gtk.CheckButton(label="Include _hidden files", use_underline=True)
        options.append(self.recursive)
        options.append(self.include_hidden)
        self.scope_controls.append(options)
        exclusions = Gtk.Box(spacing=8)
        exclusion_label = Gtk.Label(label="_Exclude directories:", use_underline=True, xalign=0)
        self.exclusions = Gtk.Entry(hexpand=True)
        self.exclusions.set_placeholder_text("Comma-separated names or relative patterns, e.g. .git, build, cache/*")
        exclusion_label.set_mnemonic_widget(self.exclusions)
        exclusions.append(exclusion_label)
        exclusions.append(self.exclusions)
        self.scope_controls.append(exclusions)
        content.append(self.scope_controls)
        self.recursive.connect("toggled", self._scope_changed)
        self.include_hidden.connect("toggled", self._scope_changed)
        self.exclusions.connect("changed", self._scope_changed)

        progress_box = Gtk.Box(spacing=8)
        self.spinner = Gtk.Spinner()
        self.status = Gtk.Label(label="Select Scan to find identical copies.", xalign=0, wrap=True, hexpand=True)
        progress_box.append(self.spinner)
        progress_box.append(self.status)
        content.append(progress_box)

        self.model = Gio.ListStore.new(DuplicateRow)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_row)
        factory.connect("bind", self._bind_row)
        factory.connect("unbind", self._unbind_row)
        self.list_view = Gtk.ListView(model=Gtk.NoSelection(model=self.model), factory=factory)
        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True, min_content_height=140)
        scroll.set_child(self.list_view)
        content.append(scroll)

        selection_box = Gtk.Box(spacing=8)
        self.select_all_button = Gtk.Button(label="Select _all copies", use_underline=True)
        self.select_none_button = Gtk.Button(label="_Deselect all", use_underline=True)
        self.select_all_button.connect("clicked", lambda *_: self._select_all(True))
        self.select_none_button.connect("clicked", lambda *_: self._select_all(False))
        self.selection_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        selection_box.append(self.select_all_button)
        selection_box.append(self.select_none_button)
        selection_box.append(self.selection_label)
        content.append(selection_box)

        self.details = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        details_scroll = Gtk.ScrolledWindow(min_content_height=90)
        details_scroll.set_child(self.details)
        self.details_expander = Gtk.Expander(label="Scan and operation details")
        self.details_expander.set_child(details_scroll)
        self.details_expander.set_visible(False)
        content.append(self.details_expander)
        content.append(Gtk.Label(
            label=(
                "Retained files are never selected for removal. Moving files to Trash does not normally "
                "free storage. This operation cannot be undone through Dakkar; recovery is through the system Trash."
            ),
            xalign=0, wrap=True,
        ))
        self.connect("response", self._response)
        self.connect("close-request", self._close_requested)
        self._update_selection()

    def _setup_row(self, _factory, item) -> None:
        box = Gtk.Box(spacing=8)
        box.set_margin_top(5)
        box.set_margin_bottom(5)
        check = Gtk.CheckButton(valign=Gtk.Align.START)
        label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        box.append(check)
        box.append(label)
        item.set_child(box)

    def _bind_row(self, _factory, item) -> None:
        row = item.get_item()
        box = item.get_child()
        check = box.get_first_child()
        label = check.get_next_sibling()
        label.set_text(row.label)
        check.set_visible(row.copy is not None)
        check.set_sensitive(self._review_ready and not self._busy)
        check.update_property([Gtk.AccessibleProperty.LABEL], [row.label])
        item._selection_binding = row.bind_property(
            "selected", check, "active",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        if row.copy is None:
            label.add_css_class("heading")
        else:
            label.remove_css_class("heading")

    def _unbind_row(self, _factory, item) -> None:
        binding = getattr(item, "_selection_binding", None)
        if binding is not None:
            binding.unbind()
            item._selection_binding = None

    def _refresh_rows(self) -> None:
        self.model.splice(0, self.model.get_n_items(), self._rows)

    def _scope_changed(self, *_arguments) -> None:
        self._review_ready = False
        self._refresh_rows()
        self.status.set_text("Scope changed. Select Scan to refresh the review.")
        self._update_selection()

    def _select_all(self, selected: bool) -> None:
        self._bulk_selection = True
        for row in self._rows:
            if row.copy is not None:
                row.selected = selected
        self._bulk_selection = False
        self._update_selection()

    def _update_selection(self, *_arguments) -> None:
        if self._bulk_selection:
            return
        selected = [row.copy for row in self._rows if row.copy is not None and row.selected]
        size = sum(copy.candidate.size for copy in selected)
        self.selection_label.set_text(
            f"{len(selected)} selected; selected file size: {GLib.format_size(size)}"
        )
        ready = self._review_ready and not self._busy
        self.trash_button.set_sensitive(ready and bool(selected))
        self.select_all_button.set_sensitive(ready and bool(self._rows))
        self.select_none_button.set_sensitive(ready and bool(self._rows))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.scope_controls.set_sensitive(not busy)
        self.scan_button.set_sensitive(not busy)
        self.stop_button.set_sensitive(busy)
        self.close_button.set_label("Cancel and _close" if busy else "_Close")
        self.spinner.set_spinning(busy)
        self._refresh_rows()
        self._update_selection()

    def _response(self, _dialog, response: int) -> None:
        if response == SCAN_RESPONSE and not self._busy:
            self._start_scan()
        elif response == TRASH_RESPONSE and self._review_ready and not self._busy:
            self._move_selected()
        elif response == STOP_RESPONSE:
            self._stop()
        elif response in (Gtk.ResponseType.CLOSE, Gtk.ResponseType.DELETE_EVENT, Gtk.ResponseType.CANCEL):
            self.close_review()

    def _start_scan(self) -> None:
        self._generation += 1
        generation = self._generation
        self._scan_cancelled = threading.Event()
        cancelled = self._scan_cancelled
        recursive = self.recursive.get_active()
        include_hidden = self.include_hidden.get_active()
        patterns = tuple(value.strip() for value in self.exclusions.get_text().split(",") if value.strip())
        self._review_ready = False
        self._rows = []
        self._issues = ()
        self._set_details("")
        self._set_busy(True)
        self.status.set_text("Scanning directory contents…")

        def report(progress: ScanProgress) -> None:
            GLib.idle_add(self._scan_progress, generation, progress)

        def run() -> None:
            try:
                result = scan_duplicates(
                    self.root, recursive, include_hidden, patterns,
                    cancelled.is_set, report,
                )
            except Exception as error:
                result = ScanResult(
                    (), (ScanIssue(self.root, str(error)),), ScanProgress(0, 0, 0, self.root),
                )
            GLib.idle_add(self._scan_done, generation, result)

        threading.Thread(target=run, name="dakkar-duplicate-scan", daemon=True).start()

    def _scan_progress(self, generation: int, progress: ScanProgress) -> bool:
        if not self._closed and generation == self._generation:
            self.status.set_text(
                f"Scanning: {progress.directory_count} directories; {progress.file_count} files inspected; "
                f"{progress.comparison_count} comparisons.\n{progress.current_path}"
            )
        return GLib.SOURCE_REMOVE

    def _scan_done(self, generation: int, result: ScanResult) -> bool:
        if self._closed or generation != self._generation:
            return GLib.SOURCE_REMOVE
        self._issues = result.issues
        grouped: dict[Path, list[DuplicateCopy]] = {}
        for copy in result.copies:
            grouped.setdefault(copy.keeper.path, []).append(copy)
        for keeper, copies in sorted(grouped.items()):
            relative = keeper.relative_to(self.root)
            self._rows.append(DuplicateRow(
                f"Keep: {relative} — {GLib.format_size(copies[0].keeper.size)}"
            ))
            for copy in copies:
                row = DuplicateRow(
                    f"{copy.candidate.path.relative_to(self.root)} — {GLib.format_size(copy.candidate.size)}",
                    copy,
                )
                row.connect("notify::selected", self._update_selection)
                self._rows.append(row)
        self._review_ready = not result.cancelled
        self._set_busy(False)
        prefix = "Scan cancelled; partial results cannot be moved." if result.cancelled else "Scan complete."
        if result.issues and not result.cancelled:
            prefix = "Scan incomplete; some items could not be inspected."
        self.status.set_text(
            f"{prefix} {len(result.copies)} duplicate copies; {len(result.issues)} scan issues. "
            f"{result.progress.directory_count} directories; {result.progress.file_count} files inspected; "
            f"{result.progress.comparison_count} comparisons."
        )
        self._set_details("\n".join(f"{issue.path}: {issue.reason}" for issue in result.issues))
        return GLib.SOURCE_REMOVE

    def _move_selected(self) -> None:
        copies = tuple(row.copy for row in self._rows if row.copy is not None and row.selected)
        if not copies:
            return
        self._set_busy(True)
        self.status.set_text("Revalidating selected copies before moving them to Trash…")
        self._operation_cancellable = self.owner.operations.trash_duplicates(
            copies, self._move_done, self._move_progress,
        )

    def _move_progress(self, label: str, completed: int, total: int) -> None:
        if not self._closed:
            self.status.set_text(f"Moving reviewed copies: {completed} of {total}. {label}")

    def _move_done(self, result: DuplicateTrashResult) -> None:
        # Window state must be updated even if the review was closed during a move.
        self.owner.duplicate_copies_done(result)
        self._operation_cancellable = None
        if self._closed:
            return
        self._review_ready = False
        outcomes = {outcome.copy.candidate.path: outcome for outcome in result.outcomes}
        for row in self._rows:
            if row.copy is not None and row.copy.candidate.path in outcomes:
                outcome = outcomes[row.copy.candidate.path]
                row.label = f"{outcome.status.capitalize()}: {row.label}"
                row.selected = False
        self._set_busy(False)
        counts = Counter(outcome.status for outcome in result.outcomes)
        self.status.set_text(
            f"Moved: {counts['moved']}; skipped: {counts['skipped']}; failed: {counts['failed']}. "
            "Select Scan to prepare another review."
        )
        details = [f"{issue.path}: {issue.reason}" for issue in self._issues]
        details.extend(
            f"{outcome.copy.candidate.path}: {outcome.status}. {outcome.reason}"
            for outcome in result.outcomes
        )
        if result.error:
            details.append(result.error)
        self._set_details("\n".join(details))

    def _set_details(self, text: str) -> None:
        self.details.get_buffer().set_text(text)
        self.details_expander.set_visible(bool(text))

    def _stop(self) -> None:
        self._scan_cancelled.set()
        if self._operation_cancellable is not None:
            self._operation_cancellable.cancel()
        self.stop_button.set_sensitive(False)
        self.status.set_text("Stopping; completed moves will be retained in the result summary…")

    def _close_requested(self, _dialog) -> bool:
        self.close_review()
        return True

    def close_review(self) -> None:
        """
        Cancel outstanding work and ignore callbacks targeting destroyed widgets.
        """

        if self._closed:
            return
        self._closed = True
        self._scan_cancelled.set()
        if self._operation_cancellable is not None:
            self._operation_cancellable.cancel()
        self.owner._duplicate_dialogs.discard(self)
        self.destroy()
