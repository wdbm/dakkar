"""
Read GTK bookmark files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from urllib.parse import unquote, urlparse


@dataclass(frozen=True, slots=True)
class Bookmark:
    uri: str
    label: str


def default_bookmark_paths() -> tuple[Path, ...]:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return (
        config_home / "gtk-4.0" / "bookmarks",
        config_home / "gtk-3.0" / "bookmarks",
    )


def _default_label(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
        return path.name or str(path)
    return unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1]) or uri


def parse_bookmark_line(line: str) -> Bookmark | None:
    value = line.strip()
    if not value or value.startswith("#"):
        return None
    uri, separator, label = value.partition(" ")
    return Bookmark(uri=uri, label=label.strip() if separator else _default_label(uri))


def load_bookmarks(paths: tuple[Path, ...] | None = None) -> list[Bookmark]:
    bookmarks: list[Bookmark] = []
    seen: set[str] = set()
    for path in paths or default_bookmark_paths():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError, UnicodeError):
            continue
        for line in lines:
            bookmark = parse_bookmark_line(line)
            if bookmark is not None and bookmark.uri not in seen:
                seen.add(bookmark.uri)
                bookmarks.append(bookmark)
    return bookmarks
