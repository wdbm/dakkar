"""
Run bounded file operations separate from the GTK thread.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import threading

from gi.repository import Gio, GLib

from .services import _idle_call


OperationCallback = Callable[[Exception | None, list[Gio.File]], None]
ProgressCallback = Callable[[str, int, int], None]


@dataclass(frozen=True, slots=True)
class ConflictDecision:
    action: str
    new_name: str | None = None
    apply_all: bool = False


@dataclass(frozen=True, slots=True)
class TransferConflict:
    source_uri: str
    target_uri: str
    name: str
    destination_name: str
    source_is_directory: bool
    target_is_directory: bool
    source_size: int
    target_size: int
    source_modified: int
    target_modified: int
    move: bool

    @property
    def can_merge(self) -> bool:
        return self.source_is_directory and self.target_is_directory


ConflictResponse = Callable[[ConflictDecision], None]
ConflictPrompt = Callable[[TransferConflict, ConflictResponse], None]


@dataclass(slots=True)
class _ConflictSession:
    prompt: ConflictPrompt
    move: bool
    directory_policy: str | None = None
    item_policy: str | None = None
    modification_count: int = 0

    def mark_modified(self) -> None:
        self.modification_count += 1


class _OperationCancelled(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ClipboardState:
    uris: tuple[str, ...] = ()
    cut: bool = False

    @property
    def empty(self) -> bool:
        return not self.uris

    def files(self) -> list[Gio.File]:
        return [Gio.File.new_for_uri(uri) for uri in self.uris]


class OperationService:
    """
    Serialise mutating operations and keep the GTK main loop responsive.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dakkar-operation")
        self._cancellable: Gio.Cancellable | None = None
        self._lock = threading.Lock()

    def cancel(self) -> None:
        with self._lock:
            if self._cancellable is not None:
                self._cancellable.cancel()

    def _submit(
        self,
        operation: Callable[[Gio.Cancellable, ProgressCallback], list[Gio.File]],
        callback: OperationCallback,
        progress: ProgressCallback | None = None,
    ) -> None:
        cancellable = Gio.Cancellable()
        with self._lock:
            self._cancellable = cancellable

        def report(label: str, completed: int, total: int) -> None:
            if progress is not None:
                GLib.idle_add(_idle_call, progress, label, completed, total)

        def run() -> None:
            error: Exception | None = None
            changed: list[Gio.File] = []
            try:
                changed = operation(cancellable, report)
            except Exception as caught:
                # The worker boundary must return unexpected operation failures
                # to the GTK thread rather than terminating silently.
                error = caught
            GLib.idle_add(_idle_call, callback, error, changed)

        self._executor.submit(run)

    def create_directory(
        self,
        parent: Gio.File,
        name: str,
        callback: OperationCallback,
    ) -> None:
        clean_name = name.strip()
        if not clean_name or "/" in clean_name:
            callback(ValueError("A directory name must be non-empty and contain no slash."), [])
            return

        def operation(cancellable, _progress):
            child = parent.get_child(clean_name)
            child.make_directory(cancellable)
            return [child]

        self._submit(operation, callback)

    def create_file(
        self,
        parent: Gio.File,
        name: str,
        callback: OperationCallback,
    ) -> None:
        clean_name = name.strip()
        if not clean_name or "/" in clean_name:
            callback(ValueError("A file name must be non-empty and contain no slash."), [])
            return

        def operation(cancellable, _progress):
            child = parent.get_child(clean_name)
            stream = child.create(Gio.FileCreateFlags.NONE, cancellable)
            stream.close(cancellable)
            return [child]

        self._submit(operation, callback)

    def rename(
        self,
        file: Gio.File,
        new_name: str,
        callback: OperationCallback,
    ) -> None:
        clean_name = new_name.strip()
        if not clean_name or "/" in clean_name:
            callback(ValueError("A name must be non-empty and contain no slash."), [])
            return

        def operation(cancellable, _progress):
            renamed = file.set_display_name(clean_name, cancellable)
            return [renamed]

        self._submit(operation, callback)

    def delete_empty_directory(
        self,
        directory: Gio.File,
        callback: OperationCallback,
    ) -> None:
        """
        Delete a directory only when GIO confirms that it remains empty.
        """

        def operation(cancellable, _progress):
            directory.delete(cancellable)
            return [directory]

        self._submit(operation, callback)

    def delete_empty_file(
        self,
        file: Gio.File,
        callback: OperationCallback,
    ) -> None:
        """
        Delete a regular file only when GIO confirms that it remains empty.
        """

        def operation(cancellable, _progress):
            info = file.query_info(
                f"{Gio.FILE_ATTRIBUTE_STANDARD_TYPE},{Gio.FILE_ATTRIBUTE_STANDARD_SIZE}",
                Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
                cancellable,
            )
            if (
                info.get_file_type() != Gio.FileType.REGULAR
                or info.get_size() != 0
            ):
                raise ValueError("The file is no longer empty and cannot be removed by Undo.")
            file.delete(cancellable)
            return [file]

        self._submit(operation, callback)

    def trash(
        self,
        files: Iterable[Gio.File],
        callback: OperationCallback,
        progress: ProgressCallback | None = None,
    ) -> None:
        file_list = list(files)

        def operation(cancellable, report):
            changed: list[Gio.File] = []
            for index, file in enumerate(file_list, 1):
                file.trash(cancellable)
                changed.append(file)
                report(file.get_basename() or file.get_uri(), index, len(file_list))
            return changed

        self._submit(operation, callback, progress)

    def delete(
        self,
        files: Iterable[Gio.File],
        callback: OperationCallback,
        progress: ProgressCallback | None = None,
    ) -> None:
        file_list = list(files)

        def operation(cancellable, report):
            changed: list[Gio.File] = []
            for index, file in enumerate(file_list, 1):
                self._delete_tree(file, cancellable)
                changed.append(file)
                report(file.get_basename() or file.get_uri(), index, len(file_list))
            return changed

        self._submit(operation, callback, progress)

    def copy_or_move(
        self,
        files: Iterable[Gio.File],
        destination: Gio.File,
        move: bool,
        callback: OperationCallback,
        progress: ProgressCallback | None = None,
        conflict: str = "rename",
        conflict_prompt: ConflictPrompt | None = None,
    ) -> None:
        file_list: list[Gio.File] = []
        for source in files:
            if destination.equal(source) or destination.has_prefix(source):
                callback(
                    ValueError("A directory cannot be copied or moved into itself."),
                    [],
                )
                return
            parent = source.get_parent()
            if move and parent is not None and destination.equal(parent):
                continue
            file_list.append(source)
        if conflict not in {"ask", "rename", "skip", "replace"}:
            callback(ValueError(f"Unsupported conflict policy: {conflict}"), [])
            return
        if conflict == "ask" and conflict_prompt is None:
            callback(ValueError("Interactive conflict handling requires a prompt."), [])
            return
        if not file_list:
            callback(None, [])
            return

        def operation(cancellable, report):
            changed: list[Gio.File] = []
            session = (
                _ConflictSession(conflict_prompt, move)
                if conflict_prompt is not None and conflict == "ask"
                else None
            )
            for index, source in enumerate(file_list, 1):
                basename = source.get_basename()
                if not basename:
                    continue
                target = destination.get_child(basename)
                modifications_before = (
                    session.modification_count if session is not None else 0
                )
                try:
                    if move:
                        result = self._move_tree(
                            source,
                            target,
                            conflict,
                            cancellable,
                            session,
                        )
                    else:
                        result = self._copy_tree(
                            source,
                            target,
                            conflict,
                            cancellable,
                            session,
                        )
                except _OperationCancelled:
                    if (
                        session is not None
                        and session.modification_count > modifications_before
                    ):
                        changed.append(target)
                    break
                if result is not None:
                    changed.append(result)
                    report(basename, index, len(file_list))
            return changed

        self._submit(operation, callback, progress)

    def duplicate(
        self,
        files: Iterable[Gio.File],
        callback: OperationCallback,
        progress: ProgressCallback | None = None,
    ) -> None:
        file_list = list(files)

        def operation(cancellable, report):
            changed: list[Gio.File] = []
            for index, source in enumerate(file_list, 1):
                parent = source.get_parent()
                basename = source.get_basename()
                if parent is None or not basename:
                    continue
                target = self._unique_target(parent.get_child(basename), cancellable)
                self._copy_tree(source, target, "rename", cancellable)
                changed.append(target)
                report(basename, index, len(file_list))
            return changed

        self._submit(operation, callback, progress)

    @staticmethod
    def _exists(file: Gio.File, cancellable: Gio.Cancellable) -> bool:
        return file.query_exists(cancellable)

    def _unique_target(self, original: Gio.File, cancellable: Gio.Cancellable) -> Gio.File:
        if not self._exists(original, cancellable):
            return original
        parent = original.get_parent()
        basename = original.get_basename() or "copy"
        if parent is None:
            raise ValueError("A unique name cannot be made at this location.")
        stem, dot, suffix = basename.rpartition(".")
        if not dot or not stem:
            stem, suffix = basename, ""
        for number in range(1, 10000):
            label = f"{stem} (copy)" if number == 1 else f"{stem} (copy {number})"
            if suffix:
                label = f"{label}.{suffix}"
            candidate = parent.get_child(label)
            if not self._exists(candidate, cancellable):
                return candidate
        raise FileExistsError("No available duplicate filename was found.")

    @staticmethod
    def _item_attributes() -> str:
        return ",".join(
            (
                Gio.FILE_ATTRIBUTE_STANDARD_DISPLAY_NAME,
                Gio.FILE_ATTRIBUTE_STANDARD_TYPE,
                Gio.FILE_ATTRIBUTE_STANDARD_SIZE,
                Gio.FILE_ATTRIBUTE_STANDARD_IS_SYMLINK,
                Gio.FILE_ATTRIBUTE_TIME_MODIFIED,
            )
        )

    def _query_item_info(
        self,
        file: Gio.File,
        cancellable: Gio.Cancellable,
    ) -> Gio.FileInfo:
        return file.query_info(
            self._item_attributes(),
            Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
            cancellable,
        )

    @staticmethod
    def _is_directory(info: Gio.FileInfo) -> bool:
        return (
            info.get_file_type() == Gio.FileType.DIRECTORY
            and not info.get_is_symlink()
        )

    def _transfer_conflict(
        self,
        source: Gio.File,
        target: Gio.File,
        source_info: Gio.FileInfo,
        target_info: Gio.FileInfo,
        move: bool,
    ) -> TransferConflict:
        parent = target.get_parent()
        destination_name = (
            parent.get_basename() if parent is not None else None
        ) or (
            parent.get_parse_name()
            if parent is not None
            else target.get_parse_name()
        )
        return TransferConflict(
            source_uri=source.get_uri(),
            target_uri=target.get_uri(),
            name=(
                source_info.get_display_name()
                or source.get_basename()
                or source.get_parse_name()
            ),
            destination_name=destination_name,
            source_is_directory=self._is_directory(source_info),
            target_is_directory=self._is_directory(target_info),
            source_size=max(0, source_info.get_size()),
            target_size=max(0, target_info.get_size()),
            source_modified=source_info.get_attribute_uint64(
                Gio.FILE_ATTRIBUTE_TIME_MODIFIED
            ),
            target_modified=target_info.get_attribute_uint64(
                Gio.FILE_ATTRIBUTE_TIME_MODIFIED
            ),
            move=move,
        )

    def _ask_conflict(
        self,
        conflict: TransferConflict,
        session: _ConflictSession,
        cancellable: Gio.Cancellable,
    ) -> ConflictDecision:
        remembered = (
            session.directory_policy if conflict.can_merge else session.item_policy
        )
        if remembered is not None:
            return ConflictDecision(remembered)

        answer: list[ConflictDecision] = []
        answered = threading.Event()

        def respond(decision: ConflictDecision) -> None:
            if answered.is_set():
                return
            answer.append(decision)
            answered.set()

        GLib.idle_add(_idle_call, session.prompt, conflict, respond)
        while not answered.wait(0.05):
            if cancellable.is_cancelled():
                raise _OperationCancelled()
        decision = answer[0]
        allowed = (
            {"cancel", "merge", "skip"}
            if conflict.can_merge
            else {"cancel", "replace", "rename", "skip"}
        )
        if decision.action not in allowed:
            raise ValueError(
                f"Unsupported {('directory' if conflict.can_merge else 'item')} "
                f"conflict decision: {decision.action}"
            )
        if decision.action == "cancel":
            raise _OperationCancelled()
        if decision.apply_all and decision.action != "rename":
            if conflict.can_merge:
                session.directory_policy = decision.action
            else:
                session.item_policy = decision.action
        return decision

    def _conflict_decision(
        self,
        source: Gio.File,
        target: Gio.File,
        source_info: Gio.FileInfo,
        target_info: Gio.FileInfo,
        policy: str,
        cancellable: Gio.Cancellable,
        session: _ConflictSession | None,
    ) -> ConflictDecision:
        if policy == "ask":
            if session is None:
                raise ValueError("Interactive conflict handling has no active session.")
            return self._ask_conflict(
                self._transfer_conflict(
                    source,
                    target,
                    source_info,
                    target_info,
                    session.move,
                ),
                session,
                cancellable,
            )
        if policy == "rename":
            return ConflictDecision("rename")
        if policy == "skip":
            return ConflictDecision("skip")
        if self._is_directory(source_info) and self._is_directory(target_info):
            return ConflictDecision("merge")
        return ConflictDecision("replace")

    def _renamed_target(
        self,
        target: Gio.File,
        decision: ConflictDecision,
        cancellable: Gio.Cancellable,
    ) -> Gio.File:
        if decision.new_name is None:
            return self._unique_target(target, cancellable)
        clean_name = decision.new_name.strip()
        if not clean_name or "/" in clean_name:
            raise ValueError(
                "A replacement name must be non-empty and contain no slash."
            )
        parent = target.get_parent()
        if parent is None:
            raise ValueError("The conflicting item has no destination directory.")
        renamed = parent.get_child(clean_name)
        if renamed.equal(target):
            raise ValueError(
                "The replacement name must be different from the existing name."
            )
        return renamed

    @staticmethod
    def _mark_modified(session: _ConflictSession | None) -> None:
        if session is not None:
            session.mark_modified()

    def _copy_tree(
        self,
        source: Gio.File,
        target: Gio.File,
        conflict: str,
        cancellable: Gio.Cancellable,
        session: _ConflictSession | None = None,
    ) -> Gio.File | None:
        source_info = self._query_item_info(source, cancellable)
        source_is_directory = self._is_directory(source_info)
        target_info = (
            self._query_item_info(target, cancellable)
            if target.query_exists(cancellable)
            else None
        )
        if target_info is not None:
            decision = self._conflict_decision(
                source,
                target,
                source_info,
                target_info,
                conflict,
                cancellable,
                session,
            )
            if decision.action == "skip":
                return None
            if decision.action == "rename":
                return self._copy_tree(
                    source,
                    self._renamed_target(target, decision, cancellable),
                    conflict,
                    cancellable,
                    session,
                )
            if decision.action == "replace" and not (
                source_is_directory and self._is_directory(target_info)
            ):
                if not source_is_directory and not self._is_directory(target_info):
                    source.copy(
                        target,
                        Gio.FileCopyFlags.NOFOLLOW_SYMLINKS
                        | Gio.FileCopyFlags.OVERWRITE,
                        cancellable,
                        None,
                        None,
                    )
                    self._mark_modified(session)
                    return target
                self._delete_tree(target, cancellable)
                self._mark_modified(session)
                target_info = None

        if not source_is_directory:
            source.copy(
                target,
                Gio.FileCopyFlags.NOFOLLOW_SYMLINKS,
                cancellable,
                None,
                None,
            )
            self._mark_modified(session)
            return target

        if target_info is None:
            target.make_directory(cancellable)
            self._mark_modified(session)
        enumerator = source.enumerate_children(
            Gio.FILE_ATTRIBUTE_STANDARD_NAME,
            Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
            cancellable,
        )
        try:
            while True:
                child_info = enumerator.next_file(cancellable)
                if child_info is None:
                    break
                name = child_info.get_name()
                if not name:
                    continue
                child_source = enumerator.get_child(child_info)
                child_target = target.get_child(name)
                self._copy_tree(
                    child_source,
                    child_target,
                    conflict,
                    cancellable,
                    session,
                )
        finally:
            enumerator.close(cancellable)
        return target

    def _move_tree(
        self,
        source: Gio.File,
        target: Gio.File,
        conflict: str,
        cancellable: Gio.Cancellable,
        session: _ConflictSession | None = None,
    ) -> Gio.File | None:
        source_info = self._query_item_info(source, cancellable)
        source_is_directory = self._is_directory(source_info)
        target_info = (
            self._query_item_info(target, cancellable)
            if target.query_exists(cancellable)
            else None
        )
        if target_info is not None:
            decision = self._conflict_decision(
                source,
                target,
                source_info,
                target_info,
                conflict,
                cancellable,
                session,
            )
            if decision.action == "skip":
                return None
            if decision.action == "rename":
                return self._move_tree(
                    source,
                    self._renamed_target(target, decision, cancellable),
                    conflict,
                    cancellable,
                    session,
                )
            if decision.action == "merge":
                children: list[tuple[Gio.File, str]] = []
                enumerator = source.enumerate_children(
                    Gio.FILE_ATTRIBUTE_STANDARD_NAME,
                    Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
                    cancellable,
                )
                try:
                    while True:
                        child_info = enumerator.next_file(cancellable)
                        if child_info is None:
                            break
                        name = child_info.get_name()
                        if name:
                            children.append((enumerator.get_child(child_info), name))
                finally:
                    enumerator.close(cancellable)
                modified = False
                for child_source, name in children:
                    result = self._move_tree(
                        child_source,
                        target.get_child(name),
                        conflict,
                        cancellable,
                        session,
                    )
                    modified = result is not None or modified
                try:
                    source.delete(cancellable)
                    self._mark_modified(session)
                    modified = True
                except GLib.Error as error:
                    if not error.matches(
                        Gio.io_error_quark(),
                        Gio.IOErrorEnum.NOT_EMPTY,
                    ):
                        raise
                return target if modified else None
            if source_is_directory != self._is_directory(target_info):
                self._delete_tree(target, cancellable)
                self._mark_modified(session)
                target_info = None

        flags = Gio.FileCopyFlags.NOFOLLOW_SYMLINKS
        if target_info is not None:
            flags |= Gio.FileCopyFlags.OVERWRITE
        try:
            source.move(target, flags, cancellable, None, None)
            self._mark_modified(session)
            return target
        except GLib.Error:
            copied = self._copy_tree(
                source,
                target,
                "replace" if target_info is not None else conflict,
                cancellable,
                session,
            )
            if copied is not None:
                self._delete_tree(source, cancellable)
                self._mark_modified(session)
            return copied

    def _delete_tree(self, file: Gio.File, cancellable: Gio.Cancellable) -> None:
        file_type = file.query_file_type(
            Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
            cancellable,
        )
        if file_type == Gio.FileType.DIRECTORY:
            enumerator = file.enumerate_children(
                Gio.FILE_ATTRIBUTE_STANDARD_NAME,
                Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
                cancellable,
            )
            try:
                while True:
                    info = enumerator.next_file(cancellable)
                    if info is None:
                        break
                    self._delete_tree(enumerator.get_child(info), cancellable)
            finally:
                enumerator.close(cancellable)
        file.delete(cancellable)
