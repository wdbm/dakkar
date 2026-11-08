"""
Provide file models shared by views and services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject


FILE_ATTRIBUTES = ",".join(
    (
        Gio.FILE_ATTRIBUTE_STANDARD_NAME,
        Gio.FILE_ATTRIBUTE_STANDARD_DISPLAY_NAME,
        Gio.FILE_ATTRIBUTE_STANDARD_TYPE,
        Gio.FILE_ATTRIBUTE_STANDARD_CONTENT_TYPE,
        Gio.FILE_ATTRIBUTE_STANDARD_FAST_CONTENT_TYPE,
        Gio.FILE_ATTRIBUTE_STANDARD_SIZE,
        Gio.FILE_ATTRIBUTE_STANDARD_IS_HIDDEN,
        Gio.FILE_ATTRIBUTE_STANDARD_IS_BACKUP,
        Gio.FILE_ATTRIBUTE_STANDARD_IS_SYMLINK,
        Gio.FILE_ATTRIBUTE_STANDARD_ICON,
        Gio.FILE_ATTRIBUTE_ACCESS_CAN_EXECUTE,
        Gio.FILE_ATTRIBUTE_TIME_MODIFIED,
        Gio.FILE_ATTRIBUTE_TIME_ACCESS,
        Gio.FILE_ATTRIBUTE_TIME_CREATED,
        Gio.FILE_ATTRIBUTE_THUMBNAIL_PATH,
        Gio.FILE_ATTRIBUTE_THUMBNAIL_IS_VALID,
    )
)


@dataclass(frozen=True, slots=True)
class ItemData:
    uri: str
    name: str
    display_name: str
    is_directory: bool
    is_symlink: bool
    is_hidden: bool
    is_executable: bool
    content_type: str
    size: int
    modified: int
    accessed: int
    created: int
    icon: Gio.Icon | None
    thumbnail_path: str | None

    @classmethod
    def from_info(cls, file: Gio.File, info: Gio.FileInfo) -> "ItemData":
        content_type = (
            info.get_content_type()
            or info.get_attribute_string(Gio.FILE_ATTRIBUTE_STANDARD_FAST_CONTENT_TYPE)
            or "application/octet-stream"
        )
        thumbnail_path = None
        if info.get_attribute_boolean(Gio.FILE_ATTRIBUTE_THUMBNAIL_IS_VALID):
            thumbnail_path = info.get_attribute_byte_string(
                Gio.FILE_ATTRIBUTE_THUMBNAIL_PATH
            )
        return cls(
            uri=file.get_uri(),
            name=info.get_name() or "",
            display_name=info.get_display_name() or info.get_name() or file.get_uri(),
            is_directory=info.get_file_type() == Gio.FileType.DIRECTORY,
            is_symlink=info.get_is_symlink(),
            is_hidden=info.get_is_hidden() or info.get_is_backup(),
            is_executable=info.get_attribute_boolean(
                Gio.FILE_ATTRIBUTE_ACCESS_CAN_EXECUTE
            ),
            content_type=content_type,
            size=max(0, info.get_size()),
            modified=info.get_attribute_uint64(Gio.FILE_ATTRIBUTE_TIME_MODIFIED),
            accessed=info.get_attribute_uint64(Gio.FILE_ATTRIBUTE_TIME_ACCESS),
            created=info.get_attribute_uint64(Gio.FILE_ATTRIBUTE_TIME_CREATED),
            icon=info.get_icon(),
            thumbnail_path=thumbnail_path,
        )


class FileItem(GObject.Object):
    """
    Wrap item data for use with GTK list models.
    """

    __gtype_name__ = "DakkarFileItem"
    __gsignals__ = {
        "metadata-changed": (
            GObject.SignalFlags.RUN_LAST,
            None,
            (),
        ),
    }

    def __init__(self, data: ItemData, relevance: float = 0.0) -> None:
        super().__init__()
        self.data = data
        self.relevance = relevance
        self.file = Gio.File.new_for_uri(data.uri)
        self.display_name_collation_key = GLib.utf8_collate_key_for_filename(
            data.display_name,
            -1,
        )
        self.directory_count: int | None = None
        self.deep_modified = data.modified
        self.deep_size = data.size

    def set_directory_metadata(
        self,
        count: int | None,
        deep_modified: int,
        deep_size: int,
    ) -> None:
        changed = (
            self.directory_count != count
            or self.deep_modified != deep_modified
            or self.deep_size != deep_size
        )
        self.directory_count = count
        self.deep_modified = deep_modified
        self.deep_size = deep_size
        if changed:
            self.emit("metadata-changed")

    @property
    def uri(self) -> str:
        return self.data.uri

    @property
    def display_name(self) -> str:
        return self.data.display_name

    @property
    def is_directory(self) -> bool:
        return self.data.is_directory

    @property
    def is_executable(self) -> bool:
        return self.data.is_executable

    @property
    def content_type(self) -> str:
        return self.data.content_type

    @property
    def size_text(self) -> str:
        if self.data.is_directory:
            if self.directory_count is None:
                return "Directory"
            noun = "item" if self.directory_count == 1 else "items"
            return f"{self.directory_count} {noun}"
        return GLib.format_size(self.data.size)

    @property
    def modified_text(self) -> str:
        if not self.data.modified:
            return "Unknown"
        return datetime.fromtimestamp(self.data.modified).strftime("%d %b %Y, %H:%M")

    @property
    def type_text(self) -> str:
        if self.data.is_directory:
            return "Directory"
        description = Gio.content_type_get_description(self.data.content_type)
        return description or self.data.content_type


def query_item_data(file: Gio.File, cancellable: Gio.Cancellable | None = None) -> ItemData:
    info = file.query_info(
        FILE_ATTRIBUTES,
        Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
        cancellable,
    )
    return ItemData.from_info(file, info)
