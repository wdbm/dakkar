"""
Implement the freedesktop file-manager D-Bus interface on a best-effort basis.

The well-known name is claimed only when no other file manager owns it, so
the program does not displace the desktop default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gi.repository import Gio, GLib

if TYPE_CHECKING:
    from .application import DakkarApplication


INTERFACE_XML = """
<node>
  <interface name="org.freedesktop.FileManager1">
    <method name="ShowFolders">
      <arg type="as" name="URIs" direction="in"/>
      <arg type="s" name="StartupId" direction="in"/>
    </method>
    <method name="ShowItems">
      <arg type="as" name="URIs" direction="in"/>
      <arg type="s" name="StartupId" direction="in"/>
    </method>
    <method name="ShowItemProperties">
      <arg type="as" name="URIs" direction="in"/>
      <arg type="s" name="StartupId" direction="in"/>
    </method>
    <property name="OpenLocations" type="as" access="read"/>
  </interface>
</node>
"""


class FileManagerService:
    def __init__(self, application: DakkarApplication) -> None:
        self.application = application
        self._connection: Gio.DBusConnection | None = None
        self._registration_id = 0
        self._owner_id = 0
        self._node = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)

    def start(self) -> None:
        try:
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            interface = self._node.interfaces[0]
            self._registration_id = self._connection.register_object(
                "/org/freedesktop/FileManager1",
                interface,
                self._method_call,
                self._get_property,
                None,
            )
            self._owner_id = Gio.bus_own_name_on_connection(
                self._connection,
                "org.freedesktop.FileManager1",
                Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
                None,
                None,
            )
        except GLib.Error:
            self.stop()

    def stop(self) -> None:
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0
        if self._connection is not None and self._registration_id:
            self._connection.unregister_object(self._registration_id)
            self._registration_id = 0
        self._connection = None

    def _method_call(
        self,
        _connection,
        _sender,
        _object_path,
        _interface_name,
        method_name,
        parameters,
        invocation,
    ) -> None:
        uris, _startup_id = parameters.unpack()
        GLib.idle_add(self._dispatch, method_name, tuple(uris))
        invocation.return_value(None)

    def _dispatch(self, method_name: str, uris: tuple[str, ...]) -> bool:
        for uri in uris:
            file = Gio.File.new_for_uri(uri)
            if method_name == "ShowFolders":
                self.application.new_window(file)
                continue
            parent = file.get_parent()
            window = self.application.new_window(parent or file)
            if parent is not None:
                window.active_tab.select_uri_when_loaded(
                    uri,
                    show_properties=method_name == "ShowItemProperties",
                )
        return GLib.SOURCE_REMOVE

    def _get_property(
        self,
        _connection,
        _sender,
        _object_path,
        _interface_name,
        property_name,
    ):
        if property_name != "OpenLocations":
            return None
        locations: list[str] = []
        for window in self.application.get_windows():
            notebook = getattr(window, "notebook", None)
            if notebook is None:
                continue
            for page in range(notebook.get_n_pages()):
                tab = notebook.get_nth_page(page)
                location = getattr(tab, "location", None)
                if location is not None:
                    locations.append(location.get_uri())
        return GLib.Variant("as", locations)
