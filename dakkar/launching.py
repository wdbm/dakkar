"""
Group selected files for batched application launches.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from gi.repository import Gio

from .models import FileItem


ApplicationResolver = Callable[[FileItem], Gio.AppInfo | None]
ApplicationLookup = Callable[[str], list[Gio.AppInfo]]


def _default_application(item: FileItem) -> Gio.AppInfo | None:
    return Gio.AppInfo.get_default_for_type(
        item.content_type,
        not item.file.is_native(),
    )


def group_items_by_default_application(
    items: Iterable[FileItem],
    resolver: ApplicationResolver | None = None,
) -> tuple[list[tuple[Gio.AppInfo, list[FileItem]]], list[FileItem]]:
    """
    Group items sharing a default application and retain unhandled items.
    """

    if resolver is None:
        application_cache: dict[tuple[str, bool], Gio.AppInfo | None] = {}

        def resolve(item: FileItem) -> Gio.AppInfo | None:
            key = (item.content_type, not item.file.is_native())
            if key not in application_cache:
                application_cache[key] = _default_application(item)
            return application_cache[key]
    else:
        resolve = resolver
    groups: list[tuple[Gio.AppInfo, list[FileItem]]] = []
    unhandled: list[FileItem] = []
    for item in items:
        application = resolve(item)
        if application is None:
            unhandled.append(item)
            continue
        for grouped_application, grouped_items in groups:
            if application.equal(grouped_application):
                grouped_items.append(item)
                break
        else:
            groups.append((application, [item]))
    return groups, unhandled


def common_applications(
    items: Iterable[FileItem],
    lookup: ApplicationLookup | None = None,
) -> list[Gio.AppInfo]:
    """
    Return applications advertised for every selected content type.
    """

    selected = list(items)
    if not selected:
        return []
    find = lookup or Gio.AppInfo.get_all_for_type
    content_types = tuple(dict.fromkeys(item.content_type for item in selected))
    application_lists = [find(content_type) for content_type in content_types]
    if not application_lists:
        return []
    require_uris = any(not item.file.is_native() for item in selected)
    common: list[Gio.AppInfo] = []
    for candidate in application_lists[0]:
        if require_uris and not candidate.supports_uris():
            continue
        if any(candidate.equal(existing) for existing in common):
            continue
        if all(
            any(candidate.equal(application) for application in applications)
            for applications in application_lists[1:]
        ):
            common.append(candidate)
    return common
