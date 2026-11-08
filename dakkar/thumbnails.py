"""
Generate shared freedesktop thumbnails for visible cells.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count
import threading

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GnomeDesktop", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, GnomeDesktop

from .models import ItemData


ThumbnailCallback = Callable[[str, Gdk.Texture | None], None]
MAXIMUM_TEXTURE_CACHE_ITEMS = 512


@dataclass(frozen=True, slots=True)
class _Request:
    data: ItemData


@dataclass(frozen=True, slots=True)
class _Waiter:
    token: int
    owner_id: int
    generation: int
    callback: ThumbnailCallback


class ThumbnailService:
    """
    Generate and cache thumbnails through the GNOME Desktop thumbnail factory.
    """

    def __init__(self, maximum_parallel: int = 4) -> None:
        self._factory = GnomeDesktop.DesktopThumbnailFactory.new(
            GnomeDesktop.DesktopThumbnailSize.LARGE
        )
        self._maximum_parallel = maximum_parallel
        self._running = 0
        self._queue: deque[_Request] = deque()
        self._queued_keys: set[tuple[str, int]] = set()
        self._running_keys: set[tuple[str, int]] = set()
        self._textures: OrderedDict[
            tuple[str, int],
            Gdk.Texture | None,
        ] = OrderedDict()
        self._waiting: dict[tuple[str, int], list[_Waiter]] = {}
        self._token_keys: dict[int, tuple[str, int]] = {}
        self._owner_generations: dict[int, int] = {}
        self._tokens = count(1)

    def begin_generation(self, owner_id: int, generation: int) -> None:
        """
        Activate one owner generation and discard its obsolete requests.
        """

        self._owner_generations[owner_id] = generation
        self._remove_waiters(
            lambda waiter: (
                waiter.owner_id == owner_id
                and waiter.generation != generation
            )
        )

    def release_owner(self, owner_id: int) -> None:
        """
        Remove all pending requests and generation state for an owner.
        """

        self._owner_generations.pop(owner_id, None)
        self._remove_waiters(lambda waiter: waiter.owner_id == owner_id)

    def cancel_request(self, token: int | None) -> None:
        """
        Cancel a queued callback which is no longer represented by a cell.
        """

        if token is None:
            return
        key = self._token_keys.pop(token, None)
        if key is None:
            return
        waiters = self._waiting.get(key, [])
        remaining = [waiter for waiter in waiters if waiter.token != token]
        if remaining:
            self._waiting[key] = remaining
        else:
            self._waiting.pop(key, None)

    def _remove_waiters(self, remove: Callable[[_Waiter], bool]) -> None:
        for key, waiters in tuple(self._waiting.items()):
            remaining: list[_Waiter] = []
            for waiter in waiters:
                if remove(waiter):
                    self._token_keys.pop(waiter.token, None)
                else:
                    remaining.append(waiter)
            if remaining:
                self._waiting[key] = remaining
            else:
                self._waiting.pop(key, None)

    def request(
        self,
        data: ItemData,
        callback: ThumbnailCallback,
        owner_id: int,
        generation: int,
    ) -> int | None:
        """
        Queue a visible-cell request and return its cancellation token.
        """

        if data.is_directory:
            callback(data.uri, None)
            return None
        if self._owner_generations.get(owner_id) != generation:
            return None
        key = (data.uri, data.modified)
        if key in self._textures:
            texture = self._textures[key]
            self._textures.move_to_end(key)
            callback(data.uri, texture)
            return None
        token = next(self._tokens)
        waiter = _Waiter(token, owner_id, generation, callback)
        self._token_keys[token] = key
        if key in self._waiting:
            self._waiting[key].append(waiter)
            return token
        self._waiting[key] = [waiter]
        if key not in self._queued_keys and key not in self._running_keys:
            # Newly visible cells take precedence over obsolete scroll-ahead work.
            self._queue.appendleft(_Request(data))
            self._queued_keys.add(key)
        self._start_queued()
        return token

    def _start_queued(self) -> None:
        while self._queue and self._running < self._maximum_parallel:
            request = self._queue.popleft()
            data = request.data
            key = (data.uri, data.modified)
            self._queued_keys.discard(key)
            if key not in self._waiting or key in self._running_keys:
                continue
            if data.thumbnail_path:
                self._running += 1
                self._running_keys.add(key)
                threading.Thread(
                    target=self._load_cached_thumbnail,
                    args=(data,),
                    name="dakkar-cached-thumbnail",
                    daemon=True,
                ).start()
                continue
            if not self._factory.can_thumbnail(
                data.uri,
                data.content_type,
                data.modified,
            ):
                self._complete(data, None)
                continue
            self._running += 1
            self._running_keys.add(key)
            self._factory.generate_thumbnail_async(
                data.uri,
                data.content_type,
                None,
                self._generated,
                data,
            )

    def _load_cached_thumbnail(self, data: ItemData) -> None:
        pixbuf = None
        try:
            if data.thumbnail_path:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file(data.thumbnail_path)
        except GLib.Error:
            pixbuf = None
        GLib.idle_add(self._cached_thumbnail_loaded, data, pixbuf)

    def _cached_thumbnail_loaded(
        self,
        data: ItemData,
        pixbuf: GdkPixbuf.Pixbuf | None,
    ) -> bool:
        key = (data.uri, data.modified)
        if key not in self._waiting:
            self._running -= 1
            self._running_keys.discard(key)
            self._start_queued()
            return GLib.SOURCE_REMOVE
        if pixbuf is None and self._factory.can_thumbnail(
            data.uri,
            data.content_type,
            data.modified,
        ):
            self._factory.generate_thumbnail_async(
                data.uri,
                data.content_type,
                None,
                self._generated,
                data,
            )
            return GLib.SOURCE_REMOVE
        texture = (
            Gdk.Texture.new_for_pixbuf(pixbuf)
            if pixbuf is not None
            else None
        )
        self._running -= 1
        self._running_keys.discard(key)
        self._complete(data, texture)
        self._start_queued()
        return GLib.SOURCE_REMOVE

    def _generated(self, factory, result, data: ItemData) -> None:
        texture = None
        try:
            pixbuf = factory.generate_thumbnail_finish(result)
            if pixbuf is not None:
                texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                factory.save_thumbnail_async(
                    pixbuf,
                    data.uri,
                    data.modified,
                    None,
                    self._saved,
                    None,
                )
        except GLib.Error:
            texture = None
        self._running -= 1
        self._running_keys.discard((data.uri, data.modified))
        self._complete(data, texture)
        self._start_queued()

    @staticmethod
    def _saved(factory, result, _user_data) -> None:
        try:
            factory.save_thumbnail_finish(result)
        except GLib.Error:
            pass

    def _complete(self, data: ItemData, texture: Gdk.Texture | None) -> None:
        key = (data.uri, data.modified)
        self._textures[key] = texture
        self._textures.move_to_end(key)
        while len(self._textures) > MAXIMUM_TEXTURE_CACHE_ITEMS:
            self._textures.popitem(last=False)
        callbacks = self._waiting.pop(key, [])
        for waiter in callbacks:
            self._token_keys.pop(waiter.token, None)
            if (
                self._owner_generations.get(waiter.owner_id)
                == waiter.generation
            ):
                waiter.callback(data.uri, texture)
