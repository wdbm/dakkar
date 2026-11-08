"""
Store named filename searches as JSON.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SavedSearch:
    name: str
    query: str
    global_search: bool
    location_uri: str
    mime_group: str = "all"
    date_field: str = "modified"
    period: str = "any"


def default_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "dakkar" / "saved_searches.json"


class SavedSearchStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()

    def load(self) -> list[SavedSearch]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return []
        searches: list[SavedSearch] = []
        if not isinstance(raw, list):
            return searches
        for value in raw:
            try:
                search = SavedSearch(**value)
            except (TypeError, ValueError):
                continue
            if search.name.strip() and search.query.strip():
                searches.append(search)
        return searches

    def save(self, searches: list[SavedSearch]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps([asdict(search) for search in searches], indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def add(self, search: SavedSearch) -> None:
        searches = [value for value in self.load() if value.name != search.name]
        searches.append(search)
        searches.sort(key=lambda value: value.name.casefold())
        self.save(searches)
