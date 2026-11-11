"""
Provide the Dakkar application window and user actions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import subprocess
import threading

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from .archive import launch_archive_manager
from .bookmarks import load_bookmarks
from .browser import (
    DEFAULT_GRID_PADDING,
    DEFAULT_GRID_SPACING,
    DEFAULT_ICON_SIZE,
    DEFAULT_SHOW_FULL_ITEM_NAMES,
    DEFAULT_SORT_KEY,
    MAX_GRID_PADDING,
    MAX_GRID_SPACING,
    MAX_ICON_SIZE,
    MIN_ICON_SIZE,
    SORT_KEYS,
    BrowserTab,
    sort_is_descending,
)
from .directory_tint import (
    DEFAULT_TINT_SETTINGS,
    TINT_EDGES,
    TINT_SCOPES,
    TINT_SOURCES,
    DirectoryTintSettings,
    TintChannelSettings,
)
from .duplicate_dialog import DuplicateDialog
from .duplicates import DuplicateTrashResult, is_trash_path
from .empty_file_dialog import EmptyFileDialog
from .empty_files import RestoreResult
from .launching import common_applications, group_items_by_default_application
from .metadata import read_media_metadata
from .models import FileItem
from .operations import (
    ClipboardState,
    ConflictDecision,
    OperationService,
    TransferConflict,
)
from .saved_searches import SavedSearch, SavedSearchStore
from .terminal import terminal_program_arguments
from .thumbnails import ThumbnailService
from .undo import CannotUndoError, UndoableAction, UndoManager


MOUSE_BACK_BUTTON = 8
MOUSE_FORWARD_BUTTON = 9
MAX_CLOSED_TABS = 20
KEYBOARD_SHORTCUTS = (
    ("Ctrl+F", "Search below the displayed directory"),
    ("Ctrl+Shift+F", "Search below Home"),
    ("Escape", "Clear a filename search"),
    ("Ctrl+L", "Edit the displayed location"),
    ("Alt+Left", "Navigate back"),
    ("Alt+Right", "Navigate forward"),
    ("Alt+Up", "Open the parent directory"),
    ("Alt+Home", "Open Home"),
    ("Ctrl+1", "Use list view"),
    ("Ctrl+2", "Use grid view"),
    ("Ctrl+3", "Use expandable list view"),
    ("Ctrl+H", "Show or hide hidden files"),
    ("Ctrl+T", "Open a tab"),
    ("Ctrl+W", "Close a tab"),
    ("Ctrl+Shift+T", "Restore the most recently closed tab"),
    ("Ctrl+N", "Open another window"),
    ("Ctrl+Shift+N", "Create a directory"),
    ("F2", "Rename the selected item"),
    ("Ctrl+C", "Copy selected items"),
    ("Ctrl+X", "Cut selected items"),
    ("Ctrl+V", "Paste copied or cut items"),
    ("Ctrl+Z", "Undo a safe operation"),
    ("Ctrl+Shift+Z", "Redo a safe operation"),
    ("Delete", "Move selected items to Trash"),
    ("Shift+Delete", "Delete selected items permanently"),
    ("F5 or Ctrl+R", "Refresh the displayed location"),
    ("F9", "Show or hide Places"),
    ("Ctrl+Shift+O", "Show or hide file operations"),
)


@dataclass(frozen=True, slots=True)
class ClosedTabState:
    location_uri: str
    history_uris: tuple[str, ...]
    history_position: int
    pane_index: int
    page_index: int
    view_name: str
    sort_key: str
    sort_descending: bool
    show_hidden: bool
    grid_padding: int
    grid_spacing: int
    grid_icon_size: int
    show_full_item_names: bool
    search_mime_group: str
    search_date_field: str
    search_period: str
    tint_settings: DirectoryTintSettings


class DakkarWindow(Gtk.ApplicationWindow):
    def __init__(
        self,
        application: Gtk.Application,
        use_index: bool = True,
        high_contrast: bool = True,
        directory_tint: bool = False,
    ) -> None:
        super().__init__(application=application, title="Dakkar")
        self.set_default_size(1100, 720)
        self.set_size_request(560, 360)
        if high_contrast:
            self.add_css_class("dakkar-high-contrast")
        self.use_index = use_index
        self.directory_tint = directory_tint
        self.operations = OperationService()
        self.clipboard = ClipboardState()
        self.thumbnails = ThumbnailService()
        self.saved_searches = SavedSearchStore()
        self.undo_manager = UndoManager()
        self._duplicate_dialogs: set[DuplicateDialog] = set()
        self._empty_file_dialogs: set[EmptyFileDialog] = set()
        self._closing = False
        self._sidebar_rows: list[Gtk.ListBoxRow] = []
        self._path_buttons: list[Gtk.Widget] = []
        self._closed_tabs: list[ClosedTabState] = []
        self._places_width = 225
        self._volume_monitor = Gio.VolumeMonitor.get()
        self._volume_monitor_handlers: list[int] = []
        self._updating_filter_controls = False
        self._updating_grid_controls = False
        self._updating_sort_controls = False

        self.header = self._create_header_bar()
        self.set_titlebar(self.header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)
        root.append(self._create_navigation_bar())

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        self.content_paned = paned
        paned.set_position(self._places_width)
        self.places_panel = self._create_sidebar()
        paned.set_start_child(self.places_panel)
        self.pane_split = Gtk.Paned(
            orientation=Gtk.Orientation.HORIZONTAL,
            wide_handle=True,
            hexpand=True,
            vexpand=True,
        )
        self.pane_notebooks: list[Gtk.Notebook] = []
        primary_notebook = self._create_pane_notebook()
        self.pane_notebooks.append(primary_notebook)
        self.notebook = primary_notebook
        self.notebook.add_css_class("dakkar-active-pane")
        self.pane_split.set_start_child(primary_notebook)
        paned.set_end_child(self.pane_split)
        root.append(paned)
        self.search_entry.set_key_capture_widget(paned)

        for signal_name in ("mount-added", "mount-removed", "mount-changed"):
            self._volume_monitor_handlers.append(
                self._volume_monitor.connect(signal_name, self._mounts_changed)
            )

        self._install_actions()
        self._install_mouse_navigation()
        self.connect("close-request", self._close_requested)

    def _create_header_bar(self) -> Gtk.HeaderBar:
        header = Gtk.HeaderBar()
        self.back_button = self._icon_button("go-previous-symbolic", "Back", "win.back")
        self.forward_button = self._icon_button("go-next-symbolic", "Forward", "win.forward")
        self.up_button = self._icon_button("go-up-symbolic", "Parent directory", "win.up")
        self.home_button = self._icon_button("go-home-symbolic", "Home", "win.home")
        header.pack_start(self.back_button)
        header.pack_start(self.forward_button)
        header.pack_start(self.up_button)
        header.pack_start(self.home_button)

        self.window_title = Gtk.Label(label="Dakkar", ellipsize=3)
        self.window_title.add_css_class("title")
        header.set_title_widget(self.window_title)

        search_button = self._icon_button("edit-find-symbolic", "Search filenames", "win.search")
        new_tab_button = self._icon_button("tab-new-symbolic", "New tab", "win.new-tab")
        grid_settings_button = self._create_grid_settings_button()
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Main menu")
        menu_button.set_menu_model(self._main_menu())
        header.pack_end(menu_button)
        header.pack_end(grid_settings_button)
        header.pack_end(new_tab_button)
        header.pack_end(search_button)
        return header

    def _create_grid_settings_button(self) -> Gtk.MenuButton:
        button = Gtk.MenuButton(
            icon_name="view-grid-symbolic",
            tooltip_text="Grid appearance",
        )
        popover = Gtk.Popover(autohide=True)
        grid = Gtk.Grid(column_spacing=8, row_spacing=10)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        self.grid_setting_scales: list[Gtk.Scale] = []
        self.grid_setting_spins: list[Gtk.SpinButton] = []
        controls = (
            (
                "Grid padding",
                DEFAULT_GRID_PADDING,
                0,
                MAX_GRID_PADDING,
                "grid_padding_adjustment",
            ),
            (
                "Item spacing",
                DEFAULT_GRID_SPACING,
                0,
                MAX_GRID_SPACING,
                "grid_spacing_adjustment",
            ),
            (
                "Icon size",
                DEFAULT_ICON_SIZE,
                MIN_ICON_SIZE,
                MAX_ICON_SIZE,
                "icon_size_adjustment",
            ),
        )
        for row, (label, value, lower, upper, attribute) in enumerate(controls):
            adjustment = Gtk.Adjustment(
                value=value,
                lower=lower,
                upper=upper,
                step_increment=1,
                page_increment=8,
            )
            setattr(self, attribute, adjustment)
            adjustment.connect("value-changed", self._grid_settings_changed)
            scale = Gtk.Scale(
                orientation=Gtk.Orientation.HORIZONTAL,
                adjustment=adjustment,
                draw_value=False,
                hexpand=True,
            )
            scale.set_size_request(170, -1)
            spin = Gtk.SpinButton(adjustment=adjustment, climb_rate=1, digits=0)
            spin.set_numeric(True)
            spin.set_width_chars(3)
            self.grid_setting_scales.append(scale)
            self.grid_setting_spins.append(spin)
            grid.attach(Gtk.Label(label=label, xalign=0.0), 0, row, 1, 1)
            grid.attach(scale, 1, row, 1, 1)
            grid.attach(spin, 2, row, 1, 1)
            grid.attach(Gtk.Label(label="px"), 3, row, 1, 1)
        self.show_full_item_names_check = Gtk.CheckButton(
            label="Show full item names",
            active=DEFAULT_SHOW_FULL_ITEM_NAMES,
        )
        self.show_full_item_names_check.connect(
            "toggled",
            self._grid_settings_changed,
        )
        grid.attach(self.show_full_item_names_check, 0, len(controls), 4, 1)
        popover.set_child(grid)
        button.set_popover(popover)
        self.grid_settings_popover = popover
        return button

    @staticmethod
    def _icon_button(icon_name: str, tooltip: str, action: str) -> Gtk.Button:
        return Gtk.Button(icon_name=icon_name, tooltip_text=tooltip, action_name=action)

    @staticmethod
    def _main_menu() -> Gio.Menu:
        menu = Gio.Menu()
        window_section = Gio.Menu()
        window_section.append("New window", "win.new-window")
        window_section.append("New tab", "win.new-tab")
        window_section.append("Close tab", "win.close-tab")
        menu.append_section(None, window_section)
        view_section = Gio.Menu()
        view_section.append("Grid view", "win.grid-view")
        view_section.append("List view", "win.list-view")
        view_section.append(
            "Expandable list view",
            "win.expandable-list-view",
        )
        view_section.append("Show or hide Places", "win.toggle-places")
        view_section.append("Show hidden files", "win.show-hidden")
        view_section.append("Refresh", "win.refresh")
        view_section.append("Keyboard shortcuts…", "win.keyboard-shortcuts")
        menu.append_section(None, view_section)
        file_section = Gio.Menu()
        file_section.append("New directory…", "win.new-directory")
        file_section.append("New file…", "win.new-file")
        file_section.append("Undo", "win.undo")
        file_section.append("Redo", "win.redo")
        file_section.append("Open in terminal", "win.terminal")
        file_section.append("Create archive…", "win.create-archive")
        file_section.append("Extract here", "win.extract-here")
        file_section.append("Find duplicate copies…", "win.find-duplicates")
        file_section.append("Restore empty files…", "win.restore-empty-files")
        file_section.append("Cancel file operation", "win.cancel-operation")
        file_section.append("Show or hide file operations", "win.toggle-operations")
        file_section.append("Empty Trash…", "win.empty-trash")
        file_section.append("Properties…", "win.properties")
        menu.append_section(None, file_section)
        return menu

    def _show_keyboard_shortcuts(self) -> None:
        dialog = Gtk.Dialog(
            title="Keyboard shortcuts",
            transient_for=self,
            modal=True,
        )
        dialog.set_default_size(540, 480)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)

        grid = Gtk.Grid(column_spacing=24, row_spacing=8)
        grid.set_margin_top(16)
        grid.set_margin_bottom(16)
        grid.set_margin_start(16)
        grid.set_margin_end(16)
        cause_heading = Gtk.Label(label="Cause", xalign=0.0)
        cause_heading.add_css_class("heading")
        effect_heading = Gtk.Label(label="Effect", xalign=0.0)
        effect_heading.add_css_class("heading")
        grid.attach(cause_heading, 0, 0, 1, 1)
        grid.attach(effect_heading, 1, 0, 1, 1)
        for row, (cause, effect) in enumerate(KEYBOARD_SHORTCUTS, start=1):
            grid.attach(
                Gtk.Label(label=cause, xalign=0.0, selectable=True),
                0,
                row,
                1,
                1,
            )
            grid.attach(
                Gtk.Label(label=effect, xalign=0.0, wrap=True),
                1,
                row,
                1,
                1,
            )

        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vexpand=True,
        )
        scrolled.set_child(grid)
        dialog.get_content_area().append(scrolled)
        dialog.connect("response", lambda current, _response: current.destroy())
        dialog.present()

    def _create_navigation_bar(self) -> Gtk.Box:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.add_css_class("dakkar-navigation-area")
        self.navigation_area = outer
        toolbar = Gtk.Box(spacing=6)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)
        toolbar.set_margin_top(5)
        toolbar.set_margin_bottom(5)

        self.location_entry = Gtk.Entry(hexpand=True, placeholder_text="Location path or URI")
        self.location_entry.add_css_class("dakkar-location-entry")
        self.location_entry.connect("activate", self._location_activated)
        toolbar.append(self.location_entry)

        self.search_entry = Gtk.SearchEntry(hexpand=True, placeholder_text="Search filenames")
        self.search_entry.add_css_class("dakkar-search-entry")
        self.search_entry.set_max_width_chars(36)
        self.search_entry.connect("search-changed", self._search_changed)
        self.search_entry.connect("stop-search", self._stop_search)
        toolbar.append(self.search_entry)

        self.global_search_button = Gtk.ToggleButton(
            icon_name="system-search-symbolic",
            tooltip_text="Search filenames everywhere below Home",
        )
        self.global_search_button.connect("toggled", self._global_search_toggled)
        toolbar.append(self.global_search_button)
        toolbar.append(self._create_filter_button())
        toolbar.append(self._create_saved_search_button())

        self.sort_drop_down = Gtk.DropDown.new_from_strings(
            [
                "Name",
                "Last modified",
                "Deep directory modification",
                "Size",
                "Type",
                "Relevance",
            ]
        )
        self.sort_drop_down.set_tooltip_text("Sort order")
        self.sort_drop_down.connect("notify::selected", self._sort_changed)
        toolbar.append(self.sort_drop_down)
        self.colour_rankings_button = self._create_colour_rankings_button()
        toolbar.append(self.colour_rankings_button)

        self.reverse_button = Gtk.ToggleButton(
            icon_name="view-sort-descending-symbolic",
            tooltip_text="Reverse sort order",
        )
        self.reverse_button.connect("toggled", self._sort_changed)
        toolbar.append(self.reverse_button)

        outer.append(toolbar)
        self.path_bar = Gtk.Box(spacing=2)
        self.path_bar.add_css_class("dakkar-pathbar")
        path_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        path_scroll.set_child(self.path_bar)
        outer.append(path_scroll)
        return outer

    def _create_colour_rankings_button(self) -> Gtk.MenuButton:
        button = Gtk.MenuButton(
            icon_name="preferences-color-symbolic",
            tooltip_text="Colour rankings",
        )
        button.set_sensitive(self.directory_tint)
        if not self.directory_tint:
            button.set_tooltip_text(
                "Colour rankings require --directory-tint"
            )

        self.colour_rankings_popover = Gtk.Popover(autohide=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        heading = Gtk.Label(label="Colour rankings", xalign=0.0)
        heading.add_css_class("heading")
        box.append(heading)

        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        grid.attach(Gtk.Label(label="Colour", xalign=0.0), 0, 0, 1, 1)
        grid.attach(Gtk.Label(label="Ranking", xalign=0.0), 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Edge", xalign=0.0), 2, 0, 1, 1)
        source_labels = (
            "None",
            "Follow position ranking",
            "Size",
            "Last modified",
            "Deep modification",
        )
        edge_labels = ("Left edge", "Right edge")
        self.turquoise_tint_source = Gtk.DropDown.new_from_strings(source_labels)
        self.turquoise_tint_edge = Gtk.DropDown.new_from_strings(edge_labels)
        self.blue_tint_source = Gtk.DropDown.new_from_strings(source_labels)
        self.blue_tint_edge = Gtk.DropDown.new_from_strings(edge_labels)
        grid.attach(Gtk.Label(label="Turquoise", xalign=0.0), 0, 1, 1, 1)
        grid.attach(self.turquoise_tint_source, 1, 1, 1, 1)
        grid.attach(self.turquoise_tint_edge, 2, 1, 1, 1)
        grid.attach(Gtk.Label(label="Blue", xalign=0.0), 0, 2, 1, 1)
        grid.attach(self.blue_tint_source, 1, 2, 1, 1)
        grid.attach(self.blue_tint_edge, 2, 2, 1, 1)
        box.append(grid)

        scope_row = Gtk.Box(spacing=8)
        scope_row.append(Gtk.Label(label="Scope", xalign=0.0))
        self.tint_scope = Gtk.DropDown.new_from_strings(
            ("Directories only", "All items")
        )
        scope_row.append(self.tint_scope)
        box.append(scope_row)

        self.turquoise_tint_legend = Gtk.Label(xalign=0.0, wrap=True)
        self.blue_tint_legend = Gtk.Label(xalign=0.0, wrap=True)
        self.turquoise_tint_legend.add_css_class("dim-label")
        self.blue_tint_legend.add_css_class("dim-label")
        box.append(self.turquoise_tint_legend)
        box.append(self.blue_tint_legend)

        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        reset = Gtk.Button(label="Reset")
        reset.connect("clicked", self._reset_colour_ranking_controls)
        apply = Gtk.Button(label="Apply")
        apply.connect("clicked", self._apply_colour_ranking_controls)
        actions.append(reset)
        actions.append(apply)
        box.append(actions)

        for drop_down in (
            self.turquoise_tint_source,
            self.turquoise_tint_edge,
            self.blue_tint_source,
            self.blue_tint_edge,
            self.tint_scope,
        ):
            drop_down.connect(
                "notify::selected",
                lambda *_arguments: self._update_colour_ranking_legends(),
            )

        self.colour_rankings_popover.set_child(box)
        button.set_popover(self.colour_rankings_popover)
        button.connect("notify::active", self._colour_rankings_opened)
        self._show_tint_settings(DEFAULT_TINT_SETTINGS)
        return button

    def _colour_rankings_opened(
        self,
        button: Gtk.MenuButton,
        _parameter,
    ) -> None:
        if button.get_active() and self.active_tab is not None:
            self._show_tab_colour_rankings(self.active_tab)

    def _show_tint_settings(self, settings: DirectoryTintSettings) -> None:
        self.turquoise_tint_source.set_selected(
            TINT_SOURCES.index(settings.turquoise.source)
        )
        self.turquoise_tint_edge.set_selected(
            TINT_EDGES.index(settings.turquoise.edge)
        )
        self.blue_tint_source.set_selected(
            TINT_SOURCES.index(settings.blue.source)
        )
        self.blue_tint_edge.set_selected(
            TINT_EDGES.index(settings.blue.edge)
        )
        self.tint_scope.set_selected(TINT_SCOPES.index(settings.scope))
        self._update_colour_ranking_legends()

    def _show_tab_colour_rankings(self, tab: BrowserTab) -> None:
        self._show_tint_settings(tab.directory_tint_settings)

    def _reset_colour_ranking_controls(self, _button=None) -> None:
        self._show_tint_settings(DEFAULT_TINT_SETTINGS)

    def _apply_colour_ranking_controls(self, _button=None) -> None:
        tab = self.active_tab
        if tab is None:
            return
        tab.set_directory_tint_settings(
            DirectoryTintSettings(
                turquoise=TintChannelSettings(
                    TINT_SOURCES[self.turquoise_tint_source.get_selected()],
                    TINT_EDGES[self.turquoise_tint_edge.get_selected()],
                ),
                blue=TintChannelSettings(
                    TINT_SOURCES[self.blue_tint_source.get_selected()],
                    TINT_EDGES[self.blue_tint_edge.get_selected()],
                ),
                scope=TINT_SCOPES[self.tint_scope.get_selected()],
            )
        )
        self.colour_rankings_popover.popdown()

    def _update_colour_ranking_legends(self) -> None:
        self.turquoise_tint_legend.set_text(
            self._tint_legend(
                "Turquoise",
                TINT_SOURCES[self.turquoise_tint_source.get_selected()],
            )
        )
        self.blue_tint_legend.set_text(
            self._tint_legend(
                "Blue",
                TINT_SOURCES[self.blue_tint_source.get_selected()],
            )
        )

    @staticmethod
    def _tint_legend(colour: str, source: str) -> str:
        if source == "none":
            return f"{colour}: off"
        if source == "follow-sort":
            return f"{colour}: follows the position ranking"
        if source == "size":
            return f"{colour}: weakest for smallest, strongest for largest"
        return f"{colour}: weakest for oldest, strongest for newest"

    def _create_filter_button(self) -> Gtk.MenuButton:
        button = Gtk.MenuButton(label="Filters", tooltip_text="File and date filters")
        self.filter_popover = Gtk.Popover()
        self.filter_popover.set_autohide(True)
        self.filter_popover.connect("closed", self._filter_popover_closed)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.append(Gtk.Label(label="File type", xalign=0.0))
        self.type_filter = Gtk.DropDown.new_from_strings(
            ["All types", "Documents", "Images", "Audio", "Video", "Directories"]
        )
        box.append(self.type_filter)
        box.append(Gtk.Label(label="Date field", xalign=0.0))
        self.date_field_filter = Gtk.DropDown.new_from_strings(
            ["Modified", "Accessed", "Created"]
        )
        box.append(self.date_field_filter)
        box.append(Gtk.Label(label="Date range", xalign=0.0))
        self.period_filter = Gtk.DropDown.new_from_strings(
            ["Any time", "Today", "Past week", "Past month", "Past year"]
        )
        box.append(self.period_filter)
        self.apply_filter_button = Gtk.Button(label="Apply", halign=Gtk.Align.END)
        self.apply_filter_button.connect("clicked", self._apply_filter_selections)
        box.append(self.apply_filter_button)
        self.filter_popover.set_child(box)
        button.set_popover(self.filter_popover)
        return button

    def _create_saved_search_button(self) -> Gtk.MenuButton:
        button = Gtk.MenuButton(label="Saved", tooltip_text="Saved filename searches")
        self.saved_search_popover = Gtk.Popover()
        self.saved_search_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.saved_search_box.set_margin_top(10)
        self.saved_search_box.set_margin_bottom(10)
        self.saved_search_box.set_margin_start(10)
        self.saved_search_box.set_margin_end(10)
        self.saved_search_popover.set_child(self.saved_search_box)
        button.set_popover(self.saved_search_popover)
        button.connect("notify::active", lambda *_: self._refresh_saved_searches())
        self._refresh_saved_searches()
        return button

    def _refresh_saved_searches(self) -> None:
        while child := self.saved_search_box.get_first_child():
            self.saved_search_box.remove(child)
        save_button = Gtk.Button(label="Save current search…")
        save_button.connect("clicked", lambda *_: self._save_current_search())
        self.saved_search_box.append(save_button)
        searches = self.saved_searches.load()
        if searches:
            self.saved_search_box.append(Gtk.Separator())
        for search in searches:
            button = Gtk.Button(label=search.name)
            button.connect("clicked", lambda _button, value=search: self._open_saved_search(value))
            self.saved_search_box.append(button)

    def _save_current_search(self) -> None:
        tab = self.active_tab
        query = self.search_entry.get_text().strip()
        if tab is None or not query:
            self.show_error(
                "Save search",
                ValueError("Specify a filename search first."),
            )
            return

        def save(name: str) -> None:
            clean_name = name.strip()
            if not clean_name:
                return
            try:
                self.saved_searches.add(
                    SavedSearch(
                        name=clean_name,
                        query=query,
                        global_search=self.global_search_button.get_active(),
                        location_uri=tab.location.get_uri(),
                        mime_group=tab.search_mime_group,
                        date_field=tab.search_date_field,
                        period=tab.search_period,
                    )
                )
                self._refresh_saved_searches()
                self.saved_search_popover.popdown()
            except OSError as error:
                self.show_error("Unable to save search", error)

        self.prompt_text("Save filename search", "Search name", query, save)

    def _open_saved_search(self, search: SavedSearch) -> None:
        tab = self.active_tab
        if tab is None:
            return
        tab.navigate(Gio.File.new_for_uri(search.location_uri))
        tab.search_mime_group = search.mime_group
        tab.search_date_field = search.date_field
        tab.search_period = search.period
        self._show_tab_filters(tab)
        self.global_search_button.set_active(search.global_search)
        self.search_entry.set_text(search.query)
        self.saved_search_popover.popdown()

    def _create_sidebar(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("dakkar-sidebar")
        self.places_heading_box = Gtk.Box(spacing=6)
        self.places_heading_box.add_css_class("dakkar-places-heading")
        label = Gtk.Label(label="Places", xalign=0.0, hexpand=True)
        label.add_css_class("heading")
        label.set_margin_start(12)
        label.set_margin_top(10)
        label.set_margin_bottom(6)
        self.places_heading_box.append(label)
        self.places_toggle_button = Gtk.Button(
            icon_name="sidebar-hide-symbolic",
            tooltip_text="Hide Places",
            has_frame=False,
            action_name="win.toggle-places",
        )
        self.places_toggle_button.set_margin_end(6)
        self.places_heading_box.append(self.places_toggle_button)
        box.append(self.places_heading_box)
        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar.connect("row-activated", self._sidebar_activated)
        self._populate_sidebar()
        self._install_sidebar_pointer_actions()
        scrolled = Gtk.ScrolledWindow(vexpand=True, min_content_width=190)
        scrolled.add_css_class("dakkar-places-panel")
        scrolled.set_child(self.sidebar)
        box.append(scrolled)
        return box

    def _populate_sidebar(self) -> None:
        while child := self.sidebar.get_first_child():
            self.sidebar.remove(child)
        self._sidebar_rows.clear()
        home = Path.home()
        places: list[tuple[str, str, str]] = [
            ("Home", home.as_uri(), "user-home-symbolic"),
        ]
        for label in ("Desktop", "Documents", "Downloads", "Music", "Pictures", "Videos"):
            path = home / label
            if path.is_dir():
                places.append((label, path.as_uri(), "folder-symbolic"))
        places.extend(
            (
                ("Trash", "trash:///", "user-trash-symbolic"),
                ("Network", "network:///", "network-workgroup-symbolic"),
            )
        )
        for label, uri, icon in places:
            self._append_sidebar_row(label, uri, icon)

        bookmarks = load_bookmarks()
        if bookmarks:
            self.sidebar.append(Gtk.Separator())
            for bookmark in bookmarks:
                self._append_sidebar_row(bookmark.label, bookmark.uri, "user-bookmarks-symbolic")

        mounts = self._volume_monitor.get_mounts()
        if mounts:
            self.sidebar.append(Gtk.Separator())
            for mount in mounts:
                root = mount.get_root()
                self._append_sidebar_row(
                    mount.get_name(),
                    root.get_uri(),
                    "drive-harddisk-symbolic",
                    mount,
                )

    def _append_sidebar_row(
        self,
        label: str,
        uri: str,
        icon_name: str,
        mount: Gio.Mount | None = None,
    ) -> None:
        row = Gtk.ListBoxRow()
        row.target_uri = uri
        content = Gtk.Box(spacing=9)
        content.set_margin_start(10)
        content.set_margin_end(8)
        content.set_margin_top(7)
        content.set_margin_bottom(7)
        content.append(Gtk.Image(icon_name=icon_name, pixel_size=18))
        content.append(Gtk.Label(label=label, xalign=0.0, ellipsize=3, hexpand=True))
        if mount is not None and (mount.can_eject() or mount.can_unmount()):
            eject = mount.can_eject()
            button = Gtk.Button(
                icon_name="media-eject-symbolic" if eject else "media-removable-symbolic",
                tooltip_text="Eject" if eject else "Unmount",
                has_frame=False,
            )
            button.connect(
                "clicked",
                lambda _button, target=mount, use_eject=eject: self._unmount(target, use_eject),
            )
            content.append(button)
        row.set_child(content)
        self.sidebar.append(row)
        self._sidebar_rows.append(row)

    def _sidebar_activated(self, _list_box, row) -> None:
        uri = getattr(row, "target_uri", None)
        if uri and self.active_tab is not None:
            self.active_tab.navigate(Gio.File.new_for_uri(uri))

    def _install_sidebar_pointer_actions(self) -> None:
        popover = Gtk.PopoverMenu.new_from_model(Gio.Menu())
        popover.set_parent(self.sidebar)

        context_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        context_click.connect(
            "pressed",
            lambda gesture, _count, x, y: self._sidebar_context_pressed(
                gesture,
                x,
                y,
                popover,
            ),
        )
        self.sidebar.add_controller(context_click)
        self.sidebar._dakkar_context_gesture = context_click
        self.sidebar._dakkar_context_popover = popover

        middle_click = Gtk.GestureClick(button=Gdk.BUTTON_MIDDLE)
        middle_click.connect("pressed", self._sidebar_middle_pressed)
        self.sidebar.add_controller(middle_click)
        self.sidebar._dakkar_middle_gesture = middle_click

    def _sidebar_row_at(self, y: float) -> Gtk.ListBoxRow | None:
        row = self.sidebar.get_row_at_y(int(y))
        if row is None or not getattr(row, "target_uri", None):
            return None
        return row

    def _sidebar_context_pressed(
        self,
        gesture: Gtk.GestureClick,
        x: float,
        y: float,
        popover: Gtk.PopoverMenu,
    ) -> None:
        row = self._sidebar_row_at(y)
        if row is None:
            return
        self.sidebar.select_row(row)
        location = Gio.File.new_for_uri(row.target_uri)
        popover.set_menu_model(self._places_context_menu_model(location))
        rectangle = Gdk.Rectangle()
        rectangle.x = int(x)
        rectangle.y = int(y)
        rectangle.width = 1
        rectangle.height = 1
        popover.set_pointing_to(rectangle)
        popover.popup()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _sidebar_middle_pressed(
        self,
        gesture: Gtk.GestureClick,
        _count: int,
        _x: float,
        y: float,
    ) -> None:
        row = self._sidebar_row_at(y)
        if row is None:
            return
        self.open_location_in_tab(Gio.File.new_for_uri(row.target_uri))
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    @staticmethod
    def _location_menu_item(
        label: str,
        action: str,
        location: Gio.File,
    ) -> Gio.MenuItem:
        item = Gio.MenuItem.new(label, None)
        item.set_action_and_target_value(
            action,
            GLib.Variant.new_string(location.get_uri()),
        )
        return item

    def _places_context_menu_model(self, location: Gio.File) -> Gio.Menu:
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append_item(
            self._location_menu_item(
                "Open in new tab",
                "win.open-tab-at",
                location,
            )
        )
        if self.can_open_new_pane():
            section.append_item(
                self._location_menu_item(
                    "Open in new pane",
                    "win.open-pane-at",
                    location,
                )
            )
        section.append_item(
            self._location_menu_item(
                "Open in new window",
                "win.open-window-at",
                location,
            )
        )
        section.append_item(
            self._location_menu_item(
                "Open in terminal",
                "win.open-terminal-at",
                location,
            )
        )
        menu.append_section(None, section)
        return menu

    def _install_path_button_context_menu(
        self,
        button: Gtk.Button,
        location: Gio.File,
    ) -> None:
        popover = Gtk.PopoverMenu.new_from_model(Gio.Menu())
        popover.set_parent(button)
        context_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        context_click.connect(
            "pressed",
            self._path_button_context_pressed,
            location,
            popover,
        )
        button.add_controller(context_click)
        button._dakkar_context_gesture = context_click
        button._dakkar_context_popover = popover
        button._dakkar_location_uri = location.get_uri()

    def _path_button_context_pressed(
        self,
        gesture: Gtk.GestureClick,
        _count: int,
        x: float,
        y: float,
        location: Gio.File,
        popover: Gtk.PopoverMenu,
    ) -> None:
        popover.set_menu_model(self._places_context_menu_model(location))
        rectangle = Gdk.Rectangle()
        rectangle.x = int(x)
        rectangle.y = int(y)
        rectangle.width = 1
        rectangle.height = 1
        popover.set_pointing_to(rectangle)
        popover.popup()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _mounts_changed(self, *_arguments) -> None:
        self._populate_sidebar()

    def _unmount(self, mount: Gio.Mount, eject: bool) -> None:
        operation = Gtk.MountOperation.new(self)

        def finished(source, result, *_arguments):
            try:
                if eject:
                    source.eject_with_operation_finish(result)
                else:
                    source.unmount_with_operation_finish(result)
            except GLib.Error as error:
                self.show_error("Unable to eject" if eject else "Unable to unmount", error)

        if eject:
            mount.eject_with_operation(
                Gio.MountUnmountFlags.NONE,
                operation,
                None,
                finished,
            )
        else:
            mount.unmount_with_operation(
                Gio.MountUnmountFlags.NONE,
                operation,
                None,
                finished,
            )

    @property
    def active_tab(self) -> BrowserTab | None:
        page = self.notebook.get_current_page()
        if page < 0:
            return None
        child = self.notebook.get_nth_page(page)
        return child if isinstance(child, BrowserTab) else None

    def _create_pane_notebook(self) -> Gtk.Notebook:
        notebook = Gtk.Notebook(hexpand=True, vexpand=True, scrollable=True)
        notebook.add_css_class("dakkar-notebook")
        notebook.set_group_name("dakkar-tabs")
        notebook.connect("switch-page", self._tab_switched)

        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect(
            "pressed",
            lambda *_arguments, target=notebook: self._set_active_notebook(target),
        )
        notebook.add_controller(click)
        notebook._dakkar_pane_click = click

        focus = Gtk.EventControllerFocus()
        focus.connect(
            "enter",
            lambda _controller, target=notebook: self._set_active_notebook(target),
        )
        notebook.add_controller(focus)
        notebook._dakkar_pane_focus = focus
        return notebook

    def _set_active_notebook(self, notebook: Gtk.Notebook) -> None:
        if notebook not in self.pane_notebooks:
            return
        if self.notebook is not notebook:
            self.notebook.remove_css_class("dakkar-active-pane")
            self.notebook = notebook
            self.notebook.add_css_class("dakkar-active-pane")
        tab = self.active_tab
        if tab is not None:
            self._show_tab_filters(tab)
            self._show_tab_grid_settings(tab)
            self._show_tab_colour_rankings(tab)
            self._show_tab_sort(tab)
            self.tab_location_changed(tab)

    def _notebook_for_tab(self, tab: BrowserTab) -> Gtk.Notebook | None:
        for notebook in self.pane_notebooks:
            if notebook.page_num(tab) >= 0:
                return notebook
        return None

    def can_open_new_pane(self) -> bool:
        return len(self.pane_notebooks) < 2

    def add_tab(
        self,
        location: Gio.File,
        make_active: bool = True,
        notebook: Gtk.Notebook | None = None,
        position: int | None = None,
    ) -> BrowserTab:
        target_notebook = notebook or self.notebook
        try:
            info = location.query_info(
                Gio.FILE_ATTRIBUTE_STANDARD_TYPE,
                Gio.FileQueryInfoFlags.NONE,
                None,
            )
            if info.get_file_type() != Gio.FileType.DIRECTORY:
                parent = location.get_parent()
                if parent is not None:
                    location = parent
        except GLib.Error:
            pass
        tab = BrowserTab(
            self,
            location,
            self.thumbnails,
            self.use_index,
            directory_tint=self.directory_tint,
        )
        label_box = Gtk.Box(spacing=5)
        directory_name = location.get_basename() or location.get_parse_name()
        label = Gtk.Label(label=directory_name, ellipsize=3)
        label.set_max_width_chars(18)
        label.set_tooltip_text(directory_name)
        close = Gtk.Button(icon_name="window-close-symbolic", has_frame=False, tooltip_text="Close tab")
        close.connect("clicked", lambda _button: self.close_tab(tab))
        label_box.append(label)
        label_box.append(close)
        label_box._dakkar_label = label
        self._install_tab_context_menu(label_box, tab)
        page = (
            target_notebook.append_page(tab, label_box)
            if position is None
            else target_notebook.insert_page(
                tab,
                label_box,
                min(max(position, 0), target_notebook.get_n_pages()),
            )
        )
        target_notebook.set_tab_reorderable(tab, False)
        if make_active:
            self._set_active_notebook(target_notebook)
            target_notebook.set_current_page(page)
        self.tab_location_changed(tab)
        return tab

    def close_tab(self, tab: BrowserTab | None = None) -> None:
        target = tab or self.active_tab
        if target is None:
            return
        notebook = self._notebook_for_tab(target)
        if notebook is None:
            return
        page = notebook.page_num(target)
        if page >= 0:
            self._remember_closed_tab(target, notebook, page)
            self._clear_tab_context_menu(notebook, target)
            target.dispose_tab()
            notebook.remove_page(page)
        if notebook.get_n_pages() > 0:
            if notebook is self.notebook:
                active = self.active_tab
                if active is not None:
                    self._show_tab_filters(active)
                    self._show_tab_grid_settings(active)
                    self._show_tab_colour_rankings(active)
                    self._show_tab_sort(active)
                    self.tab_location_changed(active)
            return
        if len(self.pane_notebooks) == 1:
            self.close()
            return
        self._remove_empty_pane(notebook)

    def _remove_empty_pane(self, notebook: Gtk.Notebook) -> None:
        if notebook not in self.pane_notebooks or len(self.pane_notebooks) < 2:
            return
        remaining = next(
            candidate
            for candidate in self.pane_notebooks
            if candidate is not notebook
        )
        closing_start_pane = self.pane_split.get_start_child() is notebook
        self._set_active_notebook(remaining)
        active = self.active_tab
        if active is not None:
            active.grab_view_focus()
        if closing_start_pane:
            self.pane_split.set_start_child(None)
            self.pane_split.set_end_child(None)
            self.pane_split.set_start_child(remaining)
        else:
            self.pane_split.set_end_child(None)
        self.pane_notebooks.remove(notebook)
        self._update_action_availability()

    def _remember_closed_tab(
        self,
        tab: BrowserTab,
        notebook: Gtk.Notebook,
        page_index: int,
    ) -> None:
        history_uris, history_position = tab.history.snapshot()
        state = ClosedTabState(
            location_uri=tab.location.get_uri(),
            history_uris=history_uris,
            history_position=history_position,
            pane_index=self.pane_notebooks.index(notebook),
            page_index=page_index,
            view_name=tab.view_name,
            sort_key=tab.sort_key,
            sort_descending=tab.sort_descending,
            show_hidden=tab.show_hidden,
            grid_padding=tab.grid_padding,
            grid_spacing=tab.grid_spacing,
            grid_icon_size=tab.grid_icon_size,
            show_full_item_names=tab.show_full_item_names,
            search_mime_group=tab.search_mime_group,
            search_date_field=tab.search_date_field,
            search_period=tab.search_period,
            tint_settings=tab.directory_tint_settings,
        )
        self._closed_tabs.append(state)
        del self._closed_tabs[:-MAX_CLOSED_TABS]
        self._update_action_availability()

    def _restore_closed_tab(self) -> None:
        if not self._closed_tabs:
            return
        state = self._closed_tabs.pop()
        target_notebook = self._notebook_for_restored_tab(state.pane_index)
        restored = self.add_tab(
            Gio.File.new_for_uri(state.location_uri),
            make_active=True,
            notebook=target_notebook,
            position=state.page_index,
        )
        restored.history.restore(
            state.history_uris,
            state.history_position,
        )
        restored.show_hidden = state.show_hidden
        restored.search_mime_group = state.search_mime_group
        restored.search_date_field = state.search_date_field
        restored.search_period = state.search_period
        restored.set_grid_layout(
            padding=state.grid_padding,
            spacing=state.grid_spacing,
            icon_size=state.grid_icon_size,
            show_full_item_names=state.show_full_item_names,
        )
        restored.set_directory_tint_settings(state.tint_settings)
        restored.set_sort(state.sort_key, state.sort_descending)
        restored.set_view(state.view_name)
        if state.show_hidden:
            restored.refresh()
        self._show_tab_filters(restored)
        self._show_tab_grid_settings(restored)
        self._show_tab_colour_rankings(restored)
        self._show_tab_sort(restored)
        self.tab_location_changed(restored)

    def _notebook_for_restored_tab(self, pane_index: int) -> Gtk.Notebook:
        if pane_index < len(self.pane_notebooks):
            return self.pane_notebooks[pane_index]
        if pane_index == 1 and self.can_open_new_pane():
            notebook = self._create_pane_notebook()
            self.pane_notebooks.append(notebook)
            self.pane_split.set_end_child(notebook)
            width = self.pane_split.get_width()
            if width > 0:
                self.pane_split.set_position(width // 2)
            return notebook
        return self.notebook

    def _tab_switched(self, notebook, page, _page_number) -> None:
        if notebook is self.notebook and isinstance(page, BrowserTab):
            self._show_tab_filters(page)
            self._show_tab_grid_settings(page)
            self._show_tab_colour_rankings(page)
            self._show_tab_sort(page)
            self.tab_location_changed(page)

    def _install_tab_context_menu(
        self,
        label_box: Gtk.Box,
        tab: BrowserTab,
    ) -> None:
        action_group = Gio.SimpleActionGroup()
        callbacks = {
            "duplicate": "_duplicate_context_tab",
            "open-pane": "_open_context_tab_in_pane",
            "open-window": "_open_context_tab_in_window",
            "open-terminal": "_open_context_tab_in_terminal",
            "copy-location": "_copy_context_tab_location",
            "refresh": "_refresh_context_tab",
            "close": "close_tab",
        }
        for name, method_name in callbacks.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect(
                "activate",
                lambda _action, _parameter, method=method_name: getattr(
                    self,
                    method,
                )(tab),
            )
            action_group.add_action(action)
        label_box.insert_action_group("tab", action_group)

        popover = Gtk.PopoverMenu.new_from_model(Gio.Menu())
        popover.set_parent(label_box)
        context_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        context_click.connect(
            "pressed",
            self._tab_context_pressed,
            tab,
            popover,
        )
        label_box.add_controller(context_click)
        label_box._dakkar_context_actions = action_group
        label_box._dakkar_context_gesture = context_click
        label_box._dakkar_context_popover = popover

    def _tab_context_pressed(
        self,
        gesture: Gtk.GestureClick,
        _count: int,
        x: float,
        y: float,
        tab: BrowserTab,
        popover: Gtk.PopoverMenu,
    ) -> None:
        if self._notebook_for_tab(tab) is None:
            return
        popover.set_menu_model(self._tab_context_menu_model())
        rectangle = Gdk.Rectangle()
        rectangle.x = int(x)
        rectangle.y = int(y)
        rectangle.width = 1
        rectangle.height = 1
        popover.set_pointing_to(rectangle)
        popover.popup()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _tab_context_menu_model(self) -> Gio.Menu:
        menu = Gio.Menu()
        location_section = Gio.Menu()
        location_section.append("Duplicate tab", "tab.duplicate")
        if self.can_open_new_pane():
            location_section.append("Open in new pane", "tab.open-pane")
        location_section.append("Open in new window", "tab.open-window")
        menu.append_section(None, location_section)

        utility_section = Gio.Menu()
        utility_section.append("Open in terminal", "tab.open-terminal")
        utility_section.append("Copy location", "tab.copy-location")
        utility_section.append("Refresh", "tab.refresh")
        menu.append_section(None, utility_section)

        close_section = Gio.Menu()
        close_section.append("Close tab", "tab.close")
        menu.append_section(None, close_section)
        return menu

    def _clear_tab_context_menu(
        self,
        notebook: Gtk.Notebook,
        tab: BrowserTab,
    ) -> None:
        label_box = notebook.get_tab_label(tab)
        if label_box is None:
            return
        popover = getattr(label_box, "_dakkar_context_popover", None)
        if popover is not None and popover.get_parent() is not None:
            popover.popdown()
            popover.unparent()

    def _duplicate_context_tab(self, tab: BrowserTab) -> None:
        notebook = self._notebook_for_tab(tab)
        if notebook is not None:
            self.add_tab(tab.location, make_active=True, notebook=notebook)

    def _open_context_tab_in_pane(self, tab: BrowserTab) -> None:
        if self._notebook_for_tab(tab) is not None:
            self.open_location_in_pane(tab.location)

    def _open_context_tab_in_window(self, tab: BrowserTab) -> None:
        if self._notebook_for_tab(tab) is not None:
            self.open_location_in_window(tab.location)

    def _open_context_tab_in_terminal(self, tab: BrowserTab) -> None:
        if self._notebook_for_tab(tab) is not None:
            self._open_terminal_at(tab.location)

    def _copy_context_tab_location(self, tab: BrowserTab) -> None:
        if self._notebook_for_tab(tab) is None:
            return
        location_text = tab.location.get_path() or tab.location.get_uri()
        self._set_clipboard_text(location_text)
        tab.status_label.set_text("Location copied")

    def _refresh_context_tab(self, tab: BrowserTab) -> None:
        if self._notebook_for_tab(tab) is not None:
            tab.refresh()

    def _show_tab_sort(self, tab: BrowserTab) -> None:
        key = tab.sort_key if tab.sort_key in SORT_KEYS else DEFAULT_SORT_KEY
        self._updating_sort_controls = True
        try:
            self.sort_drop_down.set_selected(SORT_KEYS.index(key))
            self.reverse_button.set_active(
                sort_is_descending(key) != tab.sort_descending
            )
        finally:
            self._updating_sort_controls = False
        self._update_colour_ranking_legends()

    def tab_sort_changed(self, tab: BrowserTab) -> None:
        if tab is self.active_tab:
            self._show_tab_sort(tab)

    def tab_selection_changed(self, tab: BrowserTab) -> None:
        if tab is self.active_tab:
            self._update_action_availability()

    def _show_tab_filters(self, tab: BrowserTab) -> None:
        mime_values = ("all", "documents", "images", "audio", "video", "directories")
        date_values = ("modified", "accessed", "created")
        period_values = ("any", "today", "week", "month", "year")
        self._updating_filter_controls = True
        try:
            self.type_filter.set_selected(
                mime_values.index(tab.search_mime_group)
                if tab.search_mime_group in mime_values
                else 0
            )
            self.date_field_filter.set_selected(
                date_values.index(tab.search_date_field)
                if tab.search_date_field in date_values
                else 0
            )
            self.period_filter.set_selected(
                period_values.index(tab.search_period)
                if tab.search_period in period_values
                else 0
            )
        finally:
            self._updating_filter_controls = False

    def _show_tab_grid_settings(self, tab: BrowserTab) -> None:
        self._updating_grid_controls = True
        try:
            self.grid_padding_adjustment.set_value(tab.grid_padding)
            self.grid_spacing_adjustment.set_value(tab.grid_spacing)
            self.icon_size_adjustment.set_value(tab.grid_icon_size)
            self.show_full_item_names_check.set_active(
                tab.show_full_item_names
            )
        finally:
            self._updating_grid_controls = False

    def _grid_settings_changed(self, *_arguments) -> None:
        if self._updating_grid_controls:
            return
        tab = self.active_tab
        if tab is None:
            return
        tab.set_grid_layout(
            padding=round(self.grid_padding_adjustment.get_value()),
            spacing=round(self.grid_spacing_adjustment.get_value()),
            icon_size=round(self.icon_size_adjustment.get_value()),
            show_full_item_names=(
                self.show_full_item_names_check.get_active()
            ),
        )

    def tab_location_changed(self, tab: BrowserTab) -> None:
        notebook = self._notebook_for_tab(tab)
        page = notebook.page_num(tab) if notebook is not None else -1
        if page >= 0:
            label_box = notebook.get_tab_label(tab)
            if label_box is not None:
                directory_name = (
                    tab.location.get_basename()
                    or tab.location.get_parse_name()
                )
                label_box._dakkar_label.set_text(directory_name)
                label_box._dakkar_label.set_tooltip_text(directory_name)
        if tab is not self.active_tab:
            return
        location_text = tab.location.get_path() or tab.location.get_uri()
        self.location_entry.set_text(location_text)
        self.window_title.set_text(tab.location.get_basename() or tab.location.get_parse_name())
        self.back_button.set_sensitive(tab.history.can_go_back)
        self.forward_button.set_sensitive(tab.history.can_go_forward)
        self.up_button.set_sensitive(tab.location.get_parent() is not None)
        self._rebuild_path_bar(tab.location)
        self._update_action_availability()

    def _rebuild_path_bar(self, location: Gio.File) -> None:
        self._clear_path_bar()
        lineage: list[Gio.File] = []
        cursor: Gio.File | None = location
        while cursor is not None:
            lineage.append(cursor)
            cursor = cursor.get_parent()
        if len(lineage) <= 1 and location.get_path() is None:
            lineage = [location]
        for file in reversed(lineage):
            label = file.get_basename() or file.get_parse_name()
            button = Gtk.Button(label=label, has_frame=False)
            button.connect(
                "clicked",
                lambda _button, target=file: self._navigate_active(target),
            )
            self._install_path_button_context_menu(button, file)
            self.path_bar.append(button)
            self._path_buttons.append(button)

    def _clear_path_bar(self) -> None:
        for button in self._path_buttons:
            popover = getattr(button, "_dakkar_context_popover", None)
            if popover is not None and popover.get_parent() is not None:
                popover.popdown()
                popover.unparent()
        while child := self.path_bar.get_first_child():
            self.path_bar.remove(child)
        self._path_buttons.clear()

    def _navigate_active(self, location: Gio.File) -> None:
        if self.active_tab is not None:
            self.active_tab.navigate(location)

    def _location_activated(self, entry: Gtk.Entry) -> None:
        value = entry.get_text().strip()
        if not value or self.active_tab is None:
            return
        try:
            location = Gio.File.new_for_commandline_arg(value)
            self.active_tab.navigate(location)
        except (GLib.Error, ValueError) as error:
            self.show_error("Unable to open location", error)

    def _search_changed(self, entry: Gtk.SearchEntry) -> None:
        if self.active_tab is not None:
            self.active_tab.search_later(entry.get_text(), self.global_search_button.get_active())

    def _stop_search(self, _entry: Gtk.SearchEntry) -> None:
        self.search_entry.set_text("")
        if self.active_tab is not None:
            self.active_tab.grab_view_focus()

    def _stop_search_action(self, *_arguments) -> None:
        if self.search_entry.get_text():
            self._stop_search(self.search_entry)

    def _global_search_toggled(self, _button) -> None:
        if self.active_tab is not None and self.search_entry.get_text().strip():
            self.active_tab.search_later(self.search_entry.get_text(), self.global_search_button.get_active())

    def _sort_changed(self, *_arguments) -> None:
        if self._updating_sort_controls:
            return
        tab = self.active_tab
        if tab is None:
            return
        key = SORT_KEYS[self.sort_drop_down.get_selected()]
        tab.set_sort(
            key,
            sort_is_descending(key, self.reverse_button.get_active()),
        )
        self._update_colour_ranking_legends()

    def _filters_changed(self, *_arguments) -> None:
        if self._updating_filter_controls:
            return
        tab = self.active_tab
        if tab is None:
            return
        tab.search_mime_group = (
            "all",
            "documents",
            "images",
            "audio",
            "video",
            "directories",
        )[self.type_filter.get_selected()]
        tab.search_date_field = (
            "modified",
            "accessed",
            "created",
        )[self.date_field_filter.get_selected()]
        tab.search_period = (
            "any",
            "today",
            "week",
            "month",
            "year",
        )[self.period_filter.get_selected()]
        if self.search_entry.get_text().strip():
            tab.search_later(
                self.search_entry.get_text(),
                self.global_search_button.get_active(),
            )
        else:
            tab.apply_filters()

    def _apply_filter_selections(self, *_arguments) -> None:
        self._filters_changed()
        self.filter_popover.popdown()

    def _filter_popover_closed(self, *_arguments) -> None:
        tab = self.active_tab
        if tab is not None:
            self._show_tab_filters(tab)

    def _install_actions(self) -> None:
        callbacks = {
            "new-tab": lambda *_: self.add_tab(self.active_tab.location if self.active_tab else Gio.File.new_for_path(GLib.get_home_dir())),
            "close-tab": lambda *_: self.close_tab(),
            "restore-tab": lambda *_: self._restore_closed_tab(),
            "new-window": lambda *_: self.get_application().new_window(self.active_tab.location if self.active_tab else None),
            "back": lambda *_: self._with_tab("go_back"),
            "forward": lambda *_: self._with_tab("go_forward"),
            "up": lambda *_: self._with_tab("go_up"),
            "home": lambda *_: self._with_tab("go_home"),
            "refresh": lambda *_: self._with_tab("refresh"),
            "location": lambda *_: self._focus_location(),
            "search": lambda *_: self._focus_search(False),
            "global-search": lambda *_: self._focus_search(True),
            "stop-search": self._stop_search_action,
            "keyboard-shortcuts": lambda *_: self._show_keyboard_shortcuts(),
            "show-hidden": lambda *_: self._toggle_hidden(),
            "toggle-places": lambda *_: self._toggle_places(),
            "grid-view": lambda *_: self._set_view("grid"),
            "list-view": lambda *_: self._set_view("list"),
            "expandable-list-view": lambda *_: self._set_view("expandable"),
            "new-directory": lambda *_: self._new_directory(),
            "new-file": lambda *_: self._new_file(),
            "copy-name": lambda *_: self._copy_selected_names(),
            "copy-path": lambda *_: self._copy_selected_paths(),
            "rename": lambda *_: self._rename(),
            "undo": lambda *_: self._undo(),
            "redo": lambda *_: self._redo(),
            "trash": lambda *_: self._trash(),
            "delete": lambda *_: self._delete(),
            "copy": lambda *_: self._copy(False),
            "cut": lambda *_: self._copy(True),
            "paste": lambda *_: self._paste(),
            "copy-to": lambda *_: self._copy_or_move_to(False),
            "move-to": lambda *_: self._copy_or_move_to(True),
            "duplicate": lambda *_: self._duplicate(),
            "find-duplicates": lambda *_: self._find_duplicates(),
            "restore-empty-files": lambda *_: self._restore_empty_files(),
            "create-archive": lambda *_: self._create_archive(),
            "extract-here": lambda *_: self._extract_archive(False),
            "extract-to": lambda *_: self._extract_archive(True),
            "properties": lambda *_: self._properties(),
            "terminal": lambda *_: self._open_terminal(),
            "open": lambda *_: self._open_selected(),
            "open-with": lambda *_: self._open_with(),
            "run-program": lambda *_: self._run_selected_program(),
            "open-tab": lambda *_: self._open_selected_in_tab(),
            "open-pane": lambda *_: self._open_selected_in_pane(),
            "open-window": lambda *_: self._open_selected_in_window(),
            "open-location": lambda *_: self._with_tab("reveal_selected_location"),
            "open-location-tab": lambda *_: self._open_selected_location_in_tab(),
            "open-location-pane": lambda *_: self._open_selected_location_in_pane(),
            "open-location-window": lambda *_: self._open_selected_location_in_window(),
            "select-all": lambda *_: self._with_tab("select_all"),
            "zoom-in": lambda *_: self._zoom(16),
            "zoom-out": lambda *_: self._zoom(-16),
            "empty-trash": lambda *_: self._empty_trash(),
            "cancel-operation": lambda *_: self.operations.cancel(),
            "toggle-operations": lambda *_: self._with_tab("toggle_operation_panel"),
        }
        for name, callback in callbacks.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        targeted_callbacks = {
            "open-tab-at": self.open_location_in_tab,
            "open-pane-at": self.open_location_in_pane,
            "open-window-at": self.open_location_in_window,
            "open-terminal-at": self._open_terminal_at,
            "paste-to": self._paste_to,
            "find-duplicates-at": self._find_duplicates_at,
            "restore-empty-files-at": self._restore_empty_files_at,
        }
        string_type = GLib.VariantType.new("s")
        for name, callback in targeted_callbacks.items():
            action = Gio.SimpleAction.new(name, string_type)
            action.connect(
                "activate",
                lambda _action, target, handler=callback: handler(
                    Gio.File.new_for_uri(target.get_string())
                ),
            )
            self.add_action(action)
        self._update_action_availability()

    def _update_action_availability(self) -> None:
        tab = self.active_tab
        selected = tab.selected_items() if tab is not None else []
        single_item = selected[0] if len(selected) == 1 else None
        all_files = bool(selected) and all(not item.is_directory for item in selected)
        all_local = bool(selected) and all(
            item.file.get_path() is not None for item in selected
        )
        selected_directory = (
            single_item is not None and single_item.is_directory
        )
        availability = {
            "new-directory": tab is not None,
            "new-file": tab is not None,
            "restore-tab": bool(self._closed_tabs),
            "copy-name": bool(selected),
            "copy-path": bool(selected),
            "rename": single_item is not None,
            "undo": self.undo_manager.can_undo,
            "redo": self.undo_manager.can_redo,
            "trash": bool(selected),
            "delete": bool(selected),
            "copy": bool(selected),
            "cut": bool(selected),
            "paste": tab is not None and not self.clipboard.empty,
            "copy-to": bool(selected),
            "move-to": bool(selected),
            "duplicate": bool(selected),
            "find-duplicates": tab is not None and self.can_find_duplicates(tab.location),
            "restore-empty-files": tab is not None and self.can_restore_empty_files(tab.location),
            "create-archive": all_local,
            "extract-here": all_files and all_local,
            "extract-to": all_files and all_local,
            "properties": single_item is not None,
            "terminal": tab is not None and tab.location.get_path() is not None,
            "open": selected_directory or all_files,
            "open-with": all_files,
            "run-program": (
                single_item is not None
                and not single_item.is_directory
                and single_item.is_executable
            ),
            "open-tab": selected_directory,
            "open-pane": selected_directory and self.can_open_new_pane(),
            "open-window": selected_directory,
        }
        for name, enabled in availability.items():
            action = self.lookup_action(name)
            if action is not None:
                action.set_enabled(enabled)

    def _install_mouse_navigation(self) -> None:
        gesture = Gtk.GestureClick()
        gesture.set_button(0)
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect("pressed", self._mouse_navigation_pressed)
        self.add_controller(gesture)
        self._mouse_navigation_gesture = gesture

    def _mouse_navigation_pressed(
        self,
        gesture: Gtk.GestureClick,
        _press_count: int,
        _x: float,
        _y: float,
    ) -> None:
        self._handle_extra_mouse_button(gesture.get_current_button())

    def _handle_extra_mouse_button(self, button: int) -> bool:
        if button == MOUSE_BACK_BUTTON:
            self._with_tab("go_back")
            return True
        if button == MOUSE_FORWARD_BUTTON:
            self._with_tab("go_forward")
            return True
        return False

    def _with_tab(self, method: str) -> None:
        tab = self.active_tab
        if tab is not None:
            getattr(tab, method)()

    def _focus_location(self) -> None:
        self.location_entry.grab_focus()
        self.location_entry.select_region(0, -1)

    def _focus_search(self, global_search: bool) -> None:
        self.global_search_button.set_active(global_search)
        self.search_entry.grab_focus()

    def _toggle_hidden(self) -> None:
        if self.active_tab is not None:
            self.active_tab.show_hidden = not self.active_tab.show_hidden
            self.active_tab.refresh()

    def _toggle_places(self) -> None:
        visible = self.places_panel.get_visible()
        if visible:
            current_width = self.content_paned.get_position()
            if current_width > 0:
                self._places_width = current_width
            self.places_panel.set_visible(False)
            return
        self.places_panel.set_visible(True)
        self.content_paned.set_position(self._places_width)

    def _set_view(self, view: str) -> None:
        if self.active_tab is not None:
            self.active_tab.set_view(view)

    def _zoom(self, delta: int) -> None:
        tab = self.active_tab
        if tab is not None:
            tab.set_zoom(delta)
            self._show_tab_grid_settings(tab)

    def _new_directory(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        parent = tab.location

        def create(value: str) -> None:
            clean_name = value.strip()

            def completed(error: Exception | None, changed: list[Gio.File]) -> None:
                created_uri = None
                if error is None and changed:
                    created_uri = changed[0].get_uri()
                    state = {"directory": changed[0]}

                    def undo(done) -> None:
                        self.operations.delete_empty_directory(state["directory"], done)

                    def redo(done) -> None:
                        def created(redo_error, redo_changed):
                            if redo_error is None and redo_changed:
                                state["directory"] = redo_changed[0]
                            done(redo_error, redo_changed)

                        self.operations.create_directory(parent, clean_name, created)

                    self.undo_manager.push(UndoableAction(f"Create {clean_name}", undo, redo))
                self._operation_done(error, changed, tab)
                if (
                    created_uri is not None
                    and self._notebook_for_tab(tab) is not None
                ):
                    tab.select_uri_when_loaded(created_uri)

            self.operations.create_directory(parent, clean_name, completed)

        self.prompt_text("Create directory", "Directory name", "directory", create)

    def _new_file(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        parent = tab.location

        def create(value: str) -> None:
            clean_name = value.strip()

            def completed(error: Exception | None, changed: list[Gio.File]) -> None:
                if error is None and changed:
                    state = {"file": changed[0]}

                    def undo(done) -> None:
                        self.operations.delete_empty_file(state["file"], done)

                    def redo(done) -> None:
                        def created(redo_error, redo_changed):
                            if redo_error is None and redo_changed:
                                state["file"] = redo_changed[0]
                            done(redo_error, redo_changed)

                        self.operations.create_file(parent, clean_name, created)

                    self.undo_manager.push(
                        UndoableAction(f"Create {clean_name}", undo, redo)
                    )
                self._operation_done(error, changed)

            self.operations.create_file(parent, clean_name, completed)

        self.prompt_text("Create file", "File name", "file", create)

    def _set_clipboard_text(self, text: str) -> None:
        value = GObject.Value()
        value.init(str)
        value.set_string(text)
        self.get_clipboard().set(value)

    def _copy_selected_names(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        items = tab.selected_items()
        if not items:
            return
        names = [
            item.data.name
            or item.file.get_basename()
            or item.display_name
            for item in items
        ]
        self._set_clipboard_text("\n".join(names))
        noun = "Name" if len(names) == 1 else f"{len(names)} names"
        tab.status_label.set_text(f"{noun} copied")

    def _copy_selected_paths(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        items = tab.selected_items()
        if not items:
            return
        paths = [item.file.get_path() or item.file.get_uri() for item in items]
        self._set_clipboard_text("\n".join(paths))
        noun = "Path" if len(paths) == 1 else f"{len(paths)} paths"
        tab.status_label.set_text(f"{noun} copied")

    def _rename(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        items = tab.selected_items()
        if len(items) != 1:
            self.show_error("Rename", ValueError("Select exactly one item to rename."))
            return
        item = items[0]
        original_name = item.display_name

        def rename(value: str) -> None:
            new_name = value.strip()

            def completed(error: Exception | None, changed: list[Gio.File]) -> None:
                if error is None and changed:
                    state = {"file": changed[0]}

                    def rename_to(name: str, done) -> None:
                        def renamed(rename_error, rename_changed):
                            if rename_error is None and rename_changed:
                                state["file"] = rename_changed[0]
                            done(rename_error, rename_changed)

                        self.operations.rename(state["file"], name, renamed)

                    self.undo_manager.push(
                        UndoableAction(
                            f"Rename {original_name}",
                            lambda done: rename_to(original_name, done),
                            lambda done: rename_to(new_name, done),
                        )
                    )
                self._operation_done(error, changed)

            self.operations.rename(item.file, new_name, completed)

        self.prompt_text("Rename item", "New name", original_name, rename)

    def _undo(self) -> None:
        started = self.undo_manager.undo(self._undo_done)
        self._update_action_availability()
        if not started and self.active_tab is not None:
            self.active_tab.status_label.set_text("Nothing can be undone")

    def _redo(self) -> None:
        started = self.undo_manager.redo(self._undo_done)
        self._update_action_availability()
        if not started and self.active_tab is not None:
            self.active_tab.status_label.set_text("Nothing can be redone")

    def _undo_done(self, error: Exception | None, changed: list[Gio.File]) -> None:
        if isinstance(error, CannotUndoError):
            self.show_error("Operation cannot be undone", error)
            self._update_action_availability()
            return
        self._operation_done(error, changed)

    def _non_undoable_done(self, label: str):
        def completed(error: Exception | None, changed: list[Gio.File]) -> None:
            if changed:
                self.undo_manager.block(label)
            self._operation_done(error, changed)

        return completed

    def _copy(self, cut: bool) -> None:
        tab = self.active_tab
        if tab is None:
            return
        files = tab.selected_files()
        if not files:
            return
        self.clipboard = ClipboardState(tuple(file.get_uri() for file in files), cut)
        self._update_action_availability()
        tab.status_label.set_text(f"{'Cut' if cut else 'Copied'} {len(files)} items")

    def _paste(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        self._paste_to(tab.location)

    def _paste_to(self, destination: Gio.File) -> None:
        if self.clipboard.empty:
            return
        state = self.clipboard
        self.operations.copy_or_move(
            state.files(),
            destination,
            state.cut,
            self._non_undoable_done("Move" if state.cut else "Copy"),
            conflict="ask",
            conflict_prompt=self._present_transfer_conflict,
        )
        if state.cut:
            self.clipboard = ClipboardState()
            self._update_action_availability()

    def drop_files(
        self,
        files: list[Gio.File],
        destination: Gio.File,
        move: bool,
    ) -> bool:
        """
        Complete a drag-and-drop copy or move with interactive conflicts.
        """

        if not files:
            return False
        self.operations.copy_or_move(
            files,
            destination,
            move,
            self._non_undoable_done(
                "Drag-and-drop move" if move else "Drag-and-drop copy"
            ),
            conflict="ask",
            conflict_prompt=self._present_transfer_conflict,
        )
        return True

    def _copy_or_move_to(self, move: bool) -> None:
        tab = self.active_tab
        if tab is None or not tab.selected_files():
            return
        files = tab.selected_files()
        chooser = Gtk.FileChooserNative(
            title="Move to directory" if move else "Copy to directory",
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label="Move" if move else "Copy",
            cancel_label="Cancel",
        )

        def response(target, response_id):
            if response_id != Gtk.ResponseType.ACCEPT:
                target.destroy()
                return
            destination = target.get_file()
            target.destroy()
            if destination is not None:
                self.operations.copy_or_move(
                    files,
                    destination,
                    move,
                    self._non_undoable_done("Move" if move else "Copy"),
                    conflict="ask",
                    conflict_prompt=self._present_transfer_conflict,
                )

        chooser.connect("response", response)
        chooser.show()

    def _present_transfer_conflict(
        self,
        conflict: TransferConflict,
        respond,
    ) -> Gtk.Dialog:
        if conflict.can_merge:
            return self._present_directory_merge_conflict(conflict, respond)
        return self._present_item_conflict(conflict, respond)

    def _present_directory_merge_conflict(
        self,
        conflict: TransferConflict,
        respond,
    ) -> Gtk.MessageDialog:
        existing = (
            "A newer directory"
            if conflict.target_modified > conflict.source_modified
            else "A directory"
        )
        operation = "moved" if conflict.move else "copied"
        dialog = Gtk.MessageDialog(
            title="Merge directory",
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=f'Merge directory "{conflict.name}"?',
            secondary_text=(
                f'{existing} with the same name already exists in '
                f'"{conflict.destination_name}". Merging will ask for '
                f"confirmation before replacing any conflicting items being {operation}."
            ),
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        skip_response = 1
        merge_response = 2
        dialog.add_button("Skip", skip_response)
        dialog.add_button("Merge", merge_response)
        apply_all = Gtk.CheckButton(
            label="Apply this action to all files and directories"
        )
        apply_all.set_margin_top(8)
        dialog.get_content_area().append(apply_all)

        def response(target, response_id):
            apply_to_all = apply_all.get_active()
            target.destroy()
            actions = {
                skip_response: "skip",
                merge_response: "merge",
            }
            respond(
                ConflictDecision(
                    actions.get(response_id, "cancel"),
                    apply_all=apply_to_all,
                )
            )

        dialog.connect("response", response)
        dialog.present()
        return dialog

    @staticmethod
    def _conflict_modified_text(timestamp: int) -> str:
        if not timestamp:
            return "Unknown"
        return datetime.fromtimestamp(timestamp).strftime("%d %b %Y, %H:%M")

    def _present_item_conflict(
        self,
        conflict: TransferConflict,
        respond,
    ) -> Gtk.Dialog:
        source_kind = "directory" if conflict.source_is_directory else "file"
        target_kind = "directory" if conflict.target_is_directory else "file"
        existing = (
            f"A newer {target_kind}"
            if conflict.target_modified > conflict.source_modified
            else f"A {target_kind}"
        )
        if not conflict.source_is_directory and not conflict.target_is_directory:
            explanation = (
                f'{existing} with the same name already exists in '
                f'"{conflict.destination_name}". Replacing it will overwrite '
                "its content."
            )
        else:
            explanation = (
                f'{existing} with the same name already exists in '
                f'"{conflict.destination_name}". Replacing it will remove the '
                f"existing {target_kind} and its contents."
            )
        dialog = Gtk.Dialog(title="File conflict", transient_for=self, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        skip_response = 1
        replace_response = 2
        dialog.add_button("Skip", skip_response)
        dialog.add_button("Replace", replace_response)

        content = dialog.get_content_area()
        content.set_spacing(10)
        content.set_margin_top(14)
        content.set_margin_bottom(14)
        content.set_margin_start(14)
        content.set_margin_end(14)
        heading = Gtk.Label(
            label=f'Replace {target_kind} "{conflict.name}"?',
            xalign=0.0,
            wrap=True,
        )
        heading.add_css_class("title-3")
        content.append(heading)
        content.append(
            Gtk.Label(
                label=explanation,
                xalign=0.0,
                wrap=True,
            )
        )

        details = Gtk.Grid(column_spacing=16, row_spacing=6)
        details.attach(Gtk.Label(label="", xalign=0.0), 0, 0, 1, 1)
        details.attach(Gtk.Label(label="Source", xalign=0.0), 1, 0, 1, 1)
        details.attach(Gtk.Label(label="Destination", xalign=0.0), 2, 0, 1, 1)
        details.attach(Gtk.Label(label="Type", xalign=0.0), 0, 1, 1, 1)
        details.attach(Gtk.Label(label=source_kind.capitalize(), xalign=0.0), 1, 1, 1, 1)
        details.attach(Gtk.Label(label=target_kind.capitalize(), xalign=0.0), 2, 1, 1, 1)
        details.attach(Gtk.Label(label="Size", xalign=0.0), 0, 2, 1, 1)
        details.attach(
            Gtk.Label(
                label=(
                    "Directory"
                    if conflict.source_is_directory
                    else GLib.format_size(conflict.source_size)
                ),
                xalign=0.0,
            ),
            1,
            2,
            1,
            1,
        )
        details.attach(
            Gtk.Label(
                label=(
                    "Directory"
                    if conflict.target_is_directory
                    else GLib.format_size(conflict.target_size)
                ),
                xalign=0.0,
            ),
            2,
            2,
            1,
            1,
        )
        details.attach(Gtk.Label(label="Modified", xalign=0.0), 0, 3, 1, 1)
        details.attach(
            Gtk.Label(
                label=self._conflict_modified_text(conflict.source_modified),
                xalign=0.0,
            ),
            1,
            3,
            1,
            1,
        )
        details.attach(
            Gtk.Label(
                label=self._conflict_modified_text(conflict.target_modified),
                xalign=0.0,
            ),
            2,
            3,
            1,
            1,
        )
        content.append(details)

        rename = Gtk.CheckButton(
            label="Select a new name for the destination"
        )
        name_entry = Gtk.Entry(text=conflict.name, hexpand=True, sensitive=False)
        apply_all = Gtk.CheckButton(
            label="Apply this action to all files and directories"
        )

        def rename_toggled(button) -> None:
            active = button.get_active()
            name_entry.set_sensitive(active)
            apply_all.set_active(False)
            apply_all.set_sensitive(not active)
            if active:
                name_entry.grab_focus()
                name_entry.select_region(0, -1)

        rename.connect("toggled", rename_toggled)
        content.append(rename)
        content.append(name_entry)
        content.append(apply_all)

        def response(target, response_id):
            if response_id == replace_response and rename.get_active():
                new_name = name_entry.get_text().strip()
                if (
                    not new_name
                    or "/" in new_name
                    or new_name == conflict.name
                ):
                    name_entry.add_css_class("error")
                    name_entry.grab_focus()
                    return
                decision = ConflictDecision("rename", new_name)
            elif response_id == replace_response:
                decision = ConflictDecision(
                    "replace",
                    apply_all=apply_all.get_active(),
                )
            elif response_id == skip_response:
                decision = ConflictDecision(
                    "skip",
                    apply_all=apply_all.get_active(),
                )
            else:
                decision = ConflictDecision("cancel")
            target.destroy()
            respond(decision)

        dialog.connect("response", response)
        dialog.present()
        return dialog

    def _duplicate(self) -> None:
        tab = self.active_tab
        if tab is not None and tab.selected_files():
            self.operations.duplicate(
                tab.selected_files(),
                self._non_undoable_done("Duplicate"),
            )

    def _create_archive(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        paths = [file.get_path() for file in tab.selected_files()]
        if not paths:
            self.show_error("Create archive", ValueError("Select one or more items first."))
            return
        if any(path is None for path in paths) or tab.location.get_path() is None:
            self.show_error(
                "Create archive",
                ValueError("Select local items from a local directory."),
            )
            return
        dialog = Gtk.Dialog(title="Create archive", transient_for=self, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Create", Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_spacing(8)
        content.set_margin_top(14)
        content.set_margin_bottom(14)
        content.set_margin_start(14)
        content.set_margin_end(14)
        content.append(Gtk.Label(label="Archive name", xalign=0.0))
        name_entry = Gtk.Entry(text="Archive", activates_default=True)
        content.append(name_entry)
        content.append(Gtk.Label(label="Format", xalign=0.0))
        format_drop_down = Gtk.DropDown.new_from_strings(
            ["ZIP (.zip)", "Tar with XZ (.tar.xz)", "Tar with gzip (.tar.gz)", "7-Zip (.7z)"]
        )
        content.append(format_drop_down)
        dialog.set_default_response(Gtk.ResponseType.OK)

        def response(target, response_id):
            name = name_entry.get_text().strip()
            format_index = format_drop_down.get_selected()
            target.destroy()
            if response_id != Gtk.ResponseType.OK:
                return
            if not name or "/" in name:
                self.show_error(
                    "Create archive",
                    ValueError("Specify an archive name containing no slash."),
                )
                return
            suffixes = (".zip", ".tar.xz", ".tar.gz", ".7z")
            suffix = suffixes[format_index]
            if not name.casefold().endswith(suffix):
                name += suffix
            destination_path = str(Path(tab.location.get_path()) / name)
            self._run_archive_manager(
                [f"--add-to={destination_path}", *paths],
                "Creating archive",
            )

        dialog.connect("response", response)
        dialog.present()
        name_entry.grab_focus()
        name_entry.select_region(0, -1)

    def _extract_archive(self, choose_destination: bool) -> None:
        tab = self.active_tab
        if tab is None:
            return
        paths = [file.get_path() for file in tab.selected_files()]
        if not paths:
            self.show_error("Extract archive", ValueError("Select one or more archives first."))
            return
        if any(path is None for path in paths):
            self.show_error(
                "Extract archive",
                ValueError("Select one or more local archives."),
            )
            return
        if not choose_destination:
            self._run_archive_manager(
                ["--extract-here", *paths],
                "Extracting archive",
            )
            return
        chooser = Gtk.FileChooserNative(
            title="Extract to directory",
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label="Extract",
            cancel_label="Cancel",
        )

        def response(target, response_id):
            destination = target.get_file() if response_id == Gtk.ResponseType.ACCEPT else None
            target.destroy()
            if destination is None:
                return
            destination_path = destination.get_path()
            if destination_path is None:
                self.show_error("Extract archive", ValueError("Select a local destination directory."))
                return
            self._run_archive_manager(
                [f"--extract-to={destination_path}", *paths],
                "Extracting archive",
            )

        chooser.connect("response", response)
        chooser.show()

    def _run_archive_manager(self, arguments: list[str], status: str) -> None:
        try:
            launch_archive_manager(arguments)
            self.undo_manager.block(status)
            if self.active_tab is not None:
                self.active_tab.status_label.set_text(f"{status}…")
        except OSError as error:
            self.show_error(status, error)

    @staticmethod
    def can_find_duplicates(location: Gio.File) -> bool:
        """
        Permit duplicate inspection only at native locations outside Trash storage.
        """

        path = location.get_path()
        return path is not None and not is_trash_path(Path(path))

    def _find_duplicates(self) -> None:
        tab = self.active_tab
        if tab is not None:
            self._find_duplicates_at(tab.location)

    def _find_duplicates_at(self, location: Gio.File) -> None:
        if not self.can_find_duplicates(location):
            return
        root = Path(location.get_path())
        for dialog in self._duplicate_dialogs:
            if dialog.root == root:
                dialog.present()
                return
        dialog = DuplicateDialog(self, root)
        self._duplicate_dialogs.add(dialog)
        dialog.present()

    def duplicate_copies_done(self, result: DuplicateTrashResult) -> None:
        """
        Record partial completion and refresh affected views across both panes.
        """

        changed = [
            Gio.File.new_for_path(str(outcome.copy.candidate.path))
            for outcome in result.outcomes if outcome.status == "moved"
        ]
        if self._closing:
            return
        if changed:
            self.undo_manager.block("Move duplicate copies to Trash")
            for notebook in self.pane_notebooks:
                for page in range(notebook.get_n_pages()):
                    tab = notebook.get_nth_page(page)
                    if isinstance(tab, BrowserTab):
                        tab.files_removed(changed)
        self._update_action_availability()

    @staticmethod
    def can_restore_empty_files(location: Gio.File) -> bool:
        """
        Permit restoration review at native locations outside Trash storage.
        """

        path = location.get_path()
        return path is not None and not is_trash_path(Path(path))

    def _restore_empty_files(self) -> None:
        tab = self.active_tab
        if tab is not None:
            self._restore_empty_files_at(tab.location)

    def _restore_empty_files_at(self, location: Gio.File) -> None:
        if not self.can_restore_empty_files(location):
            return
        root = Path(location.get_path())
        for dialog in self._empty_file_dialogs:
            if dialog.root == root:
                dialog.present()
                return
        dialog = EmptyFileDialog(self, root)
        self._empty_file_dialogs.add(dialog)
        dialog.present()

    def empty_files_restored(self, result: RestoreResult) -> None:
        """
        Refresh replaced files across panes and install a barrier after any success.
        """

        if self._closing:
            return
        changed = [
            Gio.File.new_for_path(str(outcome.request.destination.path))
            for outcome in result.outcomes if outcome.status == "restored"
        ]
        if changed:
            self.undo_manager.block("Restore empty files")
            for notebook in self.pane_notebooks:
                for page in range(notebook.get_n_pages()):
                    tab = notebook.get_nth_page(page)
                    if isinstance(tab, BrowserTab):
                        tab.files_replaced(changed)
        self._update_action_availability()

    def _trash(self) -> None:
        tab = self.active_tab
        if tab is None or not tab.selected_files():
            return
        self.operations.trash(
            tab.selected_files(),
            self._non_undoable_done("Move to Trash"),
        )

    def _delete(self) -> None:
        tab = self.active_tab
        if tab is None or not tab.selected_files():
            return
        files = tab.selected_files()
        item_count = len(files)
        item_noun = "item" if item_count == 1 else "items"
        self.confirm(
            "Delete permanently?",
            f"{item_count} selected {item_noun} will be permanently deleted. "
            "This cannot be undone.",
            lambda: self.operations.delete(
                files,
                self._non_undoable_done("Permanent deletion"),
            ),
        )

    def _empty_trash(self) -> None:
        trash = Gio.File.new_for_uri("trash:///")
        try:
            enumerator = trash.enumerate_children(Gio.FILE_ATTRIBUTE_STANDARD_NAME, Gio.FileQueryInfoFlags.NONE, None)
            files = []
            while True:
                info = enumerator.next_file(None)
                if info is None:
                    break
                files.append(enumerator.get_child(info))
            enumerator.close(None)
        except GLib.Error as error:
            self.show_error("Unable to inspect Trash", error)
            return
        if not files:
            return
        self.confirm(
            "Empty Trash?",
            "Every item in Trash will be permanently deleted.",
            lambda: self.operations.delete(
                files,
                self._non_undoable_done("Empty Trash"),
            ),
        )

    def _operation_done(
        self,
        error: Exception | None,
        changed: list[Gio.File],
        target_tab: BrowserTab | None = None,
    ) -> None:
        if error is not None and not (
            isinstance(error, GLib.Error)
            and error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED)
        ):
            self.show_error("File operation failed", error)
        if target_tab is None:
            target_tab = self.active_tab
        elif self._notebook_for_tab(target_tab) is None:
            target_tab = None
        if target_tab is not None:
            target_tab.files_changed(changed)
        self._update_action_availability()

    def _open_selected(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        items = tab.selected_items()
        if len(items) == 1 and items[0].is_directory:
            tab.navigate(items[0].file)
            return
        if not items or any(item.is_directory for item in items):
            return
        self.open_items(items)

    def open_items(self, items: list[FileItem]) -> None:
        groups, unhandled = group_items_by_default_application(items)
        for application, grouped_items in groups:
            self._launch_application(
                application,
                [item.file for item in grouped_items],
            )
        for item in unhandled:
            self.open_file(item.file)

    def open_file(self, file: Gio.File) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri(
                file.get_uri(),
                self._application_launch_context(),
            )
        except GLib.Error as error:
            self.show_error("Unable to open file", error)

    def _application_launch_context(self) -> Gio.AppLaunchContext | None:
        display = self.get_display()
        return display.get_app_launch_context() if display is not None else None

    def _launch_application(
        self,
        application: Gio.AppInfo,
        files: list[Gio.File],
    ) -> bool:
        if not files:
            return False
        try:
            context = self._application_launch_context()
            if all(file.is_native() for file in files):
                application.launch(files, context)
            else:
                application.launch_uris(
                    [file.get_uri() for file in files],
                    context,
                )
            return True
        except GLib.Error as error:
            title = (
                "Unable to open files"
                if len(files) > 1
                else "Unable to open file"
            )
            self.show_error(title, error)
            return False

    def _open_selected_in_tab(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        for item in tab.selected_items():
            if item.is_directory:
                self.open_location_in_tab(item.file)

    def open_location_in_tab(self, location: Gio.File) -> None:
        self.add_tab(location, make_active=True)

    def _open_selected_in_pane(self) -> None:
        if not self.can_open_new_pane():
            return
        tab = self.active_tab
        if tab is None:
            return
        for item in tab.selected_items():
            if item.is_directory:
                self.open_location_in_pane(item.file)
                return

    def open_location_in_pane(self, location: Gio.File) -> None:
        if not self.can_open_new_pane():
            return
        notebook = self._create_pane_notebook()
        self.pane_notebooks.append(notebook)
        self.pane_split.set_end_child(notebook)
        width = self.pane_split.get_width()
        if width > 0:
            self.pane_split.set_position(width // 2)
        self._set_active_notebook(notebook)
        opened_tab = self.add_tab(
            location,
            make_active=True,
            notebook=notebook,
        )
        opened_tab.grab_view_focus()

    def _open_selected_in_window(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        for item in tab.selected_items():
            if item.is_directory:
                self.open_location_in_window(item.file)

    def open_location_in_window(self, location: Gio.File) -> None:
        self.get_application().new_window(location)

    def _selected_item_location(self) -> tuple[Gio.File, str] | None:
        tab = self.active_tab
        if tab is None:
            return None
        return tab.selected_item_location()

    def _open_selected_location_in_tab(self) -> None:
        target = self._selected_item_location()
        if target is None:
            return
        parent, item_uri = target
        opened_tab = self.add_tab(parent, make_active=True)
        opened_tab.select_uri_when_loaded(item_uri)

    def _open_selected_location_in_pane(self) -> None:
        if not self.can_open_new_pane():
            return
        target = self._selected_item_location()
        if target is None:
            return
        parent, item_uri = target
        notebook = self._create_pane_notebook()
        self.pane_notebooks.append(notebook)
        self.pane_split.set_end_child(notebook)
        width = self.pane_split.get_width()
        if width > 0:
            self.pane_split.set_position(width // 2)
        self._set_active_notebook(notebook)
        opened_tab = self.add_tab(
            parent,
            make_active=True,
            notebook=notebook,
        )
        opened_tab.select_uri_when_loaded(item_uri)
        opened_tab.grab_view_focus()

    def _open_selected_location_in_window(self) -> None:
        target = self._selected_item_location()
        if target is None:
            return
        parent, item_uri = target
        opened_window = self.get_application().new_window(parent)
        opened_tab = opened_window.active_tab
        if opened_tab is not None:
            opened_tab.select_uri_when_loaded(item_uri)

    def _open_with(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        items = tab.selected_items()
        if not items or any(item.is_directory for item in items):
            return
        applications = common_applications(items)
        dialog = Gtk.Dialog(title="Open with", transient_for=self, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        content = dialog.get_content_area()
        content.set_spacing(6)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        files = [item.file for item in items]
        for application in applications:
            button = Gtk.Button(label=application.get_display_name())
            button.connect(
                "clicked",
                lambda _button, app=application: self._launch_with_and_close(
                    app,
                    files,
                    dialog,
                ),
            )
            content.append(button)
        if not applications:
            content.append(
                Gtk.Label(
                    label="No application can open every selected file.",
                    wrap=True,
                )
            )
        dialog.connect("response", lambda target, _response: target.destroy())
        dialog.present()

    def _launch_with_and_close(
        self,
        application: Gio.AppInfo,
        files: list[Gio.File],
        dialog: Gtk.Dialog,
    ) -> None:
        if self._launch_application(application, files):
            dialog.destroy()

    def _open_terminal(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        self._open_terminal_at(tab.location)

    def _open_terminal_at(self, directory: Gio.File) -> None:
        path = directory.get_path()
        if path is None:
            self.show_error("Open in terminal", ValueError("Only local directories can be opened in a terminal."))
            return
        try:
            subprocess.Popen(
                ["x-terminal-emulator", "--working-directory", path],
                cwd=path,
            )
        except OSError as error:
            self.show_error("Unable to open terminal", error)

    def _run_selected_program(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        items = tab.selected_items()
        if (
            len(items) != 1
            or items[0].is_directory
            or not items[0].is_executable
        ):
            return
        item = items[0]
        self.choose_program_run_mode(
            item.display_name,
            lambda: self._run_program_in_background(item.file),
            lambda timestamp: self._run_program_in_terminal(item.file, timestamp),
        )

    def _local_program_details(self, file: Gio.File) -> tuple[str, str] | None:
        path = file.get_path()
        parent = file.get_parent()
        working_directory = parent.get_path() if parent is not None else None
        if path is None or working_directory is None:
            self.show_error(
                "Run as program",
                ValueError("Only local executable files can be run."),
            )
            return None
        return path, working_directory

    def _run_program_in_background(self, file: Gio.File) -> None:
        details = self._local_program_details(file)
        if details is None:
            return
        path, working_directory = details
        try:
            subprocess.Popen(
                [path],
                cwd=working_directory,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            self.show_error("Unable to run program in background", error)

    def _run_program_in_terminal(
        self,
        file: Gio.File,
        timestamp: int = Gdk.CURRENT_TIME,
    ) -> None:
        details = self._local_program_details(file)
        if details is None:
            return
        path, working_directory = details
        try:
            self._launch_terminal_with_focus(
                terminal_program_arguments(path, working_directory),
                timestamp,
            )
        except (GLib.Error, OSError) as error:
            self.show_error("Unable to run program in terminal", error)

    @staticmethod
    def _launch_terminal_with_focus(arguments: list[str], timestamp: int) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            commandline = " ".join(GLib.shell_quote(argument) for argument in arguments)
            try:
                application = Gio.AppInfo.create_from_commandline(
                    commandline,
                    "Default terminal",
                    Gio.AppInfoCreateFlags.SUPPORTS_STARTUP_NOTIFICATION,
                )
                context = display.get_app_launch_context()
                context.set_timestamp(timestamp)
                application.launch([], context)
                return
            except GLib.Error:
                pass
        subprocess.Popen(arguments)

    def _properties(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        items = tab.selected_items()
        if len(items) != 1:
            self.show_error("Properties", ValueError("Select exactly one item."))
            return
        item = items[0]
        dialog = Gtk.Dialog(title=f"{item.display_name} properties", transient_for=self, modal=True)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        grid = Gtk.Grid(column_spacing=16, row_spacing=8)
        grid.set_margin_top(16)
        grid.set_margin_bottom(16)
        grid.set_margin_start(16)
        grid.set_margin_end(16)
        values = (
            ("Name", item.display_name),
            ("Type", item.type_text),
            ("Size", item.size_text),
            ("Location", item.file.get_parent().get_parse_name() if item.file.get_parent() else item.uri),
            ("Modified", item.modified_text),
            ("Content type", item.content_type),
        )
        for row, (name, value) in enumerate(values):
            grid.attach(Gtk.Label(label=name, xalign=1.0), 0, row, 1, 1)
            grid.attach(Gtk.Label(label=value, xalign=0.0, selectable=True, wrap=True), 1, row, 1, 1)
        path = item.file.get_path()
        if path is not None and item.content_type.startswith(("image/", "audio/", "video/")):
            metadata_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            metadata_label = Gtk.Label(label="Reading metadata…", xalign=0.0, selectable=True)
            metadata_box.append(metadata_label)
            grid.attach(Gtk.Label(label="Media", xalign=1.0), 0, len(values), 1, 1)
            grid.attach(metadata_box, 1, len(values), 1, 1)

            def read_metadata() -> None:
                metadata = read_media_metadata(path, item.uri, item.content_type)
                GLib.idle_add(self._show_media_metadata, metadata_box, metadata_label, metadata)

            threading.Thread(target=read_metadata, name="dakkar-media-metadata", daemon=True).start()
        if item.is_directory and item.file.get_path() is not None:
            size_label = Gtk.Label(label="Not calculated", xalign=0.0)
            button = Gtk.Button(label="Calculate directory size")
            button.connect("clicked", lambda target: self._calculate_directory_size(item.file.get_path(), size_label, target))
            contents_row = len(values) + (1 if path is not None and item.content_type.startswith(("image/", "audio/", "video/")) else 0)
            grid.attach(Gtk.Label(label="Contents", xalign=1.0), 0, contents_row, 1, 1)
            detail = Gtk.Box(spacing=8)
            detail.append(size_label)
            detail.append(button)
            grid.attach(detail, 1, contents_row, 1, 1)
        dialog.get_content_area().append(grid)
        dialog.connect("response", lambda target, _response: target.destroy())
        dialog.present()

    @staticmethod
    def _show_media_metadata(
        box: Gtk.Box,
        placeholder: Gtk.Label,
        metadata: list[tuple[str, str]],
    ) -> bool:
        if box.get_root() is None:
            return GLib.SOURCE_REMOVE
        box.remove(placeholder)
        if not metadata:
            box.append(Gtk.Label(label="No additional metadata was found", xalign=0.0))
        for name, value in metadata:
            row = Gtk.Box(spacing=8)
            row.append(Gtk.Label(label=f"{name}:", xalign=0.0))
            row.append(Gtk.Label(label=value, xalign=0.0, selectable=True))
            box.append(row)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _calculate_directory_size(path: str, label: Gtk.Label, button: Gtk.Button) -> None:
        button.set_sensitive(False)
        label.set_text("Calculating…")

        def run() -> None:
            total = 0
            count = 0
            try:
                for root, directories, filenames in os.walk(path, followlinks=False):
                    count += len(directories) + len(filenames)
                    for filename in filenames:
                        try:
                            total += os.lstat(os.path.join(root, filename)).st_size
                        except OSError:
                            continue
                text = f"{GLib.format_size(total)} in {count} items"
            except OSError as error:
                text = str(error)
            GLib.idle_add(lambda: (label.set_text(text), False)[1])

        threading.Thread(target=run, name="dakkar-directory-size", daemon=True).start()

    def prompt_text(
        self,
        title: str,
        label: str,
        initial: str,
        callback: Callable[[str], None],
    ) -> None:
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Please", Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_spacing(8)
        content.set_margin_top(14)
        content.set_margin_bottom(14)
        content.set_margin_start(14)
        content.set_margin_end(14)
        content.append(Gtk.Label(label=label, xalign=0.0))
        entry = Gtk.Entry(text=initial, activates_default=True)
        entry.select_region(0, -1)
        content.append(entry)
        dialog.set_default_response(Gtk.ResponseType.OK)

        def response(target, response_id):
            value = entry.get_text()
            target.destroy()
            if response_id == Gtk.ResponseType.OK:
                callback(value)

        dialog.connect("response", response)
        dialog.present()
        entry.grab_focus()

    def confirm(
        self,
        title: str,
        message: str,
        callback: Callable[[], None],
    ) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=title,
            secondary_text=message,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Please", Gtk.ResponseType.OK)

        def response(target, response_id):
            target.destroy()
            if response_id == Gtk.ResponseType.OK:
                callback()

        dialog.connect("response", response)
        dialog.present()

    def choose_program_run_mode(
        self,
        display_name: str,
        background_callback: Callable[[], None],
        terminal_callback: Callable[[int], None],
    ) -> None:
        background_response = 1
        terminal_response = 2
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text="Run as program?",
            secondary_text=(
                f"Choose whether {display_name} is run without a visible terminal "
                "or in the default terminal."
            ),
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Run in background", background_response)
        terminal_button = dialog.add_button("Run in terminal", terminal_response)
        dialog.set_default_response(terminal_response)
        activation = {"timestamp": Gdk.CURRENT_TIME}

        terminal_click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        terminal_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        terminal_click.connect(
            "pressed",
            lambda controller, *_arguments: activation.update(
                timestamp=controller.get_current_event_time()
            ),
        )
        terminal_button.add_controller(terminal_click)

        key_controller = Gtk.EventControllerKey()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_controller.connect(
            "key-pressed",
            lambda controller, *_arguments: (
                activation.update(timestamp=controller.get_current_event_time()) or False
            ),
        )
        dialog.add_controller(key_controller)

        def response(target, response_id):
            target.destroy()
            if response_id == background_response:
                background_callback()
            elif response_id == terminal_response:
                terminal_callback(activation["timestamp"])

        dialog.connect("response", response)
        dialog.present()

    def show_error(self, title: str, error: Exception) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
            secondary_text=str(error),
        )
        dialog.connect("response", lambda target, _response: target.destroy())
        dialog.present()

    def _close_requested(self, _window) -> bool:
        self._closing = True
        for dialog in tuple(self._duplicate_dialogs):
            dialog.close_review()
        for dialog in tuple(self._empty_file_dialogs):
            dialog.close_review()
        self._clear_path_bar()
        for notebook in self.pane_notebooks:
            for page in range(notebook.get_n_pages()):
                tab = notebook.get_nth_page(page)
                if isinstance(tab, BrowserTab):
                    self._clear_tab_context_menu(notebook, tab)
                    tab.dispose_tab()
        self.operations.cancel()
        for handler in self._volume_monitor_handlers:
            self._volume_monitor.disconnect(handler)
        self._volume_monitor_handlers.clear()
        return False
