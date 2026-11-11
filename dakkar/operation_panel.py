"""
Display window operation activity beside an independent tab status line.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango

from .operation_progress import OperationMonitor


class OperationPanel(Gtk.Box):
    """
    Expand operation details within the layout without replacing tab status.
    """

    def __init__(self, monitor: OperationMonitor, status_label: Gtk.Label) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.monitor = monitor
        self._disposed = False
        self._pulse_source = 0
        self.revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
            transition_duration=0,
        )
        self.append(self.revealer)
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        panel.set_margin_start(8)
        panel.set_margin_end(8)
        panel.set_margin_top(4)
        panel.set_margin_bottom(4)
        self.revealer.set_child(panel)

        heading = Gtk.Box(spacing=6)
        panel.append(heading)
        self.title_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        heading.append(self.title_label)
        self.cancel_button = Gtk.Button(label="Cancel", tooltip_text="Cancel the displayed file operation")
        self.cancel_button.connect("clicked", lambda _button: monitor.cancel_current())
        heading.append(self.cancel_button)
        self.clear_button = Gtk.Button(label="Clear", tooltip_text="Clear the completed operation result")
        self.clear_button.connect("clicked", lambda _button: monitor.clear())
        heading.append(self.clear_button)

        self.progress_bar = Gtk.ProgressBar(show_text=True, ellipsize=Pango.EllipsizeMode.END)
        self.progress_bar.add_css_class("dakkar-operation-progress")
        panel.append(self.progress_bar)
        self.result_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        panel.append(self.result_label)

        status_row = Gtk.Box(spacing=4)
        self.append(status_row)
        status_label.set_hexpand(True)
        status_label.set_ellipsize(Pango.EllipsizeMode.END)
        status_label.connect("notify::label", self._status_changed)
        self._status_changed(status_label)
        status_row.append(status_label)
        self.toggle_button = Gtk.ToggleButton()
        self.toggle_button.update_property([Gtk.AccessibleProperty.LABEL], ["File operations"])
        self.toggle_button.set_margin_end(4)
        self.toggle_button.connect("toggled", self._toggled)
        status_row.append(self.toggle_button)
        self.indicator = Gtk.Stack()
        self.indicator.add_named(Gtk.Image(icon_name="document-send-symbolic"), "idle")
        self.spinner = Gtk.Spinner()
        self.indicator.add_named(self.spinner, "busy")
        self.toggle_button.set_child(self.indicator)
        self._monitor_handler = monitor.connect("changed", self._changed)
        self.connect("map", self._changed)
        self.connect("unmap", self._changed)
        self._changed()

    def _toggled(self, _button) -> None:
        self.revealer.set_reveal_child(self.toggle_button.get_active())
        self._changed()

    @staticmethod
    def _status_changed(label: Gtk.Label, *_arguments) -> None:
        label.set_tooltip_text(label.get_text())

    def _changed(self, *_arguments) -> None:
        if self._disposed:
            return
        status = self.monitor.status
        busy = self.monitor.busy
        title = status.title
        if self.monitor.queued_count:
            title += f" · {self.monitor.queued_count} queued"
        self.title_label.set_text(title)
        self.title_label.set_tooltip_text(title)
        verb = "Hide" if self.toggle_button.get_active() else "Show"
        self.toggle_button.set_tooltip_text(f"{verb} file operations (Ctrl+Shift+O) — {title}")
        self.indicator.set_visible_child_name("busy" if busy else "idle")
        self.spinner.set_spinning(busy and self.get_mapped())
        self.cancel_button.set_visible(busy)
        self.cancel_button.set_sensitive(busy and not self.monitor.cancelling)
        self.clear_button.set_visible(self.monitor.has_result)
        self.progress_bar.set_visible(busy)
        self.progress_bar.set_text(status.description)
        self.progress_bar.set_tooltip_text(status.description)
        if status.fraction is not None:
            self.progress_bar.set_fraction(status.fraction)
        self.result_label.set_visible(not busy and bool(status.description))
        self.result_label.set_text(status.description)
        self.result_label.set_tooltip_text(status.description)
        pulse = busy and status.fraction is None and self.toggle_button.get_active() and self.get_mapped()
        if pulse and not self._pulse_source:
            self.progress_bar.set_fraction(0)
            self._pulse_source = GLib.timeout_add(100, self._pulse)
        elif not pulse and self._pulse_source:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = 0

    def _pulse(self) -> bool:
        self.progress_bar.pulse()
        return GLib.SOURCE_CONTINUE

    def dispose_panel(self) -> None:
        """
        Release subscriptions and timers when the owning tab closes.
        """

        if self._monitor_handler:
            self.monitor.disconnect(self._monitor_handler)
            self._monitor_handler = 0
        if self._pulse_source:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = 0
        self.spinner.stop()
        self._disposed = True
