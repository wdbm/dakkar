"""
Provide the directory and search view owned by one tab.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import threading
from typing import TYPE_CHECKING

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from .directory_tint import (
    DirectoryTintController,
    DirectoryTintIcon,
    DirectoryTintSettings,
    TintMetrics,
)
from .expandable import ExpandableDirectoryModel
from .history import LocationHistory
from .local_search import IndexedFilenameSearch
from .models import FileItem, ItemData, query_item_data
from .operation_panel import OperationPanel
from .search import FilenameSearch, SearchResult, matches_filters
from .services import DirectoryLoader, DirectoryMetadataLoader, SelectionSizeLoader
from .thumbnails import ThumbnailService

if TYPE_CHECKING:
    from .window import DakkarWindow


DEFAULT_GRID_PADDING = 0
DEFAULT_GRID_SPACING = 0
DEFAULT_ICON_SIZE = 85
DEFAULT_SHOW_FULL_ITEM_NAMES = False
MAX_GRID_PADDING = 64
MAX_GRID_SPACING = 32
MIN_ICON_SIZE = 48
MAX_ICON_SIZE = 256
SORT_KEYS = (
    "name",
    "modified",
    "deep-modified",
    "size",
    "type",
    "relevance",
)
DEFAULT_SORT_KEY = SORT_KEYS[0]
SELECTION_SIZE_DELAY_MS = 500
DIRECTORY_COUNT_DELAY_MS = 600
VISIBLE_DIRECTORY_COUNT_DELAY_MS = 80
MONITOR_UPDATE_DELAY_MS = 150
SEARCH_DELAY_MS = 130
URI_SELECTION_RETRY_DELAY_MS = 100
CONTEXT_MENU_MIN_HEIGHT = 176
RECENT_DIRECTORY_CACHE_SIZE = 10
RECENT_DIRECTORY_CACHE_ITEM_LIMIT = 50_000
DEFAULT_DESCENDING_SORT_KEYS = frozenset(
    {
        "modified",
        "deep-modified",
        "size",
        "relevance",
    }
)


def sort_is_descending(key: str, reverse: bool = False) -> bool:
    """
    Return the natural direction for a sort key, optionally reversed.
    """

    return (key in DEFAULT_DESCENDING_SORT_KEYS) != reverse


def _compare_strings(left: str, right: str) -> Gtk.Ordering:
    left_key = GLib.utf8_collate_key_for_filename(left, -1)
    right_key = GLib.utf8_collate_key_for_filename(right, -1)
    return _compare_collation_keys(left_key, right_key)


def _compare_collation_keys(left_key: str, right_key: str) -> Gtk.Ordering:
    if left_key < right_key:
        return Gtk.Ordering.SMALLER
    if left_key > right_key:
        return Gtk.Ordering.LARGER
    return Gtk.Ordering.EQUAL


@dataclass(frozen=True, slots=True)
class _DirectorySnapshot:
    items: tuple[ItemData, ...]


class BrowserTab(Gtk.Box):
    """
    Represent one independently cancellable browser tab.
    """

    def __init__(
        self,
        window: DakkarWindow,
        location: Gio.File,
        thumbnails: ThumbnailService,
        use_index: bool,
        directory_tint: bool = False,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.location = location
        self.history = LocationHistory()
        self.history.push(location.get_uri())
        self.show_hidden = False
        self.view_name = "grid"
        self.sort_key = DEFAULT_SORT_KEY
        self.sort_descending = False
        self.grid_padding = DEFAULT_GRID_PADDING
        self.grid_spacing = DEFAULT_GRID_SPACING
        self.grid_icon_size = DEFAULT_ICON_SIZE
        self.show_full_item_names = DEFAULT_SHOW_FULL_ITEM_NAMES
        self._grid_cells: set[Gtk.Box] = set()
        self._list_cells: set[Gtk.Box] = set()
        self._saved_expandable_uris: set[str] = set()
        self._directory_tint = DirectoryTintController(directory_tint)
        self._thumbnails = thumbnails
        self._loader = DirectoryLoader()
        self._directory_metadata_loader = DirectoryMetadataLoader()
        self._selection_size_loader = SelectionSizeLoader()
        self._search = FilenameSearch()
        self._indexed_search = IndexedFilenameSearch(use_index)
        self._generation = 0
        self._directory_metadata_generation = 0
        self._directory_metadata_recursive = False
        self._directory_metadata_timeout = 0
        self._directory_enumeration_complete = False
        self._directory_items_by_uri: dict[str, FileItem] = {}
        self._pending_directory_count_uris: set[str] = set()
        self._inflight_directory_count_uris: set[str] = set()
        self._search_serial = 0
        self._search_timeout = 0
        self._search_auto_select_pending = False
        self._searching = False
        self._removed_search_uris: set[str] = set()
        self._global_search = False
        self.search_mime_group = "all"
        self.search_date_field = "modified"
        self.search_period = "any"
        self._directory_data: list[ItemData] = []
        self._directory_data_by_uri: dict[str, ItemData] = {}
        self._directory_data_positions: dict[str, int] = {}
        self._incoming_directory_data: list[ItemData] = []
        self._showing_cached_directory = False
        self._loaded_show_hidden = self.show_hidden
        self._recent_directories: OrderedDict[
            tuple[str, bool],
            _DirectorySnapshot,
        ] = OrderedDict()
        self._result_uris: set[str] = set()
        self._monitor: Gio.FileMonitor | None = None
        self._monitor_timeout = 0
        self._monitor_update_serial = 0
        self._pending_monitor_queries: dict[str, Gio.File] = {}
        self._pending_monitor_removals: set[str] = set()
        self._user_scrolled_during_load = False
        self._reset_scroll_after_load = True
        self._thumbnail_owner_id = id(self)
        self._thumbnail_generation = 0
        self._thumbnails.begin_generation(
            self._thumbnail_owner_id,
            self._thumbnail_generation,
        )
        self._filesystem_details: str | None = None
        self._base_status = ""
        self._selection_size_timeout = 0
        self._selection_size_serial = 0
        self._tree_tint_refresh_timeout = 0

        self.store = Gio.ListStore.new(FileItem)
        self.sorter = Gtk.CustomSorter.new(self._compare_items)
        self.sorted_model = Gtk.SortListModel(model=self.store, sorter=self.sorter)
        self.sorted_model.set_incremental(True)
        self.selection = Gtk.MultiSelection(model=self.sorted_model)
        self.expandable_model = ExpandableDirectoryModel(
            self.sorted_model,
            self.sorter,
            lambda: self.show_hidden,
            self._matches_current_filters,
            self._requires_recursive_directory_metadata,
            self._tree_items_changed,
        )
        self.expandable_selection = Gtk.MultiSelection(
            model=self.expandable_model.model
        )
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.grid_view = self._create_grid_view()
        self._grid_style_provider = Gtk.CssProvider()
        self.grid_view.get_style_context().add_provider(
            self._grid_style_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self._update_grid_style()
        self.list_view = self._create_list_view()
        self.expandable_view = self._create_expandable_view()
        self._install_context_menu(self.grid_view)
        self._install_context_menu(self.list_view)
        self._install_context_menu(self.expandable_view)
        self._install_middle_click(self.grid_view)
        self._install_middle_click(self.list_view)
        self._install_middle_click(self.expandable_view)
        self._install_background_deselection(self.grid_view)
        self._install_background_deselection(self.list_view)
        self._install_background_deselection(self.expandable_view)
        self._install_drag_and_drop(self.grid_view)
        self._install_drag_and_drop(self.list_view)
        self._install_drag_and_drop(self.expandable_view)
        self.grid_scrolled_window = self._scrolled(self.grid_view)
        self.list_scrolled_window = self._scrolled(self.list_view)
        self.expandable_scrolled_window = self._scrolled(self.expandable_view)
        for scrolled_window in (
            self.grid_scrolled_window,
            self.list_scrolled_window,
            self.expandable_scrolled_window,
        ):
            scrolled_window.get_vadjustment().connect(
                "value-changed",
                self._directory_view_scrolled,
            )
        self.stack.add_named(self.grid_scrolled_window, "grid")
        self.stack.add_named(self.list_scrolled_window, "list")
        self.stack.add_named(
            self.expandable_scrolled_window,
            "expandable",
        )
        self.append(self.stack)

        self.status_label = Gtk.Label(xalign=0.0)
        self.status_label.add_css_class("dakkar-status")
        self.operation_panel = OperationPanel(window.operations.monitor, self.status_label)
        self.append(self.operation_panel)
        self.selection.connect("selection-changed", self._selection_changed)
        self.expandable_selection.connect(
            "selection-changed",
            self._selection_changed,
        )
        self.navigate(location, add_history=False)

    def _scrolled(self, child: Gtk.Widget) -> Gtk.ScrolledWindow:
        scrolled = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scrolled.add_css_class("dakkar-file-area")
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(child)
        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.HORIZONTAL
            | Gtk.EventControllerScrollFlags.VERTICAL
        )
        scroll.connect("scroll", self._directory_scroll_input)
        scrolled.add_controller(scroll)
        scrolled._dakkar_scroll_controller = scroll
        return scrolled

    def _directory_scroll_input(
        self,
        _controller: Gtk.EventControllerScroll,
        _delta_x: float,
        _delta_y: float,
    ) -> bool:
        if not self._directory_enumeration_complete:
            self._user_scrolled_during_load = True
        return False

    def _create_item_icon(self, pixel_size: int) -> Gtk.Widget:
        if self._directory_tint.enabled:
            return DirectoryTintIcon(pixel_size)
        image = Gtk.Image(pixel_size=pixel_size)
        image.set_icon_size(Gtk.IconSize.LARGE)
        return image

    def _begin_thumbnail_generation(self) -> None:
        self._thumbnail_generation += 1
        self._thumbnails.begin_generation(
            self._thumbnail_owner_id,
            self._thumbnail_generation,
        )

    def _cancel_cell_thumbnail(self, cell: Gtk.Widget) -> None:
        self._thumbnails.cancel_request(
            getattr(cell, "_dakkar_thumbnail_request", None)
        )
        cell._dakkar_thumbnail_request = None

    def _request_cell_thumbnail(
        self,
        cell: Gtk.Widget,
        item: FileItem,
    ) -> None:
        self._cancel_cell_thumbnail(cell)
        cell._dakkar_thumbnail_request = self._thumbnails.request(
            item.data,
            lambda uri, texture, target=cell: self._set_grid_thumbnail(
                target,
                uri,
                texture,
            ),
            self._thumbnail_owner_id,
            self._thumbnail_generation,
        )

    def _create_grid_view(self) -> Gtk.GridView:
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._grid_setup)
        factory.connect("bind", self._grid_bind)
        factory.connect("unbind", self._grid_unbind)
        factory.connect("teardown", self._grid_teardown)
        view = Gtk.GridView(model=self.selection, factory=factory)
        view.set_enable_rubberband(True)
        view.set_min_columns(2)
        view.set_max_columns(14)
        view.set_single_click_activate(False)
        view.connect("activate", self._activate_position)
        return view

    def _grid_setup(self, _factory, list_item: Gtk.ListItem) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("dakkar-grid-cell")
        image = self._create_item_icon(self.grid_icon_size)
        image.set_size_request(self.grid_icon_size, self.grid_icon_size)
        label = Gtk.Label(
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            justify=Gtk.Justification.CENTER,
        )
        self._configure_grid_label(label)
        label.add_css_class("dakkar-grid-label")
        count_label = Gtk.Label()
        count_label.add_css_class("dim-label")
        count_label.set_visible(False)
        box.append(image)
        box.append(label)
        box.append(count_label)
        box._dakkar_image = image
        box._dakkar_label = label
        box._dakkar_count_label = count_label
        box._dakkar_uri = None
        box._dakkar_item = None
        box._dakkar_metadata_handler = None
        box._dakkar_thumbnail_request = None
        list_item.set_child(box)
        self._grid_cells.add(box)

    def _grid_unbind(self, _factory, list_item: Gtk.ListItem) -> None:
        child = list_item.get_child()
        if child is not None:
            self._cancel_cell_thumbnail(child)
            item = getattr(child, "_dakkar_item", None)
            if isinstance(item, FileItem):
                self._dequeue_visible_directory_count(item)
            self._disconnect_metadata_cell(child)
            child._dakkar_item = None

    def _grid_teardown(self, _factory, list_item: Gtk.ListItem) -> None:
        child = list_item.get_child()
        if child is not None:
            self._cancel_cell_thumbnail(child)
            self._disconnect_metadata_cell(child)
            self._grid_cells.discard(child)

    def _grid_bind(self, _factory, list_item: Gtk.ListItem) -> None:
        item = list_item.get_item()
        child = list_item.get_child()
        if not isinstance(item, FileItem) or child is None:
            return
        self._cancel_cell_thumbnail(child)
        self._disconnect_metadata_cell(child)
        child._dakkar_uri = item.uri
        child._dakkar_item = item
        child._dakkar_label.set_text(item.display_name)
        self._update_grid_count_label(child, item)
        self._connect_metadata_cell(
            child,
            item,
            lambda changed_item, cell=child: self._update_grid_count_label(
                cell,
                changed_item,
            ),
        )
        if item.data.icon is not None:
            child._dakkar_image.set_from_gicon(item.data.icon)
        else:
            child._dakkar_image.set_from_icon_name("folder-symbolic" if item.is_directory else "text-x-generic-symbolic")
        self._apply_directory_tint_to_cell(child)
        if item.is_directory:
            self._queue_visible_directory_count(item)
        else:
            self._request_cell_thumbnail(child, item)

    @staticmethod
    def _disconnect_metadata_cell(cell: Gtk.Widget) -> None:
        item = getattr(cell, "_dakkar_item", None)
        handler = getattr(cell, "_dakkar_metadata_handler", None)
        if isinstance(item, FileItem) and handler is not None:
            item.disconnect(handler)
        cell._dakkar_metadata_handler = None

    def _connect_metadata_cell(
        self,
        cell: Gtk.Widget,
        item: FileItem,
        callback: Callable[[FileItem], None],
    ) -> None:
        self._disconnect_metadata_cell(cell)
        cell._dakkar_item = item
        if item.is_directory:
            cell._dakkar_metadata_handler = item.connect(
                "metadata-changed",
                lambda changed_item: (
                    callback(changed_item)
                    if getattr(cell, "_dakkar_item", None) is changed_item
                    else None
                ),
            )

    @staticmethod
    def _update_grid_count_label(cell: Gtk.Box, item: FileItem) -> None:
        if getattr(cell, "_dakkar_item", None) is not item:
            return
        if not item.is_directory:
            cell._dakkar_count_label.set_visible(False)
            return
        if item.directory_count is None:
            cell._dakkar_count_label.set_text(" ")
        else:
            noun = "item" if item.directory_count == 1 else "items"
            cell._dakkar_count_label.set_text(
                f"{item.directory_count} {noun}"
            )
        cell._dakkar_count_label.set_visible(True)

    @staticmethod
    def _set_grid_thumbnail(child, uri: str, texture: Gdk.Texture | None) -> None:
        if texture is not None and child._dakkar_uri == uri:
            child._dakkar_image.set_from_paintable(texture)

    def _refresh_directory_tints(self) -> None:
        if not self._directory_tint.enabled:
            return
        file_items = [
            item
            for position in range(self.store.get_n_items())
            if isinstance(item := self.store.get_item(position), FileItem)
        ]
        file_items.extend(self.expandable_model.visible_child_items())
        unique_items = {item.uri: item for item in file_items}
        items = [
            TintMetrics(
                uri=item.uri,
                is_directory=item.is_directory,
                size=item.deep_size if item.is_directory else item.data.size,
                modified=item.data.modified,
                deep_modified=item.deep_modified,
            )
            for item in unique_items.values()
        ]
        self._directory_tint.update(items, self.sort_key)
        for cell in self._grid_cells | self._list_cells:
            self._apply_directory_tint_to_cell(cell)

    def _tree_items_changed(self) -> None:
        if self._tree_tint_refresh_timeout:
            return
        self._tree_tint_refresh_timeout = GLib.timeout_add(
            1,
            self._refresh_tree_tints_when_idle,
        )

    def _refresh_tree_tints_when_idle(self) -> bool:
        self._tree_tint_refresh_timeout = 0
        self._refresh_directory_tints()
        return GLib.SOURCE_REMOVE

    def _apply_directory_tint_to_cell(self, cell: Gtk.Box) -> None:
        item = getattr(cell, "_dakkar_item", None)
        opacities = (
            self._directory_tint.opacities_for(item.uri)
            if isinstance(item, FileItem)
            else (0.0, 0.0)
        )
        settings = self._directory_tint.settings
        if isinstance(cell._dakkar_image, DirectoryTintIcon):
            cell._dakkar_image.set_tints(
                opacities[0],
                settings.turquoise.edge,
                opacities[1],
                settings.blue.edge,
            )
        if not isinstance(item, FileItem):
            cell.set_tooltip_text(None)
            return
        descriptions = [item.display_name]
        source_labels = {
            "follow-sort": "position ranking",
            "size": "size ranking",
            "modified": "last-modified ranking",
            "deep-modified": "deep-modification ranking",
        }
        for colour, channel, opacity in (
            ("Turquoise", settings.turquoise, opacities[0]),
            ("Blue", settings.blue, opacities[1]),
        ):
            if channel.source == "none" or opacity <= 0.0:
                continue
            descriptions.append(
                f"{colour} {source_labels[channel.source]}: "
                f"{round(opacity * 100)}% strength"
            )
        cell.set_tooltip_text("\n".join(descriptions))

    def _create_list_view(self) -> Gtk.ColumnView:
        view = Gtk.ColumnView(model=self.selection)
        view.set_show_column_separators(False)
        view.set_show_row_separators(False)
        view.set_reorderable(False)
        view.set_single_click_activate(False)
        view.append_column(self._name_column())
        view.append_column(self._text_column("Size", lambda item: item.size_text, False))
        view.append_column(self._text_column("Type", lambda item: item.type_text, True))
        view.append_column(self._text_column("Modified", lambda item: item.modified_text, False))
        view.connect("activate", self._activate_position)
        return view

    def _create_expandable_view(self) -> Gtk.ColumnView:
        view = Gtk.ColumnView(model=self.expandable_selection)
        view.set_show_column_separators(False)
        view.set_show_row_separators(False)
        view.set_reorderable(False)
        view.set_single_click_activate(False)
        view.append_column(self._expandable_name_column())
        view.append_column(
            self._expandable_text_column(
                "Size",
                lambda item: item.size_text,
                False,
            )
        )
        view.append_column(
            self._expandable_text_column(
                "Type",
                lambda item: item.type_text,
                True,
            )
        )
        view.append_column(
            self._expandable_text_column(
                "Modified",
                lambda item: item.modified_text,
                False,
            )
        )
        view.connect("activate", self._activate_position)
        return view

    def _install_drag_and_drop(self, view: Gtk.Widget) -> None:
        drag_source = Gtk.DragSource.new()
        drag_source.set_actions(Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drag_source.connect(
            "prepare",
            lambda _source, x, y: self._prepare_drag(view, x, y),
        )
        view.add_controller(drag_source)

        drop_target = Gtk.DropTarget.new(
            Gdk.FileList.__gtype__,
            Gdk.DragAction.COPY | Gdk.DragAction.MOVE,
        )
        drop_target.connect("enter", self._drop_motion)
        drop_target.connect("motion", self._drop_motion)
        drop_target.connect(
            "drop",
            lambda target, value, x, y: self._drop_files(
                view,
                target,
                value,
                x,
                y,
            ),
        )
        view.add_controller(drop_target)
        view._dakkar_drag_source = drag_source
        view._dakkar_drop_target = drop_target

    def _drop_motion(
        self,
        target: Gtk.DropTarget,
        _x: float,
        _y: float,
    ) -> Gdk.DragAction:
        return self._preferred_drop_action(target)

    @staticmethod
    def _preferred_drop_action(target: Gtk.DropTarget) -> Gdk.DragAction:
        state = target.get_current_event_state()
        preferred = (
            Gdk.DragAction.COPY
            if state & Gdk.ModifierType.CONTROL_MASK
            else Gdk.DragAction.MOVE
        )
        available = Gdk.DragAction.COPY | Gdk.DragAction.MOVE
        drop = target.get_current_drop()
        if drop is not None:
            available = drop.get_actions()
        if available & preferred:
            return preferred
        if available & Gdk.DragAction.MOVE:
            return Gdk.DragAction.MOVE
        if available & Gdk.DragAction.COPY:
            return Gdk.DragAction.COPY
        return Gdk.DragAction(0)

    def _prepare_drag(
        self,
        view: Gtk.Widget,
        x: float,
        y: float,
    ) -> Gdk.ContentProvider | None:
        pointed_item = self._item_at_point(view, x, y)
        self._select_item(pointed_item)
        files = self.selected_files()
        if not files:
            return None
        file_list = Gdk.FileList.new_from_list(files)
        value = GObject.Value(Gdk.FileList.__gtype__)
        value.set_boxed(file_list)
        return Gdk.ContentProvider.new_for_value(value)

    def _drop_files(
        self,
        view: Gtk.Widget,
        target: Gtk.DropTarget,
        value,
        x: float,
        y: float,
    ) -> bool:
        if isinstance(value, GObject.Value):
            value = value.get_boxed()
        if not isinstance(value, Gdk.FileList):
            return False
        files = list(value.get_files())
        if not files:
            return False
        pointed_item = self._item_at_point(view, x, y)
        destination = (
            pointed_item.file
            if pointed_item is not None and pointed_item.is_directory
            else self.location
        )
        action = self._preferred_drop_action(target)
        return self.window.drop_files(
            files,
            destination,
            bool(action & Gdk.DragAction.MOVE),
        )

    @staticmethod
    def _item_at_point(
        view: Gtk.Widget,
        x: float,
        y: float,
    ) -> FileItem | None:
        widget = view.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None and widget is not view:
            item = getattr(widget, "_dakkar_item", None)
            if isinstance(item, FileItem):
                return item
            widget = widget.get_parent()
        return None

    @staticmethod
    def _file_item_from_model_item(model_item: object) -> FileItem | None:
        if isinstance(model_item, FileItem):
            return model_item
        if isinstance(model_item, Gtk.TreeListRow):
            item = model_item.get_item()
            return item if isinstance(item, FileItem) else None
        return None

    def _selection_for_view(self, view: Gtk.Widget) -> Gtk.MultiSelection:
        if view is self.expandable_view:
            return self.expandable_selection
        return self.selection

    def _active_selection(self) -> Gtk.MultiSelection:
        if self.view_name == "expandable" and not self._searching:
            return self.expandable_selection
        return self.selection

    def _install_context_menu(self, view: Gtk.Widget) -> None:
        popover = Gtk.PopoverMenu.new_from_model(Gio.Menu())
        popover.set_size_request(-1, CONTEXT_MENU_MIN_HEIGHT)
        popover.set_parent(view)
        gesture = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)

        def pressed(_gesture, _count, x, y):
            selection = self._selection_for_view(view)
            pointed_item = self._item_at_point(view, x, y)
            if pointed_item is None:
                selection.unselect_all()
            else:
                self._select_item(pointed_item, selection)
            popover.set_menu_model(self._context_menu_model(pointed_item))
            popover.queue_resize()
            rectangle = Gdk.Rectangle()
            rectangle.x = int(x)
            rectangle.y = int(y)
            rectangle.width = 1
            rectangle.height = 1
            popover.set_pointing_to(rectangle)
            popover.popup()

        gesture.connect("pressed", pressed)
        view.add_controller(gesture)
        view._dakkar_context_popover = popover

    def _install_middle_click(self, view: Gtk.Widget) -> None:
        gesture = Gtk.GestureClick(button=Gdk.BUTTON_MIDDLE)
        gesture.connect(
            "pressed",
            lambda controller, _count, x, y: self._middle_click_pressed(
                controller,
                view,
                x,
                y,
            ),
        )
        view.add_controller(gesture)
        view._dakkar_middle_gesture = gesture

    def _middle_click_pressed(
        self,
        gesture: Gtk.GestureClick,
        view: Gtk.Widget,
        x: float,
        y: float,
    ) -> None:
        item = self._item_at_point(view, x, y)
        if item is None:
            return
        self._open_middle_clicked_item(item)
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _open_middle_clicked_item(self, item: FileItem) -> None:
        if item.is_directory:
            self.window.open_location_in_tab(item.file)
        else:
            self.window.open_file(item.file)

    @staticmethod
    def _targeted_menu_item(label: str, action: str, file: Gio.File) -> Gio.MenuItem:
        item = Gio.MenuItem.new(label, None)
        item.set_action_and_target_value(action, GLib.Variant.new_string(file.get_uri()))
        return item

    def _context_menu_model(self, pointed_item: FileItem | None) -> Gio.Menu:
        menu = Gio.Menu()
        selected = self.selected_items()
        single_item = selected[0] if len(selected) == 1 else None
        all_files = bool(selected) and all(not item.is_directory for item in selected)
        background = pointed_item is None
        target_directory = self.location if background else None
        if single_item is not None and single_item.is_directory:
            target_directory = single_item.file

        open_section = Gio.Menu()
        if background:
            open_section.append("New directory…", "win.new-directory")
            open_section.append("New file…", "win.new-file")
        if single_item is not None or all_files:
            open_section.append("Open", "win.open")
        if single_item is not None:
            if single_item.is_directory:
                open_section.append("Open in new tab", "win.open-tab")
                if self.window.can_open_new_pane():
                    open_section.append("Open in new pane", "win.open-pane")
                open_section.append("Open in new window", "win.open-window")
            else:
                if single_item.is_executable:
                    open_section.append("Run as program…", "win.run-program")
            if self._searching:
                open_section.append("Open item location", "win.open-location")
                open_section.append(
                    "Open item location in new tab",
                    "win.open-location-tab",
                )
                if self.window.can_open_new_pane():
                    open_section.append(
                        "Open item location in new pane",
                        "win.open-location-pane",
                    )
                open_section.append(
                    "Open item location in new window",
                    "win.open-location-window",
                )
        if all_files:
            open_section.append("Open with…", "win.open-with")
        if target_directory is not None:
            open_section.append_item(
                self._targeted_menu_item(
                    "Open in terminal",
                    "win.open-terminal-at",
                    target_directory,
                )
            )
            if self.window.can_find_duplicates(target_directory):
                open_section.append_item(
                    self._targeted_menu_item(
                        "Find duplicate copies…",
                        "win.find-duplicates-at",
                        target_directory,
                    )
                )
            if self.window.can_restore_empty_files(target_directory):
                open_section.append_item(
                    self._targeted_menu_item(
                        "Restore empty files…",
                        "win.restore-empty-files-at",
                        target_directory,
                    )
                )
        if open_section.get_n_items():
            menu.append_section(None, open_section)

        transfer_section = Gio.Menu()
        if selected:
            transfer_section.append("Cut", "win.cut")
            transfer_section.append("Copy", "win.copy")
        if target_directory is not None and not self.window.clipboard.empty:
            transfer_section.append_item(
                self._targeted_menu_item(
                    "Paste",
                    "win.paste-to",
                    target_directory,
                )
            )
        if selected:
            transfer_section.append("Copy to…", "win.copy-to")
            transfer_section.append("Move to…", "win.move-to")
        if transfer_section.get_n_items():
            menu.append_section(None, transfer_section)

        if selected:
            edit_section = Gio.Menu()
            edit_section.append("Copy name", "win.copy-name")
            edit_section.append("Copy path", "win.copy-path")
            if single_item is not None:
                edit_section.append("Rename…", "win.rename")
            edit_section.append("Duplicate", "win.duplicate")
            edit_section.append("Move to Trash", "win.trash")
            edit_section.append("Delete permanently…", "win.delete")
            menu.append_section(None, edit_section)

            archive_section = Gio.Menu()
            archive_section.append("Create archive…", "win.create-archive")
            if single_item is not None and not single_item.is_directory:
                archive_section.append("Extract here", "win.extract-here")
                archive_section.append("Extract to…", "win.extract-to")
            menu.append_section(None, archive_section)

        if single_item is not None:
            details_section = Gio.Menu()
            details_section.append("Properties…", "win.properties")
            menu.append_section(None, details_section)
        return menu

    def _install_background_deselection(self, view: Gtk.Widget) -> None:
        gesture = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect(
            "pressed",
            lambda controller, count, x, y: self._background_primary_pressed(
                controller,
                count,
                x,
                y,
                view,
            ),
        )
        view.add_controller(gesture)
        view._dakkar_background_gesture = gesture

    def _background_primary_pressed(
        self,
        gesture: Gtk.GestureClick,
        _count: int,
        x: float,
        y: float,
        view: Gtk.Widget,
    ) -> None:
        selection_modifiers = (
            Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        )
        if gesture.get_current_event_state() & selection_modifiers:
            return
        if self._item_at_point(view, x, y) is None:
            self._selection_for_view(view).unselect_all()

    def _select_item(
        self,
        item: FileItem | None,
        selection: Gtk.MultiSelection | None = None,
    ) -> None:
        target_selection = selection or self._active_selection()
        selected = self._selected_items_from(target_selection)
        if item is None or any(current is item for current in selected):
            return
        for position in range(target_selection.get_n_items()):
            current = self._file_item_from_model_item(
                target_selection.get_item(position)
            )
            if current is item:
                target_selection.unselect_all()
                target_selection.select_item(position, True)
                return

    def _name_column(self) -> Gtk.ColumnViewColumn:
        factory = Gtk.SignalListItemFactory()

        def setup(_factory, list_item):
            box = Gtk.Box(spacing=8)
            image = self._create_item_icon(32)
            label = Gtk.Label(xalign=0.0, ellipsize=3)
            box.append(image)
            box.append(label)
            box._dakkar_image = image
            box._dakkar_label = label
            box._dakkar_uri = None
            box._dakkar_item = None
            box._dakkar_thumbnail_request = None
            list_item.set_child(box)
            self._list_cells.add(box)

        def bind(_factory, list_item):
            item = list_item.get_item()
            child = list_item.get_child()
            if not isinstance(item, FileItem) or child is None:
                return
            self._cancel_cell_thumbnail(child)
            child._dakkar_uri = item.uri
            child._dakkar_item = item
            child._dakkar_label.set_text(item.display_name)
            if item.data.icon is not None:
                child._dakkar_image.set_from_gicon(item.data.icon)
            else:
                child._dakkar_image.set_from_icon_name(
                    "folder-symbolic" if item.is_directory else "text-x-generic-symbolic"
                )
            self._apply_directory_tint_to_cell(child)
            if item.is_directory:
                self._queue_visible_directory_count(item)
            else:
                self._request_cell_thumbnail(child, item)

        def unbind(_factory, list_item):
            child = list_item.get_child()
            if child is not None:
                self._cancel_cell_thumbnail(child)
                item = getattr(child, "_dakkar_item", None)
                if isinstance(item, FileItem):
                    self._dequeue_visible_directory_count(item)
                child._dakkar_item = None

        def teardown(_factory, list_item):
            child = list_item.get_child()
            if child is not None:
                self._cancel_cell_thumbnail(child)
                self._list_cells.discard(child)

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        factory.connect("unbind", unbind)
        factory.connect("teardown", teardown)
        column = Gtk.ColumnViewColumn(title="Name", factory=factory)
        column.set_expand(True)
        return column

    def _expandable_name_column(self) -> Gtk.ColumnViewColumn:
        factory = Gtk.SignalListItemFactory()

        def setup(_factory, list_item):
            list_item.set_focusable(False)
            expander = Gtk.TreeExpander()
            box = Gtk.Box(spacing=8)
            image = self._create_item_icon(32)
            label = Gtk.Label(xalign=0.0, ellipsize=3)
            box.append(image)
            box.append(label)
            box._dakkar_image = image
            box._dakkar_label = label
            box._dakkar_uri = None
            box._dakkar_thumbnail_request = None
            expander.set_child(box)
            expander._dakkar_box = box
            expander._dakkar_item = None
            expander._dakkar_metadata_handler = None
            list_item.set_child(expander)
            self._list_cells.add(box)

        def bind(_factory, list_item):
            row = list_item.get_item()
            expander = list_item.get_child()
            if not isinstance(row, Gtk.TreeListRow) or expander is None:
                return
            item = row.get_item()
            if not isinstance(item, FileItem):
                return
            self._cancel_cell_thumbnail(expander._dakkar_box)
            self._disconnect_metadata_cell(expander)
            expander.set_list_row(row)
            self._connect_metadata_cell(
                expander,
                item,
                lambda changed_item, current=expander: (
                    self._update_expandable_expander(
                        current,
                        changed_item,
                    )
                ),
            )
            self._update_expandable_expander(expander, item)
            expander._dakkar_expansion_handler = row.connect(
                "notify::expanded",
                self._tree_row_expansion_changed,
            )
            box = expander._dakkar_box
            box._dakkar_uri = item.uri
            box._dakkar_item = item
            box._dakkar_label.set_text(item.display_name)
            if item.data.icon is not None:
                box._dakkar_image.set_from_gicon(item.data.icon)
            else:
                box._dakkar_image.set_from_icon_name(
                    "folder-symbolic"
                    if item.is_directory
                    else "text-x-generic-symbolic"
                )
            self._apply_directory_tint_to_cell(box)
            if item.is_directory:
                self._queue_visible_directory_count(item)
                if row.get_expanded():
                    self.expandable_model.ensure_loaded(item)
            else:
                self._request_cell_thumbnail(box, item)

        def unbind(_factory, list_item):
            expander = list_item.get_child()
            if expander is not None:
                self._cancel_cell_thumbnail(expander._dakkar_box)
                item = getattr(expander, "_dakkar_item", None)
                if isinstance(item, FileItem):
                    self._dequeue_visible_directory_count(item)
                self._disconnect_metadata_cell(expander)
                row = expander.get_list_row()
                handler = getattr(
                    expander,
                    "_dakkar_expansion_handler",
                    None,
                )
                if row is not None and handler is not None:
                    row.disconnect(handler)
                expander._dakkar_expansion_handler = None
                expander.set_list_row(None)
                expander._dakkar_box._dakkar_item = None

        def teardown(_factory, list_item):
            expander = list_item.get_child()
            if expander is not None:
                self._cancel_cell_thumbnail(expander._dakkar_box)
                self._disconnect_metadata_cell(expander)
                self._list_cells.discard(expander._dakkar_box)

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        factory.connect("unbind", unbind)
        factory.connect("teardown", teardown)
        column = Gtk.ColumnViewColumn(title="Name", factory=factory)
        column.set_expand(True)
        return column

    @staticmethod
    def _update_expandable_expander(
        expander: Gtk.TreeExpander,
        item: FileItem,
    ) -> None:
        if getattr(expander, "_dakkar_item", None) is not item:
            return
        if hasattr(expander, "set_hide_expander"):
            expander.set_hide_expander(
                item.is_directory and item.directory_count == 0
            )

    def _tree_row_expansion_changed(
        self,
        row: Gtk.TreeListRow,
        _parameter,
    ) -> None:
        if not row.get_expanded():
            self._tree_items_changed()
            return
        item = row.get_item()
        if isinstance(item, FileItem):
            self.expandable_model.ensure_loaded(item)
        self._tree_items_changed()

    def _expandable_text_column(
        self,
        title: str,
        getter: Callable[[FileItem], str],
        expand: bool,
    ) -> Gtk.ColumnViewColumn:
        factory = Gtk.SignalListItemFactory()

        def setup(_factory, list_item):
            label = Gtk.Label(xalign=0.0, ellipsize=3)
            label._dakkar_item = None
            label._dakkar_metadata_handler = None
            list_item.set_child(label)

        def bind(_factory, list_item):
            row = list_item.get_item()
            if not isinstance(row, Gtk.TreeListRow):
                return
            item = row.get_item()
            if isinstance(item, FileItem):
                child = list_item.get_child()
                self._connect_metadata_cell(
                    child,
                    item,
                    lambda changed_item, label=child: label.set_text(
                        getter(changed_item)
                    ),
                )
                child.set_text(getter(item))

        def unbind(_factory, list_item):
            child = list_item.get_child()
            if child is not None:
                self._disconnect_metadata_cell(child)
                child._dakkar_item = None

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        factory.connect("unbind", unbind)
        column = Gtk.ColumnViewColumn(title=title, factory=factory)
        column.set_expand(expand)
        return column

    def _text_column(
        self,
        title: str,
        getter: Callable[[FileItem], str],
        expand: bool,
    ) -> Gtk.ColumnViewColumn:
        factory = Gtk.SignalListItemFactory()

        def setup(_factory, list_item):
            label = Gtk.Label(xalign=0.0, ellipsize=3)
            label._dakkar_item = None
            label._dakkar_metadata_handler = None
            list_item.set_child(label)

        def bind(_factory, list_item):
            item = list_item.get_item()
            if isinstance(item, FileItem):
                child = list_item.get_child()
                self._connect_metadata_cell(
                    child,
                    item,
                    lambda changed_item, label=child: label.set_text(
                        getter(changed_item)
                    ),
                )
                child.set_text(getter(item))

        def unbind(_factory, list_item):
            child = list_item.get_child()
            if child is not None:
                self._disconnect_metadata_cell(child)
                child._dakkar_item = None

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        factory.connect("unbind", unbind)
        column = Gtk.ColumnViewColumn(title=title, factory=factory)
        column.set_expand(expand)
        return column

    def _compare_items(self, left: FileItem, right: FileItem, _data=None) -> Gtk.Ordering:
        if left.is_directory != right.is_directory:
            result = Gtk.Ordering.SMALLER if left.is_directory else Gtk.Ordering.LARGER
        elif self.sort_key == "size":
            result = self._compare_numbers(left.deep_size, right.deep_size)
        elif self.sort_key == "modified":
            result = self._compare_numbers(left.data.modified, right.data.modified)
        elif self.sort_key == "deep-modified":
            result = self._compare_numbers(left.deep_modified, right.deep_modified)
        elif self.sort_key == "type":
            result = _compare_strings(left.type_text, right.type_text)
        elif self.sort_key == "relevance":
            result = self._compare_numbers(left.relevance, right.relevance)
        else:
            result = _compare_collation_keys(
                left.display_name_collation_key,
                right.display_name_collation_key,
            )
        if self.sort_descending and left.is_directory == right.is_directory:
            if result == Gtk.Ordering.SMALLER:
                return Gtk.Ordering.LARGER
            if result == Gtk.Ordering.LARGER:
                return Gtk.Ordering.SMALLER
        return result

    @staticmethod
    def _compare_numbers(left: int | float, right: int | float) -> Gtk.Ordering:
        if left < right:
            return Gtk.Ordering.SMALLER
        if left > right:
            return Gtk.Ordering.LARGER
        return Gtk.Ordering.EQUAL

    def set_sort(self, key: str, descending: bool = False) -> None:
        if key not in SORT_KEYS:
            return
        expanded_uris = self.expandable_model.expanded_uris()
        self.sort_key = key
        self.sort_descending = descending
        self._refresh_directory_tints()
        self.sorter.changed(Gtk.SorterChange.DIFFERENT)
        self.expandable_model.queue_expansion_restore(expanded_uris)
        self.expandable_model.refresh_metadata()
        self._update_directory_metadata_requirement()
        self.window.tab_sort_changed(self)

    @property
    def directory_tint_settings(self) -> DirectoryTintSettings:
        return self._directory_tint.settings

    def set_directory_tint_settings(
        self,
        settings: DirectoryTintSettings,
    ) -> None:
        self._directory_tint.set_settings(settings)
        self._refresh_directory_tints()
        self.expandable_model.refresh_metadata()
        self._update_directory_metadata_requirement()

    def _requires_recursive_directory_metadata(self) -> bool:
        return (
            self.sort_key in {"deep-modified", "size"}
            or self._directory_tint.requires_recursive_metadata(self.sort_key)
        )

    def _update_directory_metadata_requirement(self) -> None:
        if self._searching:
            return
        inspect_recursively = self._requires_recursive_directory_metadata()
        if inspect_recursively == self._directory_metadata_recursive:
            return
        if inspect_recursively:
            self._start_directory_metadata(True)
        else:
            self._schedule_directory_counts(self._generation)

    def set_view(self, name: str) -> None:
        if name not in {"grid", "list", "expandable"}:
            return
        source_selection = self._active_selection()
        selected_uris = {item.uri for item in self.selected_items()}
        self.view_name = name
        self._update_visible_view()
        target_selection = self._active_selection()
        if target_selection is not source_selection:
            source_selection.unselect_all()
            target_selection.unselect_all()
            for position in range(target_selection.get_n_items()):
                item = self._file_item_from_model_item(
                    target_selection.get_item(position)
                )
                if item is not None and item.uri in selected_uris:
                    target_selection.select_item(position, False)
        self.grab_view_focus()

    def _update_visible_view(self) -> None:
        visible_name = (
            "list"
            if self.view_name == "expandable" and self._searching
            else self.view_name
        )
        self.stack.set_visible_child_name(visible_name)

    def grab_view_focus(self) -> None:
        visible = self.stack.get_visible_child()
        if isinstance(visible, Gtk.ScrolledWindow):
            child = visible.get_child()
            if child is not None:
                child.grab_focus()

    def set_zoom(self, delta: int) -> None:
        self.set_grid_layout(icon_size=self.grid_icon_size + delta)

    def set_grid_layout(
        self,
        padding: int | None = None,
        spacing: int | None = None,
        icon_size: int | None = None,
        show_full_item_names: bool | None = None,
    ) -> None:
        self.grid_padding = max(
            0,
            min(
                MAX_GRID_PADDING,
                padding if padding is not None else self.grid_padding,
            ),
        )
        self.grid_spacing = max(
            0,
            min(
                MAX_GRID_SPACING,
                spacing if spacing is not None else self.grid_spacing,
            ),
        )
        self.grid_icon_size = max(
            MIN_ICON_SIZE,
            min(
                MAX_ICON_SIZE,
                icon_size if icon_size is not None else self.grid_icon_size,
            ),
        )
        if show_full_item_names is not None:
            self.show_full_item_names = bool(show_full_item_names)
        self._update_grid_style()
        for cell in self._grid_cells:
            cell._dakkar_image.set_pixel_size(self.grid_icon_size)
            cell._dakkar_image.set_size_request(
                self.grid_icon_size,
                self.grid_icon_size,
            )
            self._configure_grid_label(cell._dakkar_label)
            cell.queue_resize()
        self.grid_view.queue_resize()

    def _configure_grid_label(self, label: Gtk.Label) -> None:
        """
        Apply the selected grid-name presentation to a label.
        """

        if self.show_full_item_names:
            label.set_lines(-1)
            label.set_ellipsize(Pango.EllipsizeMode.NONE)
            return
        label.set_lines(4)
        label.set_ellipsize(Pango.EllipsizeMode.END)

    def _update_grid_style(self) -> None:
        self._grid_style_provider.load_from_data(
            (
                "gridview {"
                f" padding: {self.grid_padding}px;"
                f" border-spacing: {self.grid_spacing}px;"
                " }"
            ).encode("utf-8")
        )

    def _store_directory_snapshot(self) -> None:
        if not self._directory_enumeration_complete:
            return
        key = (self.location.get_uri(), self._loaded_show_hidden)
        self._recent_directories[key] = _DirectorySnapshot(
            tuple(self._directory_data)
        )
        self._recent_directories.move_to_end(key)
        while self._recent_directories and (
            len(self._recent_directories) > RECENT_DIRECTORY_CACHE_SIZE
            or sum(
                len(snapshot.items)
                for snapshot in self._recent_directories.values()
            )
            > RECENT_DIRECTORY_CACHE_ITEM_LIMIT
        ):
            self._recent_directories.popitem(last=False)

    def _cached_directory_snapshot(
        self,
        location: Gio.File,
    ) -> _DirectorySnapshot | None:
        key = (location.get_uri(), self.show_hidden)
        snapshot = self._recent_directories.get(key)
        if snapshot is not None:
            self._recent_directories.move_to_end(key)
        return snapshot

    def _reset_directory_model(self, items: list[ItemData]) -> None:
        self._directory_data = list(items)
        self._directory_data_by_uri = {data.uri: data for data in items}
        self._directory_data_positions = {
            data.uri: position
            for position, data in enumerate(items)
        }
        self._directory_items_by_uri.clear()
        self.store.remove_all()
        visible_items = [
            FileItem(data)
            for data in items
            if self._matches_current_filters(data)
        ]
        if visible_items:
            self.store.splice(0, 0, visible_items)
            self._directory_items_by_uri.update(
                (item.uri, item)
                for item in visible_items
            )

    @staticmethod
    def _icon_identity(icon: Gio.Icon | None) -> str | None:
        return icon.to_string() if icon is not None else None

    @classmethod
    def _item_data_equal(cls, left: ItemData, right: ItemData) -> bool:
        return (
            left.uri == right.uri
            and left.name == right.name
            and left.display_name == right.display_name
            and left.is_directory == right.is_directory
            and left.is_symlink == right.is_symlink
            and left.is_hidden == right.is_hidden
            and left.is_executable == right.is_executable
            and left.content_type == right.content_type
            and left.size == right.size
            and left.modified == right.modified
            and left.accessed == right.accessed
            and left.created == right.created
            and cls._icon_identity(left.icon) == cls._icon_identity(right.icon)
            and left.thumbnail_path == right.thumbnail_path
        )

    def _remove_visible_directory_item(self, uri: str) -> bool:
        item = self._directory_items_by_uri.pop(uri, None)
        if item is None:
            return False
        found, position = self.store.find(item)
        if found:
            self.store.remove(position)
        self._pending_directory_count_uris.discard(uri)
        self._inflight_directory_count_uris.discard(uri)
        self.expandable_model.forget_uri(uri)
        return True

    def _remove_directory_data(self, uri: str) -> bool:
        position = self._directory_data_positions.pop(uri, None)
        if position is None:
            return False
        self._directory_data_by_uri.pop(uri, None)
        final_item = self._directory_data.pop()
        if position < len(self._directory_data):
            self._directory_data[position] = final_item
            self._directory_data_positions[final_item.uri] = position
        return True

    def _set_directory_data(self, data: ItemData) -> bool:
        current = self._directory_data_by_uri.get(data.uri)
        if current is not None and self._item_data_equal(current, data):
            return False
        position = self._directory_data_positions.get(data.uri)
        if position is None:
            self._directory_data_positions[data.uri] = len(self._directory_data)
            self._directory_data.append(data)
        else:
            self._directory_data[position] = data
        self._directory_data_by_uri[data.uri] = data
        return True

    def _upsert_visible_directory_item(self, data: ItemData) -> bool:
        current = self._directory_items_by_uri.get(data.uri)
        visible = self._matches_current_filters(data)
        if not visible:
            return self._remove_visible_directory_item(data.uri)
        if current is not None and self._item_data_equal(current.data, data):
            return False

        replacement = FileItem(data)
        if current is not None and current.is_directory and data.is_directory:
            replacement.directory_count = current.directory_count
            replacement.deep_modified = current.deep_modified
            replacement.deep_size = current.deep_size
        if current is None:
            self.store.append(replacement)
        else:
            found, position = self.store.find(current)
            if found:
                self.store.splice(position, 1, [replacement])
            else:
                self.store.append(replacement)
        self._directory_items_by_uri[data.uri] = replacement
        return True

    def _restore_selected_uris(self, uris: set[str]) -> None:
        if not uris:
            return
        selection = self._active_selection()
        selection.unselect_all()
        for position in range(selection.get_n_items()):
            item = self._file_item_from_model_item(selection.get_item(position))
            if item is not None and item.uri in uris:
                selection.select_item(position, False)

    def _reconcile_directory_data(self, items: list[ItemData]) -> None:
        selected_uris = {item.uri for item in self.selected_items()}
        current_by_uri = {data.uri: data for data in self._directory_data}
        replacement_by_uri = {data.uri: data for data in items}
        changed = False
        for uri in current_by_uri.keys() - replacement_by_uri.keys():
            changed = self._remove_visible_directory_item(uri) or changed
        for data in items:
            current = current_by_uri.get(data.uri)
            if current is None or not self._item_data_equal(current, data):
                changed = self._upsert_visible_directory_item(data) or changed
        self._directory_data = list(items)
        self._directory_data_by_uri = dict(replacement_by_uri)
        self._directory_data_positions = {
            data.uri: position
            for position, data in enumerate(items)
        }
        if not changed:
            return
        self._restore_selected_uris(selected_uris)
        self._refresh_directory_tints()
        self.expandable_model.restore_pending_expansions()

    def navigate(self, location: Gio.File, add_history: bool = True) -> None:
        self._store_directory_snapshot()
        restore_uris = (
            self.expandable_model.expanded_uris()
            if self.location.equal(location)
            else set()
        )
        self.cancel()
        self.expandable_model.reset(restore_uris)
        self._saved_expandable_uris.clear()
        self._scroll_to_start()
        self._user_scrolled_during_load = False
        self._reset_scroll_after_load = True
        self.location = location
        if add_history:
            self.history.push(location.get_uri())
        self._searching = False
        self._global_search = False
        self._update_visible_view()
        self._loaded_show_hidden = self.show_hidden
        snapshot = self._cached_directory_snapshot(location)
        self._showing_cached_directory = snapshot is not None
        self._incoming_directory_data = []
        self._reset_directory_model(
            list(snapshot.items) if snapshot is not None else []
        )
        self._pending_directory_count_uris.clear()
        self._inflight_directory_count_uris.clear()
        self._directory_enumeration_complete = False
        self._filesystem_details = None
        self._result_uris.clear()
        self._refresh_directory_tints()
        self._set_base_status(
            f"{self._directory_status()} · Refreshing…"
            if snapshot is not None
            else "Loading…"
        )
        self._generation = self._loader.load(
            location,
            self.show_hidden,
            self._directory_batch,
            self._directory_done,
        )
        self._start_monitor()
        self.window.tab_location_changed(self)

    def cancel(self) -> None:
        self._loader.cancel()
        self._begin_thumbnail_generation()
        self._monitor_update_serial += 1
        self._pending_monitor_queries.clear()
        self._pending_monitor_removals.clear()
        self._search_auto_select_pending = False
        self._cancel_scheduled_directory_counts()
        self._directory_metadata_loader.cancel()
        self._directory_metadata_generation = -1
        self._directory_metadata_recursive = False
        self._directory_enumeration_complete = False
        self._pending_directory_count_uris.clear()
        self._inflight_directory_count_uris.clear()
        self._cancel_selection_size()
        self._search.cancel()
        self._indexed_search.cancel()
        if self._search_timeout:
            GLib.source_remove(self._search_timeout)
            self._search_timeout = 0
        if self._monitor_timeout:
            GLib.source_remove(self._monitor_timeout)
            self._monitor_timeout = 0
        if self._tree_tint_refresh_timeout:
            GLib.source_remove(self._tree_tint_refresh_timeout)
            self._tree_tint_refresh_timeout = 0

    def refresh(self) -> None:
        if self._searching or self._loaded_show_hidden != self.show_hidden:
            self.navigate(self.location, add_history=False)
            return
        self._loader.cancel()
        self._cancel_scheduled_directory_counts()
        self._directory_metadata_loader.cancel()
        self._directory_metadata_generation = -1
        self._directory_metadata_recursive = False
        self._directory_enumeration_complete = False
        self._incoming_directory_data = []
        self._showing_cached_directory = True
        self._user_scrolled_during_load = False
        self._reset_scroll_after_load = False
        self._set_base_status(f"{self._directory_status()} · Refreshing…")
        self._generation = self._loader.load(
            self.location,
            self.show_hidden,
            self._directory_batch,
            self._directory_done,
        )

    def _directory_batch(self, generation: int, batch: list[ItemData]) -> None:
        if generation != self._generation or self._searching:
            return
        if self._showing_cached_directory:
            self._incoming_directory_data.extend(batch)
            return
        batch = [
            data
            for data in batch
            if data.uri not in self._directory_data_by_uri
        ]
        for data in batch:
            self._set_directory_data(data)
        items = [
            FileItem(data)
            for data in batch
            if self._matches_current_filters(data)
        ]
        if items:
            self.store.splice(self.store.get_n_items(), 0, items)
            self._directory_items_by_uri.update(
                (item.uri, item)
                for item in items
            )
        self._refresh_directory_tints()
        self.expandable_model.restore_pending_expansions()
        self._set_base_status(self._directory_status())

    def _directory_done(self, generation: int, error: Exception | None) -> None:
        if generation != self._generation or self._searching:
            return
        if self._showing_cached_directory:
            if error is None:
                self._reconcile_directory_data(self._incoming_directory_data)
            self._incoming_directory_data = []
            self._showing_cached_directory = False
        self._directory_enumeration_complete = True
        if error is not None:
            self._set_base_status(f"Unable to open location: {error}")
        elif not self._directory_data:
            self._set_base_status("This location is empty")
            self._query_filesystem_space(generation, "This location is empty")
        else:
            status = self._directory_status()
            self._set_base_status(status)
            self._query_filesystem_space(generation, status)
        if error is None:
            self._store_directory_snapshot()
            self._request_directory_metadata(generation)
        self.expandable_model.restore_pending_expansions()
        if (
            self._reset_scroll_after_load
            and not self._user_scrolled_during_load
        ):
            self.add_tick_callback(
                self._scroll_to_start_after_layout,
                {"generation": generation, "frames_remaining": 1},
            )
        selected_items = self.selected_items()
        if selected_items:
            self.add_tick_callback(
                self._scroll_to_uri_after_layout,
                {
                    "generation": generation,
                    "frames_remaining": 4,
                    "uri": selected_items[0].uri,
                },
            )

    def _scroll_to_position(self, position: int) -> None:
        if not hasattr(self.grid_view, "scroll_to"):
            return
        if self._active_selection() is self.expandable_selection:
            self.expandable_view.scroll_to(
                position,
                None,
                Gtk.ListScrollFlags.NONE,
                None,
            )
            return
        self.grid_view.scroll_to(position, Gtk.ListScrollFlags.NONE, None)
        self.list_view.scroll_to(position, None, Gtk.ListScrollFlags.NONE, None)

    def _scroll_to_start(self) -> None:
        if self.selection.get_n_items() > 0:
            self._scroll_to_position(0)
        for scrolled_window in (
            self.grid_scrolled_window,
            self.list_scrolled_window,
            self.expandable_scrolled_window,
        ):
            for adjustment in (
                scrolled_window.get_hadjustment(),
                scrolled_window.get_vadjustment(),
            ):
                adjustment.set_value(adjustment.get_lower())

    def _scroll_to_start_after_layout(
        self,
        _widget: Gtk.Widget,
        _frame_clock: Gdk.FrameClock,
        state: dict[str, int],
    ) -> bool:
        if (
            state["generation"] != self._generation
            or self._searching
            or self._user_scrolled_during_load
        ):
            return GLib.SOURCE_REMOVE
        self._scroll_to_start()
        state["frames_remaining"] -= 1
        return state["frames_remaining"] > 0

    def _scroll_to_uri_after_layout(
        self,
        _widget: Gtk.Widget,
        _frame_clock: Gdk.FrameClock,
        state: dict,
    ) -> bool:
        if state["generation"] != self._generation or self._searching:
            return GLib.SOURCE_REMOVE
        selection = self._active_selection()
        for position in range(selection.get_n_items()):
            item = self._file_item_from_model_item(selection.get_item(position))
            if item is not None and item.uri == state["uri"]:
                self._scroll_to_position(position)
                break
        state["frames_remaining"] -= 1
        return state["frames_remaining"] > 0

    def _start_directory_metadata(
        self,
        inspect_recursively: bool,
        only_missing_counts: bool = False,
        allowed_uris: set[str] | None = None,
    ) -> None:
        self._cancel_scheduled_directory_counts()
        self._directory_metadata_loader.cancel()
        self._directory_metadata_generation = -1
        self._directory_metadata_recursive = inspect_recursively
        self._inflight_directory_count_uris.clear()
        directories = [
            data
            for data in self._directory_data
            if (
                data.is_directory
                and not data.is_symlink
                and self._matches_current_filters(data)
                and data.uri in self._directory_items_by_uri
                and (allowed_uris is None or data.uri in allowed_uris)
                and (
                    not only_missing_counts
                    or self._directory_items_by_uri[data.uri].directory_count
                    is None
                )
            )
        ]
        if not directories:
            return
        self._inflight_directory_count_uris.update(
            data.uri
            for data in directories
        )
        self._directory_metadata_generation = self._directory_metadata_loader.load(
            directories,
            inspect_recursively,
            self._directory_metadata_ready,
            self._directory_metadata_done,
        )

    def _request_directory_metadata(self, generation: int) -> None:
        if self._requires_recursive_directory_metadata():
            self._start_directory_metadata(True)
        else:
            self._schedule_directory_counts(generation)

    def _schedule_directory_counts(
        self,
        generation: int,
        delay_ms: int = DIRECTORY_COUNT_DELAY_MS,
    ) -> None:
        self._cancel_scheduled_directory_counts()
        self._directory_metadata_loader.cancel()
        self._directory_metadata_generation = -1
        self._directory_metadata_recursive = False
        self._inflight_directory_count_uris.clear()
        self._directory_metadata_timeout = GLib.timeout_add(
            delay_ms,
            self._start_scheduled_directory_counts,
            generation,
        )

    def _cancel_scheduled_directory_counts(self) -> None:
        if self._directory_metadata_timeout:
            GLib.source_remove(self._directory_metadata_timeout)
            self._directory_metadata_timeout = 0

    def _start_scheduled_directory_counts(self, generation: int) -> bool:
        self._directory_metadata_timeout = 0
        if (
            generation != self._generation
            or self._searching
            or self._requires_recursive_directory_metadata()
        ):
            return GLib.SOURCE_REMOVE
        requested_uris = self._visible_root_directory_uris()
        self._pending_directory_count_uris.clear()
        self._start_directory_metadata(
            False,
            only_missing_counts=True,
            allowed_uris=requested_uris,
        )
        return GLib.SOURCE_REMOVE

    def _visible_root_directory_uris(self) -> set[str]:
        if self.view_name == "grid":
            cells = self._grid_cells
            viewport = self.grid_scrolled_window
        elif self.view_name == "expandable":
            cells = self._list_cells
            viewport = self.expandable_scrolled_window
        else:
            cells = self._list_cells
            viewport = self.list_scrolled_window
        uris: set[str] = set()
        for cell in cells:
            item = getattr(cell, "_dakkar_item", None)
            if (
                isinstance(item, FileItem)
                and item.is_directory
                and self._directory_items_by_uri.get(item.uri) is item
                and self._cell_intersects_viewport(cell, viewport)
            ):
                uris.add(item.uri)
        return uris

    @staticmethod
    def _cell_intersects_viewport(
        cell: Gtk.Widget,
        viewport: Gtk.ScrolledWindow,
    ) -> bool:
        if not cell.get_mapped():
            return False
        success, bounds = cell.compute_bounds(viewport)
        if not success:
            return False
        return (
            bounds.get_x() + bounds.get_width() > 0
            and bounds.get_y() + bounds.get_height() > 0
            and bounds.get_x() < viewport.get_allocated_width()
            and bounds.get_y() < viewport.get_allocated_height()
        )

    def _directory_view_scrolled(self, _adjustment: Gtk.Adjustment) -> None:
        if (
            not self._directory_enumeration_complete
            or self._searching
            or self._requires_recursive_directory_metadata()
        ):
            return
        for uri in self._visible_root_directory_uris():
            item = self._directory_items_by_uri.get(uri)
            if (
                item is not None
                and item.directory_count is None
                and uri not in self._inflight_directory_count_uris
            ):
                self._pending_directory_count_uris.add(uri)
        if (
            self._pending_directory_count_uris
            and not self._directory_metadata_timeout
            and self._directory_metadata_generation == -1
        ):
            self._schedule_directory_counts(
                self._generation,
                VISIBLE_DIRECTORY_COUNT_DELAY_MS,
            )

    def _queue_visible_directory_count(self, item: FileItem) -> None:
        if (
            not item.is_directory
            or item.directory_count is not None
            or self._directory_items_by_uri.get(item.uri) is not item
            or item.uri in self._inflight_directory_count_uris
        ):
            return
        self._pending_directory_count_uris.add(item.uri)
        if (
            not self._directory_enumeration_complete
            or self._searching
            or self._requires_recursive_directory_metadata()
            or self._directory_metadata_timeout
            or self._directory_metadata_generation != -1
        ):
            return
        self._schedule_directory_counts(
            self._generation,
            VISIBLE_DIRECTORY_COUNT_DELAY_MS,
        )

    def _dequeue_visible_directory_count(self, item: FileItem) -> None:
        if item.uri not in self._inflight_directory_count_uris:
            self._pending_directory_count_uris.discard(item.uri)

    def _directory_metadata_ready(
        self,
        generation: int,
        metadata: list[tuple[str, int | None, int, int]],
    ) -> None:
        if generation != self._directory_metadata_generation or self._searching:
            return
        expanded_uris = self.expandable_model.expanded_uris()
        restore_uris: set[str] = set()
        changed = False
        for uri, count, deep_modified, deep_size in metadata:
            self._inflight_directory_count_uris.discard(uri)
            item = self._directory_items_by_uri.get(uri)
            if item is None:
                continue
            item.set_directory_metadata(count, deep_modified, deep_size)
            changed = True
            if uri in expanded_uris:
                restore_uris.add(uri)
        if not changed:
            return
        self._refresh_directory_tints()
        if restore_uris:
            self.expandable_model.queue_expansion_restore(restore_uris)
        if self.sort_key in {"deep-modified", "size"}:
            self.sorter.changed(Gtk.SorterChange.DIFFERENT)

    def _directory_metadata_done(
        self,
        generation: int,
        _error: Exception | None,
    ) -> None:
        if generation != self._directory_metadata_generation:
            return
        self._directory_metadata_generation = -1
        self._inflight_directory_count_uris.clear()
        if self.sort_key in {"deep-modified", "size"}:
            self.sorter.changed(Gtk.SorterChange.DIFFERENT)
        elif self._pending_directory_count_uris:
            self._schedule_directory_counts(
                self._generation,
                VISIBLE_DIRECTORY_COUNT_DELAY_MS,
            )

    def _query_filesystem_space(self, generation: int, status: str) -> None:
        location = self.location

        def run() -> None:
            try:
                info = location.query_filesystem_info(
                    ",".join(
                        (
                            Gio.FILE_ATTRIBUTE_FILESYSTEM_FREE,
                            Gio.FILE_ATTRIBUTE_FILESYSTEM_SIZE,
                            Gio.FILE_ATTRIBUTE_FILESYSTEM_TYPE,
                        )
                    ),
                    None,
                )
                free = info.get_attribute_uint64(Gio.FILE_ATTRIBUTE_FILESYSTEM_FREE)
                size = info.get_attribute_uint64(Gio.FILE_ATTRIBUTE_FILESYSTEM_SIZE)
                filesystem_type = info.get_attribute_string(Gio.FILE_ATTRIBUTE_FILESYSTEM_TYPE)
            except GLib.Error:
                return
            GLib.idle_add(
                self._set_filesystem_status,
                generation,
                status,
                free,
                size,
                filesystem_type,
            )

        threading.Thread(target=run, name="dakkar-filesystem-info", daemon=True).start()

    def _set_filesystem_status(
        self,
        generation: int,
        _status: str,
        free: int,
        size: int,
        filesystem_type: str | None,
    ) -> bool:
        if generation != self._generation or self._searching:
            return GLib.SOURCE_REMOVE
        details = f"{GLib.format_size(free)} free"
        if size:
            details += f" of {GLib.format_size(size)}"
        if filesystem_type:
            details += f" · {filesystem_type}"
        self._filesystem_details = details
        self._set_base_status(self._directory_status())
        return GLib.SOURCE_REMOVE

    def _start_monitor(self) -> None:
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None
        try:
            self._monitor = self.location.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            self._monitor.connect("changed", self._monitor_changed)
        except GLib.Error:
            self._monitor = None

    def _is_direct_child(self, file: Gio.File) -> bool:
        parent = file.get_parent()
        return parent is not None and parent.equal(self.location)

    def _queue_monitor_query(self, file: Gio.File) -> None:
        if not self._is_direct_child(file):
            return
        uri = file.get_uri()
        self._pending_monitor_removals.discard(uri)
        self._pending_monitor_queries[uri] = file

    def _queue_monitor_removal(self, file: Gio.File) -> None:
        if not self._is_direct_child(file):
            return
        uri = file.get_uri()
        self._pending_monitor_queries.pop(uri, None)
        self._pending_monitor_removals.add(uri)

    def _schedule_monitor_update(self) -> None:
        if self._monitor_timeout:
            GLib.source_remove(self._monitor_timeout)
        self._monitor_timeout = GLib.timeout_add(
            MONITOR_UPDATE_DELAY_MS,
            self._flush_monitor_updates,
        )

    def _monitor_changed(
        self,
        _monitor: Gio.FileMonitor,
        file: Gio.File,
        other_file: Gio.File | None,
        event: Gio.FileMonitorEvent,
    ) -> None:
        if self._searching:
            return
        if event in {
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.MOVED_OUT,
        }:
            self._queue_monitor_removal(file)
        elif event in {
            Gio.FileMonitorEvent.MOVED,
            Gio.FileMonitorEvent.RENAMED,
        }:
            self._queue_monitor_removal(file)
            if other_file is not None:
                self._queue_monitor_query(other_file)
        elif event in {
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.CHANGED,
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.ATTRIBUTE_CHANGED,
            Gio.FileMonitorEvent.MOVED_IN,
        }:
            self._queue_monitor_query(file)
        else:
            return
        self._schedule_monitor_update()

    def files_changed(self, files: list[Gio.File]) -> None:
        """
        Queue operation results through the monitored-item update path.
        """

        if self._searching:
            return
        for file in files:
            self._queue_monitor_query(file)
        if self._pending_monitor_queries:
            self._schedule_monitor_update()

    def files_replaced(self, files: list[Gio.File]) -> None:
        """
        Refresh replacements in directory, search and loaded expandable views.
        """

        for file in files:
            parent = file.get_parent()
            if parent is not None:
                self._recent_directories.pop(parent.get_uri(), None)
        if not self._searching:
            self.files_changed(files)
            self.expandable_model.files_changed(files)
            return
        serial = self._search_serial
        visible = [file for file in files if file.get_uri() in self._result_uris]
        if not visible:
            return

        def apply(updates: list[ItemData]) -> bool:
            if serial != self._search_serial or not self._searching:
                return GLib.SOURCE_REMOVE
            selected_uris = {item.uri for item in self.selected_items()}
            by_uri = {data.uri: data for data in updates}
            for position in range(self.store.get_n_items() - 1, -1, -1):
                item = self.store.get_item(position)
                data = by_uri.get(item.uri)
                if data is None:
                    continue
                if self._matches_current_filters(data):
                    self.store.splice(position, 1, [FileItem(data, item.relevance)])
                else:
                    self.store.remove(position)
                    self._result_uris.discard(item.uri)
                    self._removed_search_uris.add(item.uri)
            self._restore_selected_uris(selected_uris)
            self._refresh_directory_tints()
            self._set_base_status(f"{len(self._result_uris)} filename matches")
            return GLib.SOURCE_REMOVE

        def query() -> None:
            updates = []
            for file in visible:
                try:
                    updates.append(query_item_data(file))
                except GLib.Error:
                    continue
            GLib.idle_add(apply, updates)

        threading.Thread(target=query, name="dakkar-replacement-update", daemon=True).start()

    def files_removed(self, files: list[Gio.File]) -> None:
        """
        Reflect completed removals in directory, search and expanded-branch views.
        """

        removed_uris = {file.get_uri() for file in files}
        for file in files:
            parent = file.get_parent()
            if parent is not None:
                self._recent_directories.pop(parent.get_uri(), None)
            self._remove_directory_data(file.get_uri())
        self._incoming_directory_data = [
            data for data in self._incoming_directory_data if data.uri not in removed_uris
        ]
        if self._searching:
            # Suppress stale batches already queued by either search provider.
            self._removed_search_uris.update(removed_uris)
            self._result_uris.difference_update(removed_uris)
            for position in range(self.store.get_n_items() - 1, -1, -1):
                if self.store.get_item(position).uri in removed_uris:
                    self.store.remove(position)
            self._refresh_directory_tints()
            self._set_base_status(f"{len(self._result_uris)} filename matches")
        else:
            self.files_changed(files)
            self.expandable_model.files_changed(files)

    def _flush_monitor_updates(self) -> bool:
        self._monitor_timeout = 0
        generation = self._generation
        serial = self._monitor_update_serial
        files = list(self._pending_monitor_queries.values())
        removals = set(self._pending_monitor_removals)
        self._pending_monitor_queries.clear()
        self._pending_monitor_removals.clear()
        if removals:
            self._apply_monitor_updates(generation, serial, [], removals)
        if not files:
            return GLib.SOURCE_REMOVE

        def query() -> None:
            updates: list[ItemData] = []
            missing: set[str] = set()
            for file in files:
                try:
                    updates.append(query_item_data(file))
                except GLib.Error as error:
                    if error.matches(
                        Gio.io_error_quark(),
                        Gio.IOErrorEnum.NOT_FOUND,
                    ):
                        missing.add(file.get_uri())
            GLib.idle_add(
                self._monitor_updates_ready,
                generation,
                serial,
                updates,
                missing,
            )

        threading.Thread(
            target=query,
            name="dakkar-monitor-update",
            daemon=True,
        ).start()
        return GLib.SOURCE_REMOVE

    def _monitor_updates_ready(
        self,
        generation: int,
        serial: int,
        updates: list[ItemData],
        removals: set[str],
    ) -> bool:
        self._apply_monitor_updates(generation, serial, updates, removals)
        return GLib.SOURCE_REMOVE

    def _apply_monitor_updates(
        self,
        generation: int,
        serial: int,
        updates: list[ItemData],
        removals: set[str],
    ) -> None:
        if (
            generation != self._generation
            or serial != self._monitor_update_serial
            or self._searching
        ):
            return
        selected_uris = {item.uri for item in self.selected_items()}
        changed = False
        for uri in removals:
            changed = self._remove_directory_data(uri) or changed
            changed = self._remove_visible_directory_item(uri) or changed

        for data in updates:
            if data.is_hidden and not self.show_hidden:
                changed = self._remove_directory_data(data.uri) or changed
                changed = (
                    self._remove_visible_directory_item(data.uri)
                    or changed
                )
                continue
            if not self._set_directory_data(data):
                continue
            changed = self._upsert_visible_directory_item(data) or changed

        if self._showing_cached_directory:
            incoming_by_uri = {
                data.uri: data
                for data in self._incoming_directory_data
                if data.uri not in removals
            }
            incoming_by_uri.update(
                (data.uri, data)
                for data in updates
                if self.show_hidden or not data.is_hidden
            )
            self._incoming_directory_data = list(incoming_by_uri.values())

        if not changed:
            return
        self._restore_selected_uris(selected_uris)
        self._refresh_directory_tints()
        self._set_base_status(self._directory_status())
        if self._directory_enumeration_complete:
            self._request_directory_metadata(self._generation)

    def search_later(self, text: str, global_search: bool = False) -> None:
        if self._search_timeout:
            GLib.source_remove(self._search_timeout)
        self._search_timeout = GLib.timeout_add(
            SEARCH_DELAY_MS,
            self._begin_search,
            text,
            global_search,
        )

    def apply_filters(self) -> None:
        """
        Apply the selected metadata filters to the loaded directory view.
        """

        if self._searching:
            return
        restore_uris = (
            self._saved_expandable_uris
            or self.expandable_model.expanded_uris()
        )
        self._saved_expandable_uris = set()
        self.expandable_model.reset(restore_uris)
        self._cancel_scheduled_directory_counts()
        self._directory_metadata_loader.cancel()
        self._directory_metadata_generation = -1
        self._directory_metadata_recursive = False
        self.store.remove_all()
        items = [
            FileItem(data)
            for data in self._directory_data
            if self._matches_current_filters(data)
        ]
        self._directory_items_by_uri = {
            item.uri: item
            for item in items
        }
        self._pending_directory_count_uris.clear()
        self._inflight_directory_count_uris.clear()
        if items:
            self.store.splice(0, 0, items)
        self._refresh_directory_tints()
        self.expandable_model.restore_pending_expansions()
        self._set_base_status(self._directory_status())
        self._request_directory_metadata(self._generation)

    def _filter_after_timestamp(self) -> int | None:
        days = {"today": 1, "week": 7, "month": 31, "year": 366}
        if self.search_period not in days:
            return None
        return int(
            (datetime.now() - timedelta(days=days[self.search_period])).timestamp()
        )

    def _matches_current_filters(self, data: ItemData) -> bool:
        return matches_filters(
            data,
            self.search_mime_group,
            self.search_date_field,
            self._filter_after_timestamp(),
        )

    def _directory_status(self) -> str:
        visible = self.store.get_n_items()
        total = len(self._directory_data)
        if total == 0:
            status = "This location is empty"
        elif visible == total:
            status = f"{total} items"
        else:
            status = f"{visible} of {total} items match filters"
        if self._filesystem_details:
            status += f" · {self._filesystem_details}"
        return status

    def _set_base_status(self, text: str) -> None:
        self._base_status = text
        if not self.selected_items():
            self.status_label.set_text(text)

    def _cancel_selection_size(self) -> None:
        self._selection_size_serial += 1
        self._selection_size_loader.cancel()
        if self._selection_size_timeout:
            GLib.source_remove(self._selection_size_timeout)
            self._selection_size_timeout = 0

    def _selection_changed(
        self,
        selection: Gtk.SelectionModel,
        _position: int,
        _number: int,
    ) -> None:
        if selection is not self._active_selection():
            return
        self._cancel_selection_size()
        items = self.selected_items()
        self.window.tab_selection_changed(self)
        if not items:
            self.status_label.set_text(self._base_status)
            return

        count = len(items)
        known_size = sum(item.data.size for item in items if not item.is_directory)
        directories = [item for item in items if item.is_directory]
        if not directories:
            self.status_label.set_text(
                self._selected_status(count, known_size)
            )
            return

        self.status_label.set_text(
            self._selected_status(count, known_size, calculating=True)
        )
        serial = self._selection_size_serial
        selected_data = [item.data for item in items]
        self._selection_size_timeout = GLib.timeout_add(
            SELECTION_SIZE_DELAY_MS,
            self._begin_selection_size,
            serial,
            selected_data,
        )

    @staticmethod
    def _selected_status(
        count: int,
        size: int,
        calculating: bool = False,
        incomplete: bool = False,
    ) -> str:
        noun = "item" if count == 1 else "items"
        if calculating:
            if size:
                detail = (
                    f"{GLib.format_size(size)} so far; "
                    "calculating total size..."
                )
            else:
                detail = "calculating total size..."
        elif incomplete:
            detail = (
                f"{GLib.format_size(size)}; "
                "some directory contents could not be read"
            )
        else:
            detail = GLib.format_size(size)
        return f"{count} {noun} selected ({detail})"

    def _begin_selection_size(
        self,
        serial: int,
        selected_data: list[ItemData],
    ) -> bool:
        self._selection_size_timeout = 0
        if serial != self._selection_size_serial:
            return GLib.SOURCE_REMOVE
        selected_uris = {item.uri for item in self.selected_items()}
        requested_uris = {item.uri for item in selected_data}
        if selected_uris != requested_uris:
            return GLib.SOURCE_REMOVE
        self._selection_size_loader.load(
            selected_data,
            lambda _generation, total, incomplete: self._selection_size_done(
                serial,
                total,
                incomplete,
            ),
        )
        return GLib.SOURCE_REMOVE

    def _selection_size_done(
        self,
        serial: int,
        total: int,
        incomplete: bool,
    ) -> None:
        if serial != self._selection_size_serial:
            return
        count = len(self.selected_items())
        if count:
            self.status_label.set_text(
                self._selected_status(count, total, incomplete=incomplete)
            )

    def _begin_search(self, text: str, global_search: bool) -> bool:
        self._search_timeout = 0
        query = text.strip()
        if not query:
            self._search_auto_select_pending = False
            if self._searching:
                self._searching = False
                self._global_search = False
                self._update_visible_view()
                self.set_sort("name")
                self.apply_filters()
            return GLib.SOURCE_REMOVE

        if not self._searching:
            self._saved_expandable_uris = self.expandable_model.expanded_uris()
            self.expandable_model.reset()
        self._searching = True
        self._update_visible_view()
        self._cancel_scheduled_directory_counts()
        self._directory_metadata_loader.cancel()
        self._directory_metadata_generation = -1
        self._directory_metadata_recursive = False
        self._global_search = global_search
        self._search_serial += 1
        self._removed_search_uris.clear()
        serial = self._search_serial
        self._search_auto_select_pending = True
        self._result_uris.clear()
        self._directory_items_by_uri.clear()
        self._pending_directory_count_uris.clear()
        self._inflight_directory_count_uris.clear()
        self.store.remove_all()
        self._refresh_directory_tints()
        self.set_sort("relevance", descending=True)
        root = Gio.File.new_for_path(GLib.get_home_dir()) if global_search else self.location
        immediate = () if global_search else tuple(self._directory_data)
        after_timestamp = self._filter_after_timestamp()
        self._set_base_status(
            f"Searching filenames below {'Home' if global_search else self.location.get_parse_name()}…"
        )
        self._search.start(
            root,
            query,
            self.show_hidden,
            True,
            lambda _generation, batch: self._search_batch(serial, batch),
            lambda _generation, error: self._search_done(serial, error, "direct"),
            immediate,
            self.search_mime_group,
            self.search_date_field,
            after_timestamp,
        )
        self._indexed_search.start(
            None if global_search else self.location,
            query,
            lambda _generation, batch: self._search_batch(serial, batch),
            lambda _generation, error: self._search_done(serial, error, "index"),
            self.search_mime_group,
            self.search_date_field,
            after_timestamp,
        )
        return GLib.SOURCE_REMOVE

    def _search_batch(self, serial: int, batch: list[SearchResult]) -> None:
        if serial != self._search_serial or not self._searching:
            return
        items: list[FileItem] = []
        for result in batch:
            if result.data.uri in self._result_uris or result.data.uri in self._removed_search_uris:
                continue
            self._result_uris.add(result.data.uri)
            items.append(FileItem(result.data, result.score))
        if items:
            self.store.splice(self.store.get_n_items(), 0, items)
            if (
                self._search_auto_select_pending
                and self.selection.get_n_items() > 0
            ):
                self.selection.unselect_all()
                self.selection.select_item(0, True)
                self._search_auto_select_pending = False
        self._refresh_directory_tints()
        self._set_base_status(f"{len(self._result_uris)} filename matches")

    def _search_done(
        self,
        serial: int,
        error: Exception | None,
        search_source: str,
    ) -> None:
        if serial != self._search_serial or not self._searching:
            return
        if search_source == "direct" and not self._result_uris and error is not None:
            self._set_base_status(f"Search failed: {error}")
        elif search_source == "direct" and not self._result_uris:
            self._set_base_status("No matching filenames")

    def _selected_items_from(
        self,
        selection: Gtk.MultiSelection,
    ) -> list[FileItem]:
        bitset = selection.get_selection()
        items: list[FileItem] = []
        for index in range(bitset.get_size()):
            item = self._file_item_from_model_item(
                selection.get_item(bitset.get_nth(index))
            )
            if item is not None:
                items.append(item)
        return items

    def selected_items(self) -> list[FileItem]:
        return self._selected_items_from(self._active_selection())

    def selected_files(self) -> list[Gio.File]:
        return [item.file for item in self.selected_items()]

    def select_all(self) -> None:
        self._active_selection().select_all()

    def _activate_position(self, view, position: int) -> None:
        selection = self._selection_for_view(view)
        item = self._file_item_from_model_item(selection.get_item(position))
        if item is None:
            return
        if item.is_directory:
            self.navigate(item.file)
        elif self._searching:
            self.window.open_file(item.file)
        else:
            self.window.open_file(item.file)

    def go_back(self) -> None:
        departing_location = self.location
        uri = self.history.back()
        if uri:
            destination = Gio.File.new_for_uri(uri)
            departing_parent = departing_location.get_parent()
            select_departing_directory = (
                departing_parent is not None
                and departing_parent.equal(destination)
            )
            self.navigate(destination, add_history=False)
            if select_departing_directory:
                self.select_uri_when_loaded(departing_location.get_uri())

    def go_forward(self) -> None:
        uri = self.history.forward()
        if uri:
            self.navigate(Gio.File.new_for_uri(uri), add_history=False)

    def go_up(self) -> None:
        parent = self.location.get_parent()
        if parent is not None:
            self.navigate(parent)

    def go_home(self) -> None:
        self.navigate(Gio.File.new_for_path(GLib.get_home_dir()))

    def selected_item_location(self) -> tuple[Gio.File, str] | None:
        items = self.selected_items()
        if len(items) != 1:
            return None
        parent = items[0].file.get_parent()
        if parent is None:
            return None
        return parent, items[0].uri

    def reveal_selected_location(self) -> None:
        target = self.selected_item_location()
        if target is None:
            return
        parent, item_uri = target
        self.navigate(parent)
        self.select_uri_when_loaded(item_uri)

    def select_uri_when_loaded(
        self,
        uri: str,
        show_properties: bool = False,
        attempts: int = 50,
    ) -> None:
        selection = self._active_selection()
        for position in range(selection.get_n_items()):
            item = self._file_item_from_model_item(selection.get_item(position))
            if item is not None and item.uri == uri:
                selection.unselect_all()
                selection.select_item(position, True)
                self._scroll_to_position(position)
                self.add_tick_callback(
                    self._scroll_to_uri_after_layout,
                    {
                        "generation": self._generation,
                        "frames_remaining": 4,
                        "uri": uri,
                    },
                )
                if show_properties:
                    self.window._properties()
                return
        if attempts > 0:
            GLib.timeout_add(
                URI_SELECTION_RETRY_DELAY_MS,
                self._retry_select_uri,
                uri,
                show_properties,
                attempts - 1,
            )

    def _retry_select_uri(self, uri: str, show_properties: bool, attempts: int) -> bool:
        self.select_uri_when_loaded(uri, show_properties, attempts)
        return GLib.SOURCE_REMOVE

    def dispose_tab(self) -> None:
        self.operation_panel.dispose_panel()
        self.cancel()
        self._thumbnails.release_owner(self._thumbnail_owner_id)
        self.expandable_model.dispose()
        self.grid_view.get_style_context().remove_provider(self._grid_style_provider)
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None

    def toggle_operation_panel(self) -> None:
        """
        Show or hide shared operation details beneath this file view.
        """

        button = self.operation_panel.toggle_button
        button.set_active(not button.get_active())
