"""
Load and monitor directories asynchronously.
"""

from __future__ import annotations

from collections.abc import Callable
import threading

from gi.repository import Gio, GLib

from .models import FILE_ATTRIBUTES, ItemData


BatchCallback = Callable[[int, list[ItemData]], None]
DoneCallback = Callable[[int, Exception | None], None]
DirectoryMetadata = tuple[str, int | None, int, int]
DirectoryMetadataCallback = Callable[[int, list[DirectoryMetadata]], None]
SelectionSizeCallback = Callable[[int, int, bool], None]


def _idle_call(callback: Callable, *arguments) -> bool:
    callback(*arguments)
    return GLib.SOURCE_REMOVE


class DirectoryLoader:
    """
    Enumerate one GIO directory away from the GTK main thread.
    """

    def __init__(
        self,
        initial_batch_size: int = 24,
        batch_size: int = 192,
    ) -> None:
        self._initial_batch_size = max(1, initial_batch_size)
        self._batch_size = max(1, batch_size)
        self._lock = threading.Lock()
        self._generation = 0
        self._cancellable: Gio.Cancellable | None = None

    def cancel(self) -> None:
        with self._lock:
            if self._cancellable is not None:
                self._cancellable.cancel()

    def load(
        self,
        location: Gio.File,
        include_hidden: bool,
        on_batch: BatchCallback,
        on_done: DoneCallback,
    ) -> int:
        self.cancel()
        with self._lock:
            self._generation += 1
            generation = self._generation
            cancellable = Gio.Cancellable()
            self._cancellable = cancellable

        thread = threading.Thread(
            target=self._run,
            args=(
                generation,
                location,
                include_hidden,
                cancellable,
                on_batch,
                on_done,
            ),
            name=f"dakkar-directory-{generation}",
            daemon=True,
        )
        thread.start()
        return generation

    def _run(
        self,
        generation: int,
        location: Gio.File,
        include_hidden: bool,
        cancellable: Gio.Cancellable,
        on_batch: BatchCallback,
        on_done: DoneCallback,
    ) -> None:
        batch: list[ItemData] = []
        batch_size = self._initial_batch_size
        error: Exception | None = None
        enumerator: Gio.FileEnumerator | None = None
        try:
            enumerator = location.enumerate_children(
                FILE_ATTRIBUTES,
                Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
                cancellable,
            )
            while not cancellable.is_cancelled():
                info = enumerator.next_file(cancellable)
                if info is None:
                    break
                if not include_hidden and (info.get_is_hidden() or info.get_is_backup()):
                    continue
                child = enumerator.get_child(info)
                batch.append(ItemData.from_info(child, info))
                if len(batch) >= batch_size:
                    GLib.idle_add(_idle_call, on_batch, generation, batch)
                    batch = []
                    batch_size = self._batch_size
            if batch:
                GLib.idle_add(_idle_call, on_batch, generation, batch)
        except GLib.Error as caught:
            if not cancellable.is_cancelled():
                error = caught
        except Exception as caught:
            # The worker boundary must return unexpected conversion failures
            # to the GTK thread rather than terminating silently.
            if not cancellable.is_cancelled():
                error = caught
        finally:
            if enumerator is not None:
                try:
                    enumerator.close(cancellable)
                except GLib.Error:
                    pass
            GLib.idle_add(_idle_call, on_done, generation, error)


class DirectoryMetadataLoader:
    """
    Inspect directory counts, modification dates and logical sizes.
    """

    _attributes = ",".join(
        (
            Gio.FILE_ATTRIBUTE_STANDARD_NAME,
            Gio.FILE_ATTRIBUTE_STANDARD_TYPE,
            Gio.FILE_ATTRIBUTE_STANDARD_IS_SYMLINK,
            Gio.FILE_ATTRIBUTE_STANDARD_SIZE,
            Gio.FILE_ATTRIBUTE_TIME_MODIFIED,
        )
    )

    def __init__(self, batch_size: int = 24) -> None:
        self._batch_size = batch_size
        self._lock = threading.Lock()
        self._generation = 0
        self._cancellable: Gio.Cancellable | None = None

    def cancel(self) -> None:
        with self._lock:
            if self._cancellable is not None:
                self._cancellable.cancel()

    def load(
        self,
        directories: list[ItemData],
        inspect_recursively: bool,
        on_metadata: DirectoryMetadataCallback,
        on_done: DoneCallback,
    ) -> int:
        self.cancel()
        with self._lock:
            self._generation += 1
            generation = self._generation
            cancellable = Gio.Cancellable()
            self._cancellable = cancellable
        threading.Thread(
            target=self._run,
            args=(
                generation,
                directories,
                inspect_recursively,
                cancellable,
                on_metadata,
                on_done,
            ),
            name=f"dakkar-directory-metadata-{generation}",
            daemon=True,
        ).start()
        return generation

    def _run(
        self,
        generation: int,
        directories: list[ItemData],
        inspect_recursively: bool,
        cancellable: Gio.Cancellable,
        on_metadata: DirectoryMetadataCallback,
        on_done: DoneCallback,
    ) -> None:
        error: Exception | None = None
        batch: list[DirectoryMetadata] = []
        try:
            for directory in directories:
                if cancellable.is_cancelled():
                    break
                count, modified, size = self.inspect_directory(
                    Gio.File.new_for_uri(directory.uri),
                    directory.modified,
                    inspect_recursively,
                    cancellable,
                )
                batch.append((directory.uri, count, modified, size))
                if len(batch) >= self._batch_size:
                    GLib.idle_add(
                        _idle_call,
                        on_metadata,
                        generation,
                        batch,
                    )
                    batch = []
        except Exception as caught:
            # Recursive inspection runs at a worker boundary, where an error
            # must be returned through the completion callback.
            if not cancellable.is_cancelled():
                error = caught
        finally:
            if batch:
                GLib.idle_add(
                    _idle_call,
                    on_metadata,
                    generation,
                    batch,
                )
            GLib.idle_add(_idle_call, on_done, generation, error)

    @classmethod
    def inspect_directory(
        cls,
        directory: Gio.File,
        initial_modified: int,
        inspect_recursively: bool,
        cancellable: Gio.Cancellable,
    ) -> tuple[int | None, int, int]:
        direct_count = 0
        maximum_modified = initial_modified
        total_size = 0
        directories: list[tuple[Gio.File, bool]] = [(directory, True)]
        while directories and not cancellable.is_cancelled():
            directory, is_root = directories.pop()
            enumerator: Gio.FileEnumerator | None = None
            try:
                enumerator = directory.enumerate_children(
                    cls._attributes,
                    Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
                    cancellable,
                )
                while not cancellable.is_cancelled():
                    info = enumerator.next_file(cancellable)
                    if info is None:
                        break
                    if is_root:
                        direct_count += 1
                    if not inspect_recursively:
                        continue
                    maximum_modified = max(
                        maximum_modified,
                        info.get_attribute_uint64(Gio.FILE_ATTRIBUTE_TIME_MODIFIED),
                    )
                    is_directory = (
                        inspect_recursively
                        and info.get_file_type() == Gio.FileType.DIRECTORY
                        and not info.get_is_symlink()
                    )
                    if is_directory:
                        directories.append((enumerator.get_child(info), False))
                    elif info.get_file_type() != Gio.FileType.DIRECTORY:
                        total_size += max(0, info.get_size())
            except GLib.Error as caught:
                if caught.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    break
                if is_root:
                    return None, maximum_modified, total_size
            finally:
                if enumerator is not None:
                    try:
                        enumerator.close(cancellable)
                    except GLib.Error:
                        pass
            if not inspect_recursively:
                break
        return direct_count, maximum_modified, total_size


class SelectionSizeLoader:
    """
    Calculate the logical size of selected items away from the GTK thread.
    """

    _attributes = ",".join(
        (
            Gio.FILE_ATTRIBUTE_STANDARD_NAME,
            Gio.FILE_ATTRIBUTE_STANDARD_TYPE,
            Gio.FILE_ATTRIBUTE_STANDARD_SIZE,
            Gio.FILE_ATTRIBUTE_STANDARD_IS_SYMLINK,
        )
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._cancellable: Gio.Cancellable | None = None

    def cancel(self) -> None:
        with self._lock:
            if self._cancellable is not None:
                self._cancellable.cancel()

    def load(
        self,
        items: list[ItemData],
        on_done: SelectionSizeCallback,
    ) -> int:
        self.cancel()
        with self._lock:
            self._generation += 1
            generation = self._generation
            cancellable = Gio.Cancellable()
            self._cancellable = cancellable
        threading.Thread(
            target=self._run,
            args=(generation, items, cancellable, on_done),
            name=f"dakkar-selection-size-{generation}",
            daemon=True,
        ).start()
        return generation

    def _run(
        self,
        generation: int,
        items: list[ItemData],
        cancellable: Gio.Cancellable,
        on_done: SelectionSizeCallback,
    ) -> None:
        total = 0
        incomplete = False
        try:
            total, incomplete = self.calculate(items, cancellable)
        except Exception:
            # Selection size is supplementary; retain a useful partial status
            # when a provider reports an unexpected failure.
            incomplete = True
        if not cancellable.is_cancelled():
            GLib.idle_add(
                _idle_call,
                on_done,
                generation,
                total,
                incomplete,
            )

    @classmethod
    def calculate(
        cls,
        items: list[ItemData],
        cancellable: Gio.Cancellable,
    ) -> tuple[int, bool]:
        total = 0
        incomplete = False
        directories: list[Gio.File] = []
        selected_uris: set[str] = set()
        for item in items:
            if item.uri in selected_uris:
                continue
            selected_uris.add(item.uri)
            if item.is_directory and not item.is_symlink:
                directories.append(Gio.File.new_for_uri(item.uri))
            else:
                total += item.size

        while directories and not cancellable.is_cancelled():
            directory = directories.pop()
            enumerator: Gio.FileEnumerator | None = None
            try:
                enumerator = directory.enumerate_children(
                    cls._attributes,
                    Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
                    cancellable,
                )
                while not cancellable.is_cancelled():
                    info = enumerator.next_file(cancellable)
                    if info is None:
                        break
                    if (
                        info.get_file_type() == Gio.FileType.DIRECTORY
                        and not info.get_is_symlink()
                    ):
                        directories.append(enumerator.get_child(info))
                    else:
                        total += max(0, info.get_size())
            except GLib.Error as error:
                if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    break
                incomplete = True
            finally:
                if enumerator is not None:
                    try:
                        enumerator.close(cancellable)
                    except GLib.Error as error:
                        if not error.matches(
                            Gio.io_error_quark(),
                            Gio.IOErrorEnum.CANCELLED,
                        ):
                            incomplete = True
        return total, incomplete
