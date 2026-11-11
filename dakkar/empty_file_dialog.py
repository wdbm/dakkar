"""
Review empty files and choose replacement sources before restoring contents.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
import threading
from typing import TYPE_CHECKING

from gi.repository import Gio, GLib, GObject, Gtk, Pango

from .duplicates import ScanIssue
from .empty_files import (
    EmptyFileMatch,
    EmptyFileProgress,
    EmptyFileScanResult,
    RestoreRequest,
    RestoreResult,
    scan_empty_files,
)

if TYPE_CHECKING:
    from .window import DakkarWindow


SCAN_RESPONSE = 1
RESTORE_RESPONSE = 2
STOP_RESPONSE = 3


class EmptyFileRow(GObject.Object):
    """
    Hold an empty destination, explicit source choice and replacement selection.
    """

    selected = GObject.Property(type=bool, default=False)

    def __init__(self, match: EmptyFileMatch) -> None:
        super().__init__()
        self.match = match
        self.source_index = 0 if len(match.sources) == 1 else -1
        self.outcome = ""

    def request(self) -> RestoreRequest | None:
        """
        Return a replacement request only when a source has been chosen.
        """

        if 0 <= self.source_index < len(self.match.sources):
            return RestoreRequest(self.match.destination, self.match.sources[self.source_index])
        return None


class EmptyFileDialog(Gtk.Dialog):
    """
    Own the restoration scope, cancellable scan and explicitly selected replacements.
    """

    def __init__(self, parent: DakkarWindow, root: Path) -> None:
        super().__init__(title="Restore empty files", transient_for=parent, modal=True)
        if parent.has_css_class("dakkar-high-contrast"):
            self.add_css_class("dakkar-high-contrast")
        self.set_default_size(880, 700)
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
        self._source_chooser: Gtk.FileChooserNative | None = None
        self._rows: list[EmptyFileRow] = []
        self._issues: tuple[ScanIssue, ...] = ()

        self.close_button = self.add_button("_Close", Gtk.ResponseType.CLOSE)
        self.stop_button = self.add_button("_Stop", STOP_RESPONSE)
        self.scan_button = self.add_button("_Scan", SCAN_RESPONSE)
        self.restore_button = self.add_button("_Restore selected files", RESTORE_RESPONSE)
        self.stop_button.set_sensitive(False)
        self.set_default_response(SCAN_RESPONSE)

        content = self.get_content_area()
        content.set_spacing(10)
        for side in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{side}")(12)
        content.append(Gtk.Label(
            label=f"Empty-file directory: {root}", xalign=0, wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True,
        ))
        content.append(Gtk.Label(
            label="Non-empty files are sought recursively below the source directory. "
            "Names must match exactly, including case. Searches are independent of view filters.",
            xalign=0, wrap=True,
        ))
        self.scope_controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        source_box = Gtk.Box(spacing=8)
        source_label = Gtk.Label(label="Source _directory:", use_underline=True, xalign=0)
        self.source_directory = Gtk.Entry(text=str(root), hexpand=True)
        self.source_directory.set_placeholder_text("Absolute path to a local source directory")
        source_label.set_mnemonic_widget(self.source_directory)
        self.browse_source_button = Gtk.Button(label="_Browse…", use_underline=True)
        self.browse_source_button.connect("clicked", self._choose_source_directory)
        source_box.append(source_label)
        source_box.append(self.source_directory)
        source_box.append(self.browse_source_button)
        self.scope_controls.append(source_box)
        options = Gtk.Box(spacing=18)
        self.recursive = Gtk.CheckButton(
            label="Include empty-file _subdirectories", use_underline=True, active=True,
        )
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
        self.scope_controls.append(Gtk.Label(
            label="Hidden-file and directory-exclusion settings apply to both searches.",
            xalign=0, wrap=True,
        ))
        content.append(self.scope_controls)
        self.source_directory.connect("changed", self._scope_changed)
        self.recursive.connect("toggled", self._scope_changed)
        self.include_hidden.connect("toggled", self._scope_changed)
        self.exclusions.connect("changed", self._scope_changed)

        progress_box = Gtk.Box(spacing=8)
        self.spinner = Gtk.Spinner()
        self.status = Gtk.Label(label="Select Scan to find empty files.", xalign=0, wrap=True, hexpand=True)
        progress_box.append(self.spinner)
        progress_box.append(self.status)
        content.append(progress_box)

        self.model = Gio.ListStore.new(EmptyFileRow)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_row)
        factory.connect("bind", self._bind_row)
        factory.connect("unbind", self._unbind_row)
        self.list_view = Gtk.ListView(model=Gtk.NoSelection(model=self.model), factory=factory)
        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True, min_content_height=140)
        scroll.set_child(self.list_view)
        content.append(scroll)

        selection_box = Gtk.Box(spacing=8)
        self.select_single_button = Gtk.Button(label="Select files with _one match", use_underline=True)
        self.select_none_button = Gtk.Button(label="_Deselect all", use_underline=True)
        self.select_single_button.connect("clicked", lambda *_: self._select_single_matches())
        self.select_none_button.connect("clicked", lambda *_: self._deselect_all())
        self.selection_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        selection_box.append(self.select_single_button)
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
            label="Empty files may be intentional. Matching names do not establish correct contents. "
            "Sources are retained. Restoration cannot be undone through Dakkar. "
            "Ensure intended destination volumes are mounted before scanning.",
            xalign=0, wrap=True,
        ))
        self.connect("response", self._response)
        self.connect("close-request", self._close_requested)
        self._update_selection()

    def _setup_row(self, _factory, item) -> None:
        box = Gtk.Box(spacing=8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        check = Gtk.CheckButton(valign=Gtk.Align.START)
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        label = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
        source_label = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
        dropdown = Gtk.DropDown(hexpand=True)
        choice_factory = Gtk.SignalListItemFactory()
        choice_factory.connect("setup", lambda _factory, choice: choice.set_child(
            Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=70)
        ))
        choice_factory.connect("bind", lambda _factory, choice: choice.get_child().set_text(
            choice.get_item().get_string()
        ))
        dropdown.set_factory(choice_factory)
        text_box.append(label)
        text_box.append(source_label)
        text_box.append(dropdown)
        box.append(check)
        box.append(text_box)
        item.set_child(box)
        item._widgets = check, label, source_label, dropdown

    def _source_label(self, row: EmptyFileRow, index: int) -> str:
        source = row.match.sources[index]
        try:
            modified = datetime.fromtimestamp(source.modified_ns / 1_000_000_000).astimezone().isoformat(
                sep=" ", timespec="seconds",
            )
        except (OverflowError, OSError, ValueError):
            modified = "unavailable"
        return (
            f"{source.path} — {GLib.format_size(source.size)}; modified {modified}"
        )

    def _bind_row(self, _factory, item) -> None:
        row = item.get_item()
        check, label, source_label, dropdown = item._widgets
        destination = row.match.destination.path.relative_to(self.root)
        label.set_text(f"{row.outcome or 'Empty'}: {destination}")
        check.update_property([Gtk.AccessibleProperty.LABEL], [f"Restore {destination}"])
        ready = self._review_ready and not self._busy
        check.set_sensitive(ready and row.request() is not None)
        item._selection_binding = row.bind_property(
            "selected", check, "active", GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        source_count = len(row.match.sources)
        source_label.set_text(
            f"Source: {self._source_label(row, 0)}" if source_count == 1
            else f"{source_count} matches; select a source." if source_count
            else "No non-empty file with an identical name was found."
        )
        dropdown.set_visible(source_count > 1)
        dropdown.set_sensitive(ready)
        dropdown.update_property([Gtk.AccessibleProperty.LABEL], [f"Replacement source for {destination}"])
        dropdown.set_model(Gtk.StringList.new(
            ["Select source…"] + [self._source_label(row, index) for index in range(source_count)]
            if source_count > 1 else []
        ))
        dropdown.set_selected(row.source_index + 1 if source_count > 1 else Gtk.INVALID_LIST_POSITION)

        def source_changed(control, _property) -> None:
            selected = control.get_selected()
            row.source_index = selected - 1 if selected != Gtk.INVALID_LIST_POSITION else -1
            if row.request() is None:
                row.selected = False
            check.set_sensitive(self._review_ready and not self._busy and row.request() is not None)
            self._update_selection()

        item._source_handler = dropdown.connect("notify::selected", source_changed)

    def _unbind_row(self, _factory, item) -> None:
        item._selection_binding.unbind()
        item._widgets[3].disconnect(item._source_handler)

    def _refresh_rows(self) -> None:
        self.model.splice(0, self.model.get_n_items(), self._rows)

    def _scope_changed(self, *_arguments) -> None:
        self._review_ready = False
        self._refresh_rows()
        self.status.set_text("Scope changed. Select Scan to refresh the review.")
        self._update_selection()

    def _source_root(self) -> Path:
        """
        Read an absolute local source path without querying storage on the GTK thread.
        """

        value = self.source_directory.get_text().strip()
        if not value:
            raise ValueError("Specify a source directory or select Browse.")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("Specify an absolute local source directory or select Browse.")
        return path

    def _choose_source_directory(self, _button) -> None:
        if self._source_chooser is not None:
            self._source_chooser.show()
            return
        chooser = Gtk.FileChooserNative(
            title="Select source directory", transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER, accept_label="Select", cancel_label="Cancel",
        )
        try:
            initial_directory = self._source_root()
        except (ValueError, RuntimeError):
            initial_directory = self.root
        try:
            chooser.set_current_folder(Gio.File.new_for_path(str(initial_directory)))
        except GLib.Error:
            pass

        def response(target, response_id):
            source = target.get_file() if response_id == Gtk.ResponseType.ACCEPT else None
            target.destroy()
            self._source_chooser = None
            if self._closed or source is None:
                return
            path = source.get_path()
            if path is None:
                self.status.set_text("Select a local source directory; remote locations are not supported.")
                return
            self.source_directory.set_text(path)

        chooser.connect("response", response)
        self._source_chooser = chooser
        chooser.show()

    def _select_single_matches(self) -> None:
        self._bulk_selection = True
        for row in self._rows:
            if len(row.match.sources) == 1:
                row.selected = True
        self._bulk_selection = False
        self._update_selection()

    def _deselect_all(self) -> None:
        self._bulk_selection = True
        for row in self._rows:
            row.selected = False
        self._bulk_selection = False
        self._update_selection()

    def _selected_requests(self) -> tuple[RestoreRequest, ...]:
        return tuple(request for row in self._rows if row.selected and (request := row.request()) is not None)

    def _update_selection(self, *_arguments) -> None:
        if self._bulk_selection:
            return
        requests = self._selected_requests()
        size = sum(request.source.size for request in requests)
        self.selection_label.set_text(f"{len(requests)} selected; copy size: {GLib.format_size(size)}")
        ready = self._review_ready and not self._busy
        self.scan_button.set_sensitive(not self._busy and bool(self.source_directory.get_text().strip()))
        self.restore_button.set_sensitive(ready and bool(requests))
        self.select_single_button.set_sensitive(ready and any(len(row.match.sources) == 1 for row in self._rows))
        self.select_none_button.set_sensitive(ready and any(row.selected for row in self._rows))

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
        elif response == RESTORE_RESPONSE and self._review_ready and not self._busy:
            self._restore_selected()
        elif response == STOP_RESPONSE:
            self._stop()
        elif response in (Gtk.ResponseType.CLOSE, Gtk.ResponseType.DELETE_EVENT, Gtk.ResponseType.CANCEL):
            self.close_review()

    def _start_scan(self) -> None:
        try:
            source_root = self._source_root()
        except (ValueError, RuntimeError) as error:
            self.status.set_text(str(error))
            return
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

        def report(progress: EmptyFileProgress) -> None:
            GLib.idle_add(self._scan_progress, generation, progress)

        def run() -> None:
            try:
                result = scan_empty_files(
                    self.root, source_root, recursive, include_hidden, patterns, cancelled.is_set, report,
                )
            except Exception as error:
                result = EmptyFileScanResult((), (ScanIssue(self.root, str(error)),), EmptyFileProgress(0, 0, self.root))
            GLib.idle_add(self._scan_done, generation, result)

        threading.Thread(target=run, name="dakkar-empty-file-scan", daemon=True).start()

    def _scan_progress(self, generation: int, progress: EmptyFileProgress) -> bool:
        if not self._closed and generation == self._generation:
            self.status.set_text(
                f"Scanning: {progress.directory_count} directories; {progress.file_count} files inspected.\n"
                f"{progress.current_path}"
            )
        return GLib.SOURCE_REMOVE

    def _scan_done(self, generation: int, result: EmptyFileScanResult) -> bool:
        if self._closed or generation != self._generation:
            return GLib.SOURCE_REMOVE
        self._issues = result.issues
        for match in result.matches:
            row = EmptyFileRow(match)
            row.connect("notify::selected", self._update_selection)
            self._rows.append(row)
        cancelled = result.cancelled or self._scan_cancelled.is_set()
        self._review_ready = not cancelled
        self._set_busy(False)
        prefix = "Scan cancelled; partial results cannot be restored." if cancelled else "Scan complete."
        if result.issues and not cancelled:
            prefix = "Scan incomplete; some items could not be inspected."
        matched_count = sum(bool(match.sources) for match in result.matches)
        self.status.set_text(
            f"{prefix} {len(result.matches)} empty files; {matched_count} with matching sources; "
            f"{len(result.issues)} scan issues."
        )
        self._set_details("\n".join(f"{issue.path}: {issue.reason}" for issue in result.issues))
        return GLib.SOURCE_REMOVE

    def _restore_selected(self) -> None:
        requests = self._selected_requests()
        if not requests:
            return
        self._set_busy(True)
        self.status.set_text("Revalidating selected files and staging replacements…")
        self._operation_cancellable = self.owner.operations.restore_empty_files(
            requests, self._restore_done, self._restore_progress,
        )

    def _restore_progress(self, label: str, completed: int, total: int) -> None:
        if not self._closed:
            self.status.set_text(f"Restoring files: {completed} of {total}. {label}")

    def _restore_done(self, result: RestoreResult) -> None:
        # Completed work must update views even after the review has been closed.
        self.owner.empty_files_restored(result)
        self._operation_cancellable = None
        if self._closed:
            return
        self._review_ready = False
        outcomes = {outcome.request.destination.path: outcome for outcome in result.outcomes}
        for row in self._rows:
            if outcome := outcomes.get(row.match.destination.path):
                row.outcome = outcome.status.capitalize()
                row.selected = False
        self._set_busy(False)
        counts = Counter(outcome.status for outcome in result.outcomes)
        self.status.set_text(
            f"Restored: {counts['restored']}; skipped: {counts['skipped']}; failed: {counts['failed']}. "
            "Select Scan to prepare another review."
        )
        details = [f"{issue.path}: {issue.reason}" for issue in self._issues]
        details.extend(
            f"{outcome.request.destination.path}: {outcome.status}. {outcome.reason}"
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
        self.status.set_text("Stopping; completed replacements will be retained…")

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
        if self._source_chooser is not None:
            self._source_chooser.destroy()
            self._source_chooser = None
        self._scan_cancelled.set()
        if self._operation_cancellable is not None:
            self._operation_cancellable.cancel()
        self.owner._empty_file_dialogs.discard(self)
        self.destroy()
