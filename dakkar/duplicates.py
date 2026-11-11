"""
Find identical copies with recognised filename suffixes at local directories.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from fnmatch import fnmatchcase
import hashlib
import os
from pathlib import Path
import re
import stat
import time


CHUNK_SIZE = 1024 * 1024
PROGRESS_INTERVAL_SECONDS = 0.1
COPY_PATTERN = re.compile(
    r"^(?P<stem>.+?) *\((?:(?P<number>[1-9][0-9]{0,2})|"
    r"copy(?: (?P<copy_number>[1-9][0-9]{0,3}))?)\)"
    r"(?P<suffix>(?:\.[^.]+)*)$"
)
FileVersion = tuple[int, ...]
ComparisonMetadata = tuple[int, int, int, int]
ExtendedAttributes = tuple[tuple[str, bytes], ...]
ScanMember = tuple[tuple[int, ...], Path, FileVersion]


class ScanCancelled(Exception):
    """
    Stop inspection after cancellation has been requested.
    """


class FileChangedError(OSError):
    """
    Report a file which no longer matches the reviewed version.
    """


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: Path
    version: FileVersion
    metadata: ComparisonMetadata
    attributes: ExtendedAttributes
    digest: bytes

    @property
    def size(self) -> int:
        return self.metadata[0]


@dataclass(frozen=True, slots=True)
class DuplicateCopy:
    keeper: FileSnapshot
    candidate: FileSnapshot


@dataclass(frozen=True, slots=True)
class ScanIssue:
    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class ScanProgress:
    directory_count: int
    file_count: int
    comparison_count: int
    current_path: Path


@dataclass(frozen=True, slots=True)
class ScanResult:
    copies: tuple[DuplicateCopy, ...]
    issues: tuple[ScanIssue, ...]
    progress: ScanProgress
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class TrashOutcome:
    copy: DuplicateCopy
    status: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class DuplicateTrashResult:
    outcomes: tuple[TrashOutcome, ...]
    error: str = ""


def original_name(filename: str) -> tuple[str, tuple[int, ...]]:
    """
    Strip repeated numbered or Dakkar copy suffixes, preserving extensions.
    """

    numbers = []
    while match := COPY_PATTERN.fullmatch(filename):
        numbers.append(int(match.group("number") or match.group("copy_number") or 1))
        filename = match.group("stem") + match.group("suffix")
    return filename, tuple(reversed(numbers))


def file_version(info: os.stat_result) -> FileVersion:
    """
    Identify a file version without treating reads as changes.
    """

    return (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
        info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid, info.st_nlink,
    )


def comparison_metadata(info: os.stat_result) -> ComparisonMetadata:
    """
    Return the basic metadata which copies must share.
    """

    return info.st_size, stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid


def is_trash_path(path: Path) -> bool:
    """
    Recognise home and volume Trash storage, including descendants.
    """

    path = Path(os.path.abspath(path))
    data_directory = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    home_trash = Path(os.path.abspath(data_directory / "Trash"))
    if path == home_trash or home_trash in path.parents:
        return True
    return any(
        part == ".Trash" or re.fullmatch(r"\.Trash-[0-9]+", part)
        for part in path.parts
    )


@contextmanager
def open_directory(path: Path) -> Iterator[int]:
    """
    Open an absolute directory without following links at any path component.
    """

    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("An absolute directory path without '..' is required.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def open_regular_file(path: Path) -> Iterator[int]:
    """
    Open a singly linked regular file without following links or blocking on FIFOs.
    """

    with open_directory(path.parent) as parent:
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent,
        )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise FileChangedError(f"Not a singly linked regular file: {path}")
        yield descriptor
    finally:
        os.close(descriptor)


def extended_attributes(descriptor: int) -> ExtendedAttributes:
    """
    Read attributes from the opened file, including ACLs exposed as attributes.
    """

    return tuple(
        (name, os.getxattr(descriptor, name))
        for name in sorted(os.listxattr(descriptor))
    )


def check_cancelled(cancelled: Callable[[], bool]) -> None:
    """
    Interrupt traversal or content reads when cancellation is requested.
    """

    if cancelled():
        raise ScanCancelled()


def validate_version(path: Path, descriptor: int, expected: FileVersion) -> None:
    """
    Check both the open file and its current directory entry against a snapshot.
    """

    with open_directory(path.parent) as parent:
        current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
    if file_version(os.fstat(descriptor)) != expected or file_version(current) != expected:
        raise FileChangedError(f"File changed during inspection: {path}")


def fingerprint(
    path: Path,
    expected: FileVersion,
    cancelled: Callable[[], bool],
    pulse: Callable[[], None],
) -> FileSnapshot:
    """
    Read a stable snapshot and digest once per scan; reject concurrent changes.
    """

    with open_regular_file(path) as descriptor:
        info = os.fstat(descriptor)
        if file_version(info) != expected:
            raise FileChangedError(f"File changed before inspection: {path}")
        attributes = extended_attributes(descriptor)
        digest = hashlib.sha256()
        while True:
            check_cancelled(cancelled)
            pulse()
            chunk = os.read(descriptor, CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        validate_version(path, descriptor, expected)
        return FileSnapshot(path, expected, comparison_metadata(info), attributes, digest.digest())


def identical(
    first: FileSnapshot,
    second: FileSnapshot,
    cancelled: Callable[[], bool],
    pulse: Callable[[], None] = lambda: None,
) -> bool:
    """
    Compare reviewed files byte for byte and reject changed versions or metadata.
    """

    if first.metadata != second.metadata or first.attributes != second.attributes:
        return False
    if first.version[:2] == second.version[:2]:
        return False
    with open_regular_file(first.path) as left, open_regular_file(second.path) as right:
        for snapshot, descriptor in ((first, left), (second, right)):
            validate_version(snapshot.path, descriptor, snapshot.version)
            if extended_attributes(descriptor) != snapshot.attributes:
                raise FileChangedError(f"File attributes changed: {snapshot.path}")
        while True:
            check_cancelled(cancelled)
            pulse()
            left_chunk = os.read(left, CHUNK_SIZE)
            right_chunk = os.read(right, CHUNK_SIZE)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                break
        validate_version(first.path, left, first.version)
        validate_version(second.path, right, second.version)
    return True


def scan_duplicates(
    root: Path,
    recursive: bool = False,
    include_hidden: bool = False,
    excluded_patterns: tuple[str, ...] = (),
    cancelled: Callable[[], bool] = lambda: False,
    on_progress: Callable[[ScanProgress], None] = lambda _progress: None,
) -> ScanResult:
    """
    Find same-directory copies; report incomplete inspection without discarding results.

    Hidden names and backup names ending in '~' are excluded by default. Exclusion
    patterns match directory basenames or paths relative to the scan root. Trash,
    symbolic links, special files and multiply linked files are always excluded.
    """

    root = Path(os.path.abspath(root))
    copies: list[DuplicateCopy] = []
    issues: list[ScanIssue] = []
    directory_count = file_count = comparison_count = 0
    current_path = root
    last_report = 0.0

    def progress() -> ScanProgress:
        return ScanProgress(directory_count, file_count, comparison_count, current_path)

    def pulse() -> None:
        nonlocal last_report
        now = time.monotonic()
        if now - last_report >= PROGRESS_INTERVAL_SECONDS:
            last_report = now
            on_progress(progress())

    if is_trash_path(root):
        return ScanResult((), (ScanIssue(root, "Trash storage cannot be scanned."),), progress())
    directories = [root]
    try:
        while directories:
            check_cancelled(cancelled)
            directory = directories.pop()
            current_path = directory
            directory_count += 1
            pulse()
            groups: dict[tuple[str, ComparisonMetadata], list[ScanMember]] = defaultdict(list)
            children: list[Path] = []
            try:
                with open_directory(directory) as descriptor, os.scandir(descriptor) as entries:
                    for entry in entries:
                        check_cancelled(cancelled)
                        path = directory / entry.name
                        if not include_hidden and (entry.name.startswith(".") or entry.name.endswith("~")):
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode):
                                if recursive and not is_trash_path(path) and not any(
                                    fnmatchcase(entry.name, pattern)
                                    or fnmatchcase(path.relative_to(root).as_posix(), pattern)
                                    for pattern in excluded_patterns
                                ):
                                    children.append(path)
                            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                                canonical, numbers = original_name(entry.name)
                                groups[canonical, comparison_metadata(info)].append(
                                    (numbers, path, file_version(info))
                                )
                        except OSError as error:
                            issues.append(ScanIssue(path, str(error)))
                        pulse()
            except OSError as error:
                issues.append(ScanIssue(directory, str(error)))
                continue
            directories.extend(sorted(children, reverse=True))
            for members in groups.values():
                check_cancelled(cancelled)
                if len(members) < 2 or not any(numbers for numbers, _, _ in members):
                    continue
                keepers: dict[tuple[bytes, ExtendedAttributes], list[FileSnapshot]] = defaultdict(list)
                for _, path, version in sorted(members, key=lambda member: (member[0], member[1].name)):
                    current_path = path
                    file_count += 1
                    try:
                        snapshot = fingerprint(path, version, cancelled, pulse)
                    except OSError as error:
                        issues.append(ScanIssue(path, str(error)))
                        continue
                    matching = keepers[snapshot.digest, snapshot.attributes]
                    keeper = None
                    for possible in tuple(matching):
                        comparison_count += 1
                        try:
                            if identical(possible, snapshot, cancelled, pulse):
                                keeper = possible
                                break
                        except OSError as error:
                            issues.append(ScanIssue(path, str(error)))
                            # Other retained files can still be checked if one has disappeared.
                            continue
                    if keeper is None:
                        matching.append(snapshot)
                    else:
                        copies.append(DuplicateCopy(keeper, snapshot))
    except ScanCancelled:
        return ScanResult(tuple(copies), tuple(issues), progress(), cancelled=True)
    on_progress(progress())
    return ScanResult(tuple(copies), tuple(issues), progress())
