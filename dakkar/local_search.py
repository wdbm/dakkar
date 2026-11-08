"""
Provide an optional indexed filename search provider.

Compatible LocalSearch services may expose either the TinySPARQL or Tracker
GObject Introspection namespace. Provider failure is non-fatal because direct
filesystem search supplies the complete fallback.
"""

from __future__ import annotations

from collections.abc import Callable
import threading

import gi
from gi.repository import Gio, GLib

from .models import query_item_data
from .search import SearchResult, matches_filters, score_filename
from .services import _idle_call


INDEX_QUERY_SCOPED = """
SELECT DISTINCT ?url WHERE {
  GRAPH tracker:FileSystem {
    ?file a nfo:FileDataObject ;
          nie:url ?url ;
          fts:match ~match .
  }
  FILTER(STRSTARTS(?url, CONCAT(~location, '/')))
}
"""

INDEX_QUERY_GLOBAL = """
SELECT DISTINCT ?url WHERE {
  GRAPH tracker:FileSystem {
    ?file a nfo:FileDataObject ;
          nie:url ?url ;
          fts:match ~match .
  }
}
"""


def _load_sparql_namespace():
    for namespace in ("Tsparql", "Tracker"):
        try:
            gi.require_version(namespace, "3.0")
            repository = __import__("gi.repository", fromlist=[namespace])
            return getattr(repository, namespace)
        except (ImportError, ValueError, AttributeError):
            continue
    return None


class IndexedFilenameSearch:
    """
    Query a compatible host filename index when bindings are available.
    """

    def __init__(self, enabled: bool = True, batch_size: int = 64) -> None:
        self._namespace = _load_sparql_namespace() if enabled else None
        self._batch_size = batch_size
        self._generation = 0
        self._lock = threading.Lock()
        self._cancellable: Gio.Cancellable | None = None
        self._connection = None
        self._service_name: str | None = None

    @property
    def bindings_available(self) -> bool:
        return self._namespace is not None

    @property
    def service_name(self) -> str | None:
        return self._service_name

    def cancel(self) -> None:
        with self._lock:
            if self._cancellable is not None:
                self._cancellable.cancel()

    def start(
        self,
        location: Gio.File | None,
        query: str,
        on_batch: Callable[[int, list[SearchResult]], None],
        on_done: Callable[[int, Exception | None], None],
        mime_group: str = "all",
        date_field: str = "modified",
        after_timestamp: int | None = None,
    ) -> int | None:
        if self._namespace is None:
            return None
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
                location,
                query,
                cancellable,
                on_batch,
                on_done,
                mime_group,
                date_field,
                after_timestamp,
            ),
            name=f"dakkar-index-{generation}",
            daemon=True,
        ).start()
        return generation

    def _connect(self):
        if self._connection is not None:
            return self._connection
        errors: list[Exception] = []
        for service_name in (
            "org.freedesktop.LocalSearch3",
            "org.freedesktop.Tracker3.Miner.Files",
        ):
            try:
                connection = self._namespace.SparqlConnection.bus_new(
                    service_name,
                    None,
                    None,
                )
                self._connection = connection
                self._service_name = service_name
                return connection
            except Exception as error:
                # Binding implementations expose different exception types;
                # retain each failure while trying the next compatible service.
                errors.append(error)
        raise RuntimeError(
            "No compatible LocalSearch service is available: "
            + "; ".join(str(error) for error in errors)
        )

    def _run(
        self,
        generation: int,
        location: Gio.File | None,
        query: str,
        cancellable: Gio.Cancellable,
        on_batch: Callable[[int, list[SearchResult]], None],
        on_done: Callable[[int, Exception | None], None],
        mime_group: str,
        date_field: str,
        after_timestamp: int | None,
    ) -> None:
        error: Exception | None = None
        cursor = None
        batch: list[SearchResult] = []
        try:
            connection = self._connect()
            statement = connection.query_statement(
                INDEX_QUERY_SCOPED if location is not None else INDEX_QUERY_GLOBAL,
                cancellable,
            )
            statement.bind_string("match", query)
            if location is not None:
                statement.bind_string("location", location.get_uri().rstrip("/"))
            cursor = statement.execute(cancellable)
            while not cancellable.is_cancelled() and cursor.next(cancellable):
                uri_value = cursor.get_string(0)
                uri = uri_value[0] if isinstance(uri_value, tuple) else uri_value
                if not uri:
                    continue
                file = Gio.File.new_for_uri(uri)
                try:
                    data = query_item_data(file, cancellable)
                except GLib.Error:
                    continue
                score = score_filename(data.display_name, query)
                if score is None or not matches_filters(
                    data,
                    mime_group,
                    date_field,
                    after_timestamp,
                ):
                    continue
                batch.append(SearchResult(data, score + 25.0))
                if len(batch) >= self._batch_size:
                    GLib.idle_add(_idle_call, on_batch, generation, batch)
                    batch = []
            if batch:
                GLib.idle_add(_idle_call, on_batch, generation, batch)
        except Exception as caught:
            # Optional index failures are reported to the caller and do not
            # interrupt the complete direct-search fallback.
            if not cancellable.is_cancelled():
                error = caught
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    # Cursor cleanup must not conceal the search result.
                    pass
            GLib.idle_add(_idle_call, on_done, generation, error)
