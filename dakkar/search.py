"""
Provide cancellable filename-only search.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import threading
import unicodedata

from gi.repository import Gio, GLib

from .models import FILE_ATTRIBUTES, ItemData
from .services import _idle_call


def normalise_text(value: str) -> str:
    """
    Return a case-insensitive, accent-insensitive matching form.
    """

    decomposed = unicodedata.normalize("NFKD", value).casefold()
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def query_words(query: str) -> tuple[str, ...]:
    return tuple(word for word in normalise_text(query).split() if word)


def score_filename(filename: str, query: str) -> float | None:
    """
    Rank a filename, or return `None` when every query word is not present.
    """

    candidate = normalise_text(filename)
    words = query_words(query)
    if not words:
        return None
    positions = [candidate.find(word) for word in words]
    if any(position < 0 for position in positions):
        return None
    joined = " ".join(words)
    if candidate == joined:
        return 1000.0
    score = 500.0 if candidate.startswith(joined) else 0.0
    for word, position in zip(words, positions):
        if position == 0 or candidate[position - 1] in " _-.()[]{}":
            score += 120.0
        score += max(1.0, 80.0 - float(position))
        score += min(40.0, float(len(word)) * 2.0)
    score -= max(0.0, float(len(candidate) - len(joined))) / 20.0
    return score


def matches_filters(
    data: ItemData,
    mime_group: str = "all",
    date_field: str = "modified",
    after_timestamp: int | None = None,
) -> bool:
    """
    Apply broad MIME and timestamp filters without opening file contents.
    """

    if mime_group == "directories":
        if not data.is_directory:
            return False
    elif data.is_directory and mime_group != "all":
        return False
    elif mime_group == "images" and not data.content_type.startswith("image/"):
        return False
    elif mime_group == "audio" and not data.content_type.startswith("audio/"):
        return False
    elif mime_group == "video" and not data.content_type.startswith("video/"):
        return False
    elif mime_group == "documents":
        document_type = (
            data.content_type.startswith("text/")
            or data.content_type == "application/pdf"
            or "document" in data.content_type
            or "spreadsheet" in data.content_type
            or "presentation" in data.content_type
        )
        if not document_type:
            return False
    if after_timestamp is not None:
        timestamp = {
            "modified": data.modified,
            "accessed": data.accessed,
            "created": data.created,
        }.get(date_field, data.modified)
        if not timestamp or timestamp < after_timestamp:
            return False
    return True


@dataclass(frozen=True, slots=True)
class SearchResult:
    data: ItemData
    score: float


SearchBatchCallback = Callable[[int, list[SearchResult]], None]
SearchDoneCallback = Callable[[int, Exception | None], None]


class FilenameSearch:
    """
    Recursively search GIO locations without reading file contents.
    """

    def __init__(self, batch_size: int = 64) -> None:
        self._batch_size = batch_size
        self._lock = threading.Lock()
        self._generation = 0
        self._cancellable: Gio.Cancellable | None = None

    def cancel(self) -> None:
        with self._lock:
            if self._cancellable is not None:
                self._cancellable.cancel()

    def start(
        self,
        location: Gio.File,
        query: str,
        include_hidden: bool,
        recursive: bool,
        on_batch: SearchBatchCallback,
        on_done: SearchDoneCallback,
        immediate_items: Iterable[ItemData] = (),
        mime_group: str = "all",
        date_field: str = "modified",
        after_timestamp: int | None = None,
    ) -> int:
        self.cancel()
        with self._lock:
            self._generation += 1
            generation = self._generation
            cancellable = Gio.Cancellable()
            self._cancellable = cancellable

        immediate: list[SearchResult] = []
        for data in immediate_items:
            score = score_filename(data.display_name, query)
            if score is not None and matches_filters(
                data,
                mime_group,
                date_field,
                after_timestamp,
            ):
                immediate.append(SearchResult(data, score))
        if immediate:
            on_batch(generation, immediate)

        thread = threading.Thread(
            target=self._run,
            args=(
                generation,
                location,
                query,
                include_hidden,
                recursive,
                cancellable,
                on_batch,
                on_done,
                mime_group,
                date_field,
                after_timestamp,
            ),
            name=f"dakkar-search-{generation}",
            daemon=True,
        )
        thread.start()
        return generation

    def _run(
        self,
        generation: int,
        root: Gio.File,
        query: str,
        include_hidden: bool,
        recursive: bool,
        cancellable: Gio.Cancellable,
        on_batch: SearchBatchCallback,
        on_done: SearchDoneCallback,
        mime_group: str,
        date_field: str,
        after_timestamp: int | None,
    ) -> None:
        error: Exception | None = None
        batch: list[SearchResult] = []
        directories = [root]
        try:
            while directories and not cancellable.is_cancelled():
                directory = directories.pop()
                enumerator: Gio.FileEnumerator | None = None
                try:
                    enumerator = directory.enumerate_children(
                        FILE_ATTRIBUTES,
                        Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
                        cancellable,
                    )
                    while not cancellable.is_cancelled():
                        info = enumerator.next_file(cancellable)
                        if info is None:
                            break
                        hidden = info.get_is_hidden() or info.get_is_backup()
                        child = enumerator.get_child(info)
                        if recursive and info.get_file_type() == Gio.FileType.DIRECTORY and not info.get_is_symlink():
                            if include_hidden or not hidden:
                                directories.append(child)
                        if hidden and not include_hidden:
                            continue
                        display_name = info.get_display_name() or info.get_name() or ""
                        score = score_filename(display_name, query)
                        data = ItemData.from_info(child, info)
                        if score is not None and matches_filters(
                            data,
                            mime_group,
                            date_field,
                            after_timestamp,
                        ):
                            batch.append(
                                SearchResult(data, score)
                            )
                        if len(batch) >= self._batch_size:
                            GLib.idle_add(_idle_call, on_batch, generation, batch)
                            batch = []
                except GLib.Error as caught:
                    if caught.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                        break
                    continue
                finally:
                    if enumerator is not None:
                        try:
                            enumerator.close(cancellable)
                        except GLib.Error:
                            pass
            if batch:
                GLib.idle_add(_idle_call, on_batch, generation, batch)
        except Exception as caught:
            # The search worker reports unexpected provider failures through
            # the completion callback so that the view remains responsive.
            if not cancellable.is_cancelled():
                error = caught
        finally:
            GLib.idle_add(_idle_call, on_done, generation, error)
