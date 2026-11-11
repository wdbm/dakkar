"""
Provide the GTK application.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from .file_manager_service import FileManagerService
from .window import DakkarWindow


APP_ID = "io.github.wdbm.dakkar"


def application_css(system_theme: bool) -> bytes:
    """
    Build structural CSS with either the Dakkar palette or GTK theme colours.
    """

    palette = ""
    border_colour = "currentColor" if system_theme else "#ffffff"
    active_pane_colour = "currentColor" if system_theme else "#4f8cff"
    if not system_theme:
        palette = """
        window, .view { background-color: #111318; color: #f3f4f6; }
        .dakkar-sidebar { background-color: #171a20; }
        .dakkar-status { color: #c7cad1; }
        .dakkar-grid-cell:selected { background-color: #0033a0; }
        """
    return f"""
        {palette}
        .dakkar-pathbar {{ padding: 4px 8px; }}
        .dakkar-status {{ padding: 5px 10px; }}
        .dakkar-operation-progress, .dakkar-operation-progress text {{ color: inherit; }}
        .dakkar-operation-progress text {{ font-size: 1em; }}
        .dakkar-grid-cell {{ padding: 8px; border-radius: 8px; }}
        .dakkar-grid-label {{ font-size: 0.92em; }}
        .dakkar-high-contrast button {{
            border: 1px solid {border_colour};
        }}
        .dakkar-high-contrast notebook > header > tabs > tab {{
            border: 1px solid {border_colour};
        }}
        .dakkar-high-contrast .dakkar-notebook > header {{
            border: 1px solid {border_colour};
        }}
        .dakkar-high-contrast .dakkar-file-area,
        .dakkar-high-contrast .dakkar-navigation-area,
        .dakkar-high-contrast .dakkar-places-panel,
        .dakkar-high-contrast .dakkar-places-heading,
        .dakkar-high-contrast .dakkar-status,
        .dakkar-high-contrast .dakkar-location-entry,
        .dakkar-high-contrast .dakkar-search-entry {{
            border: 1px solid {border_colour};
        }}
        .dakkar-active-pane {{
            box-shadow: inset 0 0 0 2px {active_pane_colour};
        }}
    """.encode("utf-8")


class DakkarApplication(Gtk.Application):
    def __init__(
        self,
        use_index: bool = True,
        open_separate_windows: bool = False,
        service_mode: bool = False,
        high_contrast: bool = True,
        directory_tint: bool = False,
        system_theme: bool = False,
    ) -> None:
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if not service_mode:
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.use_index = use_index
        self.open_separate_windows = open_separate_windows
        self.high_contrast = high_contrast
        self.directory_tint = directory_tint
        self.system_theme = system_theme
        self.file_manager_service: FileManagerService | None = None

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        settings = Gtk.Settings.get_default()
        if settings is not None and not self.system_theme:
            settings.set_property("gtk-application-prefer-dark-theme", True)
        display = Gdk.Display.get_default()
        if display is not None:
            css = Gtk.CssProvider()
            css.load_from_data(application_css(self.system_theme))
            Gtk.StyleContext.add_provider_for_display(
                display,
                css,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        self._install_accelerators()
        self.file_manager_service = FileManagerService(self)
        self.file_manager_service.start()

    def do_shutdown(self) -> None:
        if self.file_manager_service is not None:
            self.file_manager_service.stop()
            self.file_manager_service = None
        Gtk.Application.do_shutdown(self)

    def _install_accelerators(self) -> None:
        bindings = {
            "win.new-tab": ["<Primary>t"],
            "win.close-tab": ["<Primary>w"],
            "win.restore-tab": ["<Primary><Shift>t"],
            "win.new-window": ["<Primary>n"],
            "win.back": ["<Alt>Left", "Back"],
            "win.forward": ["<Alt>Right", "Forward"],
            "win.up": ["<Alt>Up"],
            "win.home": ["<Alt>Home"],
            "win.refresh": ["F5", "<Primary>r"],
            "win.location": ["<Primary>l"],
            "win.search": ["<Primary>f"],
            "win.global-search": ["<Primary><Shift>f"],
            "win.stop-search": ["Escape"],
            "win.keyboard-shortcuts": ["F1"],
            "win.show-hidden": ["<Primary>h"],
            "win.toggle-places": ["F9"],
            "win.toggle-operations": ["<Primary><Shift>o"],
            "win.list-view": ["<Primary>1"],
            "win.grid-view": ["<Primary>2"],
            "win.expandable-list-view": ["<Primary>3"],
            "win.new-directory": ["<Primary><Shift>n"],
            "win.rename": ["F2"],
            "win.trash": ["Delete"],
            "win.delete": ["<Shift>Delete"],
            "win.copy": ["<Primary>c"],
            "win.cut": ["<Primary>x"],
            "win.paste": ["<Primary>v"],
            "win.duplicate": ["<Primary>d"],
            "win.undo": ["<Primary>z"],
            "win.redo": ["<Primary><Shift>z"],
            "win.properties": ["<Alt>Return", "<Primary>i"],
            "win.terminal": ["<Primary>period"],
            "win.select-all": ["<Primary>a"],
            "win.zoom-in": ["<Primary>plus", "<Primary>equal"],
            "win.zoom-out": ["<Primary>minus"],
        }
        for action, accelerators in bindings.items():
            self.set_accels_for_action(action, accelerators)

    def new_window(self, location: Gio.File | None = None) -> DakkarWindow:
        window = DakkarWindow(
            self,
            use_index=self.use_index,
            high_contrast=self.high_contrast,
            directory_tint=self.directory_tint,
        )
        window.add_tab(location or Gio.File.new_for_path(GLib.get_home_dir()))
        window.present()
        return window

    def do_activate(self) -> None:
        self.new_window()

    def do_open(
        self,
        files: list[Gio.File],
        _count: int,
        _hint: str,
    ) -> None:
        locations = list(files)
        if not locations:
            self.do_activate()
            return
        if self.open_separate_windows:
            for location in locations:
                self.new_window(location)
            return
        window = self.new_window(locations[0])
        for location in locations[1:]:
            window.add_tab(location)
