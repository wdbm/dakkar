"""
Provide asynchronous child models for the expandable-list view.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import threading

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from .models import FileItem, ItemData, query_item_data
from .services import DirectoryLoader, DirectoryMetadataLoader


BRANCH_MONITOR_UPDATE_DELAY_MS = 150


@dataclass
class _DirectoryBranch:
    parent: FileItem
    store: Gio.ListStore
    sorted_model: Gtk.SortListModel
    loader: DirectoryLoader
    metadata_loader: DirectoryMetadataLoader
    generation: int = 0
    metadata_generation: int = 0
    monitor: Gio.FileMonitor | None = None
    monitor_timeout: int = 0
    monitor_serial: int = 0
    pending_queries: dict[str, Gio.File] = field(default_factory=dict)
    pending_removals: set[str] = field(default_factory=set)
    loading: bool = False
    loaded: bool = False
    metadata_recursive: bool = False
    items_by_uri: dict[str, FileItem] = field(default_factory=dict)


class ExpandableDirectoryModel:
    """
    Supply lazily loaded child models to a `Gtk.TreeListModel`.

    A separate loader, metadata worker and directory monitor are retained for
    each expanded directory. This allows several branches to remain active at
    once without one expansion cancelling another.
    """

    def __init__(
        self,
        root_model: Gio.ListModel,
        sorter: Gtk.Sorter,
        include_hidden: Callable[[], bool],
        matches_filter: Callable[[ItemData], bool],
        inspect_recursively: Callable[[], bool],
        on_items_changed: Callable[[], None],
    ) -> None:
        self._sorter = sorter
        self._include_hidden = include_hidden
        self._matches_filter = matches_filter
        self._inspect_recursively = inspect_recursively
        self._on_items_changed = on_items_changed
        self._branches: dict[str, _DirectoryBranch] = {}
        self._restore_uris: set[str] = set()
        self.model = Gtk.TreeListModel.new(
            root_model,
            False,
            False,
            self._create_child_model,
        )

    def _create_child_model(self, item: object, *_arguments) -> Gio.ListModel | None:
        if (
            not isinstance(item, FileItem)
            or not item.is_directory
            or item.data.is_symlink
        ):
            return None
        branch = self._branches.get(item.uri)
        if branch is None:
            store = Gio.ListStore.new(FileItem)
            sorted_model = Gtk.SortListModel(
                model=store,
                sorter=self._sorter,
            )
            sorted_model.set_incremental(True)
            branch = _DirectoryBranch(
                parent=item,
                store=store,
                sorted_model=sorted_model,
                loader=DirectoryLoader(),
                metadata_loader=DirectoryMetadataLoader(),
            )
            self._branches[item.uri] = branch
        else:
            branch.parent = item
        return branch.sorted_model

    @staticmethod
    def _dispose_branch(branch: _DirectoryBranch) -> None:
        branch.loader.cancel()
        branch.metadata_loader.cancel()
        if branch.monitor_timeout:
            GLib.source_remove(branch.monitor_timeout)
        if branch.monitor is not None:
            branch.monitor.cancel()

    def forget_uri(self, uri: str) -> None:
        """
        Stop and remove branches rooted at a removed directory URI.
        """

        prefix = f"{uri.rstrip('/')}/"
        for branch_uri in tuple(self._branches):
            if branch_uri != uri and not branch_uri.startswith(prefix):
                continue
            self._dispose_branch(self._branches.pop(branch_uri))
            self._restore_uris.discard(branch_uri)

    def ensure_loaded(self, item: FileItem) -> None:
        if not item.is_directory or item.data.is_symlink:
            return
        branch = self._branches.get(item.uri)
        if branch is None:
            self._create_child_model(item)
            branch = self._branches.get(item.uri)
        if branch is None or branch.loading or branch.loaded:
            return
        self._load_branch(branch)
        self._monitor_branch(branch)

    def _load_branch(self, branch: _DirectoryBranch) -> None:
        branch.monitor_serial += 1
        branch.pending_queries.clear()
        branch.pending_removals.clear()
        branch.loader.cancel()
        branch.metadata_loader.cancel()
        branch.metadata_generation = -1
        branch.store.remove_all()
        branch.items_by_uri.clear()
        branch.loading = True
        branch.loaded = False
        branch.generation = branch.loader.load(
            branch.parent.file,
            self._include_hidden(),
            lambda generation, batch, uri=branch.parent.uri: self._branch_batch(
                uri,
                generation,
                batch,
            ),
            lambda generation, error, uri=branch.parent.uri: self._branch_done(
                uri,
                generation,
                error,
            ),
        )

    def _branch_batch(
        self,
        uri: str,
        generation: int,
        batch: list[ItemData],
    ) -> None:
        branch = self._branches.get(uri)
        if branch is None or generation != branch.generation:
            return
        items = [
            FileItem(data)
            for data in batch
            if (
                data.uri not in branch.items_by_uri
                and self._matches_filter(data)
            )
        ]
        if items:
            branch.store.splice(branch.store.get_n_items(), 0, items)
            branch.items_by_uri.update(
                (item.uri, item)
                for item in items
            )
            self._on_items_changed()
            self.restore_pending_expansions()

    def _branch_done(
        self,
        uri: str,
        generation: int,
        error: Exception | None,
    ) -> None:
        branch = self._branches.get(uri)
        if branch is None or generation != branch.generation:
            return
        branch.loading = False
        branch.loaded = error is None
        branch.parent.set_directory_metadata(
            None if error is not None else branch.store.get_n_items(),
            branch.parent.deep_modified,
            branch.parent.deep_size,
        )
        self._on_items_changed()
        if error is None:
            self._load_branch_metadata(branch)
        self.restore_pending_expansions()

    def _load_branch_metadata(
        self,
        branch: _DirectoryBranch,
        only_missing_counts: bool = False,
    ) -> None:
        branch.metadata_loader.cancel()
        branch.metadata_generation = -1
        inspect_recursively = self._inspect_recursively()
        branch.metadata_recursive = inspect_recursively
        directories = [
            item.data
            for position in range(branch.store.get_n_items())
            if (
                isinstance(item := branch.store.get_item(position), FileItem)
                and item.is_directory
                and not item.data.is_symlink
                and (
                    not only_missing_counts
                    or item.directory_count is None
                )
            )
        ]
        if not directories:
            return
        branch.metadata_generation = branch.metadata_loader.load(
            directories,
            inspect_recursively,
            lambda generation, metadata, uri=branch.parent.uri: self._metadata_ready(
                uri,
                generation,
                metadata,
            ),
            lambda generation, error, uri=branch.parent.uri: self._metadata_done(
                uri,
                generation,
                error,
            ),
        )

    def _metadata_ready(
        self,
        branch_uri: str,
        generation: int,
        metadata: list[tuple[str, int | None, int, int]],
    ) -> None:
        branch = self._branches.get(branch_uri)
        if branch is None or generation != branch.metadata_generation:
            return
        expanded_uris = self.expanded_uris()
        restore_uris: set[str] = set()
        changed = False
        for item_uri, count, modified, size in metadata:
            item = branch.items_by_uri.get(item_uri)
            if item is None:
                continue
            item.set_directory_metadata(count, modified, size)
            changed = True
            if item_uri in expanded_uris:
                restore_uris.add(item_uri)
        if not changed:
            return
        if branch.metadata_recursive:
            self._sorter.changed(Gtk.SorterChange.DIFFERENT)
        if restore_uris:
            self.queue_expansion_restore(restore_uris)
        self._on_items_changed()

    def _metadata_done(
        self,
        branch_uri: str,
        generation: int,
        _error: Exception | None,
    ) -> None:
        branch = self._branches.get(branch_uri)
        if branch is not None and generation == branch.metadata_generation:
            branch.metadata_generation = -1
            if branch.metadata_recursive:
                self._sorter.changed(Gtk.SorterChange.DIFFERENT)

    def _monitor_branch(self, branch: _DirectoryBranch) -> None:
        if branch.monitor is not None:
            return
        try:
            branch.monitor = branch.parent.file.monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES,
                None,
            )
            branch.monitor.connect(
                "changed",
                lambda _monitor, file, other_file, event, uri=branch.parent.uri: (
                    self._branch_changed(
                        uri,
                        file,
                        other_file,
                        event,
                    )
                ),
            )
        except GLib.Error:
            branch.monitor = None

    def files_changed(self, files: Iterable[Gio.File]) -> None:
        """
        Requery operation results at any loaded branch, even without a monitor.
        """

        for file in files:
            parent = file.get_parent()
            if parent is not None:
                self._branch_changed(
                    parent.get_uri(), file, None, Gio.FileMonitorEvent.CHANGES_DONE_HINT,
                )

    def _branch_changed(
        self,
        uri: str,
        file: Gio.File,
        other_file: Gio.File | None,
        event: Gio.FileMonitorEvent,
    ) -> None:
        branch = self._branches.get(uri)
        if branch is None:
            return
        if event in {
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.MOVED_OUT,
        }:
            item_uri = file.get_uri()
            branch.pending_queries.pop(item_uri, None)
            branch.pending_removals.add(item_uri)
        elif event in {
            Gio.FileMonitorEvent.MOVED,
            Gio.FileMonitorEvent.RENAMED,
        }:
            item_uri = file.get_uri()
            branch.pending_queries.pop(item_uri, None)
            branch.pending_removals.add(item_uri)
            if other_file is not None:
                other_uri = other_file.get_uri()
                branch.pending_removals.discard(other_uri)
                branch.pending_queries[other_uri] = other_file
        elif event in {
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.CHANGED,
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.ATTRIBUTE_CHANGED,
            Gio.FileMonitorEvent.MOVED_IN,
        }:
            item_uri = file.get_uri()
            branch.pending_removals.discard(item_uri)
            branch.pending_queries[item_uri] = file
        else:
            return
        if branch.monitor_timeout:
            GLib.source_remove(branch.monitor_timeout)
        branch.monitor_timeout = GLib.timeout_add(
            BRANCH_MONITOR_UPDATE_DELAY_MS,
            self._flush_branch_updates,
            uri,
        )

    def _flush_branch_updates(self, uri: str) -> bool:
        branch = self._branches.get(uri)
        if branch is None:
            return GLib.SOURCE_REMOVE
        branch.monitor_timeout = 0
        serial = branch.monitor_serial
        files = list(branch.pending_queries.values())
        removals = set(branch.pending_removals)
        branch.pending_queries.clear()
        branch.pending_removals.clear()
        if removals:
            self._apply_branch_updates(uri, serial, [], removals)
        if not files:
            return GLib.SOURCE_REMOVE

        def query() -> None:
            updates: list[ItemData] = []
            missing: set[str] = set()
            for file in files:
                try:
                    updates.append(query_item_data(file))
                except GLib.Error as error:
                    if error.matches(
                        Gio.io_error_quark(),
                        Gio.IOErrorEnum.NOT_FOUND,
                    ):
                        missing.add(file.get_uri())
            GLib.idle_add(
                self._branch_updates_ready,
                uri,
                serial,
                updates,
                missing,
            )

        threading.Thread(
            target=query,
            name="dakkar-branch-monitor-update",
            daemon=True,
        ).start()
        return GLib.SOURCE_REMOVE

    def _branch_updates_ready(
        self,
        uri: str,
        serial: int,
        updates: list[ItemData],
        removals: set[str],
    ) -> bool:
        self._apply_branch_updates(uri, serial, updates, removals)
        return GLib.SOURCE_REMOVE

    def _apply_branch_updates(
        self,
        uri: str,
        serial: int,
        updates: list[ItemData],
        removals: set[str],
    ) -> None:
        branch = self._branches.get(uri)
        if branch is None or serial != branch.monitor_serial:
            return
        changed = False
        for item_uri in removals:
            item = branch.items_by_uri.pop(item_uri, None)
            if item is None:
                continue
            found, position = branch.store.find(item)
            if found:
                branch.store.remove(position)
            self.forget_uri(item_uri)
            changed = True
        for data in updates:
            current = branch.items_by_uri.get(data.uri)
            visible = (
                (self._include_hidden() or not data.is_hidden)
                and self._matches_filter(data)
            )
            if not visible:
                if current is not None:
                    found, position = branch.store.find(current)
                    if found:
                        branch.store.remove(position)
                    branch.items_by_uri.pop(data.uri, None)
                    self.forget_uri(data.uri)
                    changed = True
                continue
            replacement = FileItem(data)
            if current is not None and current.is_directory and data.is_directory:
                replacement.directory_count = current.directory_count
                replacement.deep_modified = current.deep_modified
                replacement.deep_size = current.deep_size
            if current is None:
                branch.store.append(replacement)
            else:
                found, position = branch.store.find(current)
                if found:
                    branch.store.splice(position, 1, [replacement])
                else:
                    branch.store.append(replacement)
            branch.items_by_uri[data.uri] = replacement
            changed = True
        if not changed:
            return
        branch.parent.set_directory_metadata(
            branch.store.get_n_items(),
            branch.parent.deep_modified,
            branch.parent.deep_size,
        )
        self._load_branch_metadata(branch)
        self._on_items_changed()
        self.restore_pending_expansions()

    def expanded_uris(self) -> set[str]:
        uris: set[str] = set()
        for position in range(self.model.get_n_items()):
            row = self.model.get_item(position)
            if not isinstance(row, Gtk.TreeListRow) or not row.get_expanded():
                continue
            item = row.get_item()
            if isinstance(item, FileItem):
                uris.add(item.uri)
        return uris

    def reset(self, restore_uris: Iterable[str] = ()) -> None:
        for branch in self._branches.values():
            self._dispose_branch(branch)
        self._branches.clear()
        self._restore_uris = set(restore_uris)

    def queue_expansion_restore(self, uris: Iterable[str]) -> None:
        self._restore_uris.update(uris)
        self.restore_pending_expansions()

    def restore_pending_expansions(self) -> None:
        if not self._restore_uris:
            return
        restored: set[str] = set()
        for position in range(self.model.get_n_items()):
            row = self.model.get_item(position)
            if not isinstance(row, Gtk.TreeListRow):
                continue
            item = row.get_item()
            if isinstance(item, FileItem) and item.uri in self._restore_uris:
                row.set_expanded(True)
                restored.add(item.uri)
        self._restore_uris.difference_update(restored)

    def refresh_metadata(self) -> None:
        inspect_recursively = self._inspect_recursively()
        for branch in self._branches.values():
            if branch.metadata_recursive == inspect_recursively:
                continue
            self._load_branch_metadata(
                branch,
                only_missing_counts=not inspect_recursively,
            )

    def visible_child_items(self) -> list[FileItem]:
        items: list[FileItem] = []
        for position in range(self.model.get_n_items()):
            row = self.model.get_item(position)
            if not isinstance(row, Gtk.TreeListRow) or row.get_depth() == 0:
                continue
            item = row.get_item()
            if isinstance(item, FileItem):
                items.append(item)
        return items

    def dispose(self) -> None:
        self.reset()
