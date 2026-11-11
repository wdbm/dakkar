"""
Find empty files and restore explicitly chosen namesakes through staged copies.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
import os
from pathlib import Path
import stat
import time
import uuid

from .duplicates import (
    CHUNK_SIZE,
    PROGRESS_INTERVAL_SECONDS,
    FileChangedError,
    FileVersion,
    ScanCancelled,
    ScanIssue,
    check_cancelled,
    file_version,
    is_trash_path,
    open_directory,
    open_regular_file,
    validate_version,
)


@dataclass(frozen=True, slots=True)
class EmptyFileSnapshot:
    path: Path
    version: FileVersion
    parent_identity: tuple[int, int]
    size: int
    modified_ns: int


@dataclass(frozen=True, slots=True)
class EmptyFileMatch:
    destination: EmptyFileSnapshot
    sources: tuple[EmptyFileSnapshot, ...]


@dataclass(frozen=True, slots=True)
class RestoreRequest:
    destination: EmptyFileSnapshot
    source: EmptyFileSnapshot


@dataclass(frozen=True, slots=True)
class EmptyFileProgress:
    directory_count: int
    file_count: int
    current_path: Path


@dataclass(frozen=True, slots=True)
class EmptyFileScanResult:
    matches: tuple[EmptyFileMatch, ...]
    issues: tuple[ScanIssue, ...]
    progress: EmptyFileProgress
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class RestoreOutcome:
    request: RestoreRequest
    status: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RestoreResult:
    outcomes: tuple[RestoreOutcome, ...]
    error: str = ""


def directory_identity(descriptor: int) -> tuple[int, int]:
    """
    Identify the opened directory and its filesystem without using mutable times.
    """

    info = os.fstat(descriptor)
    return info.st_dev, info.st_ino


def scan_empty_files(
    root: Path,
    source_root: Path,
    recursive: bool = True,
    include_hidden: bool = False,
    excluded_patterns: tuple[str, ...] = (),
    cancelled: Callable[[], bool] = lambda: False,
    on_progress: Callable[[EmptyFileProgress], None] = lambda _progress: None,
) -> EmptyFileScanResult:
    """
    Find empty files below `root` and exact namesakes below `source_root`.

    Source searching always includes subdirectories. `recursive` controls only
    destination searching. Hidden-file and exclusion settings apply to both roots;
    relative exclusion patterns are interpreted against each respective root.
    Trash, symbolic links, special files and multiply linked files are excluded.
    Hidden names include dot-prefixed names and backup names ending in '~'.
    Inaccessible paths are reported; cancelled results cannot be applied.
    """

    root = Path(os.path.abspath(root))
    source_root = Path(os.path.abspath(source_root))
    empty_files: list[EmptyFileSnapshot] = []
    empty_paths: set[Path] = set()
    empty_names: set[str] = set()
    sources: dict[str, list[EmptyFileSnapshot]] = defaultdict(list)
    issues: list[ScanIssue] = []
    directory_count = file_count = 0
    current_path = root
    last_report = 0.0

    def progress() -> EmptyFileProgress:
        return EmptyFileProgress(directory_count, file_count, current_path)

    def pulse() -> None:
        nonlocal last_report
        now = time.monotonic()
        if now - last_report >= PROGRESS_INTERVAL_SECONDS:
            last_report = now
            on_progress(progress())

    def scan_scope(scan_root: Path, include_subdirectories: bool, collect_sources: bool) -> None:
        nonlocal current_path, directory_count, file_count
        if is_trash_path(scan_root):
            issues.append(ScanIssue(scan_root, "Trash storage cannot be scanned."))
            return
        directories = [scan_root]
        visited: set[tuple[int, int]] = set()
        while directories:
            check_cancelled(cancelled)
            directory = directories.pop()
            current_path = directory
            children: list[Path] = []
            try:
                with open_directory(directory) as descriptor, os.scandir(descriptor) as entries:
                    parent_identity = directory_identity(descriptor)
                    if parent_identity in visited:
                        continue
                    visited.add(parent_identity)
                    directory_count += 1
                    pulse()
                    for entry in entries:
                        check_cancelled(cancelled)
                        path = directory / entry.name
                        if not include_hidden and (entry.name.startswith(".") or entry.name.endswith("~")):
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode):
                                if include_subdirectories and not is_trash_path(path) and not any(
                                    fnmatchcase(entry.name, pattern)
                                    or fnmatchcase(path.relative_to(scan_root).as_posix(), pattern)
                                    for pattern in excluded_patterns
                                ):
                                    children.append(path)
                            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                                file_count += 1
                                snapshot = EmptyFileSnapshot(
                                    path, file_version(info), parent_identity, info.st_size, info.st_mtime_ns,
                                )
                                if info.st_size == 0 and not collect_sources:
                                    empty_files.append(snapshot)
                                    empty_paths.add(path)
                                    empty_names.add(entry.name)
                                elif (
                                    info.st_size > 0 and collect_sources
                                    and entry.name in empty_names and path not in empty_paths
                                ):
                                    # A destination changed between the two searches
                                    # must not become a source for another destination.
                                    sources[entry.name].append(snapshot)
                        except OSError as error:
                            issues.append(ScanIssue(path, str(error)))
                        pulse()
            except OSError as error:
                issues.append(ScanIssue(directory, str(error)))
            directories.extend(sorted(children, reverse=True))

    was_cancelled = False
    try:
        scan_scope(root, recursive, False)
        scan_scope(source_root, True, True)
    except ScanCancelled:
        was_cancelled = True
    on_progress(progress())
    matched_sources = {
        name: tuple(sorted(sources.get(name, ()), key=lambda item: item.path))
        for name in {item.path.name for item in empty_files}
    }
    matches = tuple(
        EmptyFileMatch(destination, matched_sources[destination.path.name])
        for destination in sorted(empty_files, key=lambda item: item.path)
    )
    return EmptyFileScanResult(matches, tuple(issues), progress(), was_cancelled or cancelled())


def validate_parent(snapshot: EmptyFileSnapshot, descriptor: int) -> None:
    """
    Reject replacement directories and changed mounts before writing or committing.
    """

    if directory_identity(descriptor) != snapshot.parent_identity:
        raise FileChangedError(f"Directory or destination filesystem changed: {snapshot.path.parent}")
    with open_directory(snapshot.path.parent) as current:
        if directory_identity(current) != snapshot.parent_identity:
            raise FileChangedError(f"Directory or destination filesystem changed: {snapshot.path.parent}")


def restore_empty_file(
    request: RestoreRequest,
    cancelled: Callable[[], bool] = lambda: False,
    on_progress: Callable[[int, int], None] = lambda _completed, _total: None,
) -> None:
    """
    Stage and flush a complete copy, then atomically replace the reviewed empty file.

    Both files and directory identities are revalidated before committing. Sources
    retain their contents; permissions and timestamps are copied. A failed or
    cancelled copy removes its temporary file and leaves the destination intact.
    Concurrent writes in the final validation/rename interval cannot be excluded.
    """

    destination, source = request.destination, request.source
    if destination.path.name != source.path.name or destination.size != 0 or source.size <= 0:
        raise ValueError("An empty destination and a non-empty source with identical names are required.")
    if is_trash_path(destination.path) or is_trash_path(source.path):
        raise ValueError("Trash storage cannot be used for restoration.")
    check_cancelled(cancelled)
    with open_directory(destination.path.parent) as parent, open_regular_file(source.path) as source_file:
        validate_parent(destination, parent)
        with open_directory(source.path.parent) as source_parent:
            validate_parent(source, source_parent)
        validate_version(source.path, source_file, source.version)
        with open_regular_file(destination.path) as destination_file:
            validate_version(destination.path, destination_file, destination.version)
            source_info = os.fstat(source_file)
            if os.fstat(destination_file).st_size != 0 or source_info.st_size <= 0:
                raise FileChangedError("The destination must remain empty and the source non-empty.")

            # Create relative to the pinned parent so a disappearing mount cannot
            # redirect the temporary copy to the underlying directory.
            while True:
                tag = str(uuid.uuid4())[:8]
                if not any(character.isalpha() for character in tag):
                    continue
                temporary_name = f".dakkar-restore-{tag}.tmp"
                try:
                    temporary_file = os.open(
                        temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                        0o600, dir_fd=parent,
                    )
                    break
                except FileExistsError:
                    check_cancelled(cancelled)
            try:
                copied_bytes = 0
                last_report = 0.0
                with os.fdopen(temporary_file, "wb") as staged:
                    while True:
                        check_cancelled(cancelled)
                        chunk = os.read(source_file, CHUNK_SIZE)
                        if not chunk:
                            break
                        staged.write(chunk)
                        copied_bytes += len(chunk)
                        if copied_bytes > source.size:
                            raise FileChangedError(f"Source grew during copying: {source.path}")
                        now = time.monotonic()
                        if now - last_report >= PROGRESS_INTERVAL_SECONDS:
                            last_report = now
                            on_progress(copied_bytes, source.size)
                    staged.flush()
                    if copied_bytes != source.size or os.fstat(staged.fileno()).st_size != source.size:
                        raise OSError(f"Incomplete copy of source: {source.path}")
                    os.fchmod(staged.fileno(), stat.S_IMODE(source_info.st_mode))
                    os.utime(staged.fileno(), ns=(source_info.st_atime_ns, source_info.st_mtime_ns))
                    os.fsync(staged.fileno())
                validate_parent(destination, parent)
                with open_directory(source.path.parent) as source_parent:
                    validate_parent(source, source_parent)
                validate_version(source.path, source_file, source.version)
                validate_version(destination.path, destination_file, destination.version)
                check_cancelled(cancelled)
                os.replace(temporary_name, destination.path.name, src_dir_fd=parent, dst_dir_fd=parent)
                temporary_name = None
            finally:
                if temporary_name is not None:
                    try:
                        os.unlink(temporary_name, dir_fd=parent)
                    except FileNotFoundError:
                        pass
