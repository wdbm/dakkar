"""
Configure, rank and paint independent colour channels over item icons.

This module contains the complete visual treatment so that it can be removed
without changing directory loading, file operations or the underlying icons.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, Graphene, Gsk, Gtk


MINIMUM_OPACITY = 0.05
NEUTRAL_OPACITY = 0.35
MAXIMUM_OPACITY = 0.65
TURQUOISE = "#20f5dc"
BLUE = "#477bff"
TINT_SOURCES = (
    "none",
    "follow-sort",
    "size",
    "modified",
    "deep-modified",
)
TINT_EDGES = ("left", "right")
TINT_SCOPES = ("directories", "all")
RANKED_SORT_KEYS = frozenset({"size", "modified", "deep-modified"})
MASKED_GRADIENT_AVAILABLE = hasattr(Gsk, "MaskNode") and hasattr(Gsk, "MaskMode")


@dataclass(frozen=True, slots=True)
class TintChannelSettings:
    """
    Select the metric and edge used by one colour channel.
    """

    source: str
    edge: str

    def __post_init__(self) -> None:
        if self.source not in TINT_SOURCES:
            raise ValueError(f"Unknown tint source: {self.source}")
        if self.edge not in TINT_EDGES:
            raise ValueError(f"Unknown tint edge: {self.edge}")


@dataclass(frozen=True, slots=True)
class DirectoryTintSettings:
    """
    Store the two channels and their shared item scope.
    """

    turquoise: TintChannelSettings = TintChannelSettings("follow-sort", "right")
    blue: TintChannelSettings = TintChannelSettings("none", "right")
    scope: str = "directories"

    def __post_init__(self) -> None:
        if self.scope not in TINT_SCOPES:
            raise ValueError(f"Unknown tint scope: {self.scope}")


DEFAULT_TINT_SETTINGS = DirectoryTintSettings()


@dataclass(frozen=True, slots=True)
class TintMetrics:
    """
    Supply ranking metrics without coupling this module to application models.
    """

    uri: str
    is_directory: bool
    size: int
    modified: int
    deep_modified: int


def ranked_opacities(
    values: Mapping[str, int | float],
) -> dict[str, float]:
    """
    Map increasing metric ranks linearly onto the configured opacity range.

    Equal metrics receive equal opacity. A group with no variation receives
    the neutral opacity because neither end of the range outranks the other.
    """

    if not values:
        return {}
    levels = sorted(set(values.values()))
    if len(levels) == 1:
        return {identifier: NEUTRAL_OPACITY for identifier in values}
    interval = MAXIMUM_OPACITY - MINIMUM_OPACITY
    denominator = len(levels) - 1
    opacity_by_value = {
        value: MINIMUM_OPACITY + interval * rank / denominator
        for rank, value in enumerate(levels)
    }
    return {
        identifier: opacity_by_value[value]
        for identifier, value in values.items()
    }


class DirectoryTintController:
    """
    Hold two opacity maps independently of GTK list-item recycling.
    """

    def __init__(
        self,
        enabled: bool = True,
        settings: DirectoryTintSettings = DEFAULT_TINT_SETTINGS,
    ) -> None:
        self.enabled = enabled
        self.settings = settings
        self._turquoise_opacities: dict[str, float] = {}
        self._blue_opacities: dict[str, float] = {}

    def set_settings(self, settings: DirectoryTintSettings) -> None:
        self.settings = settings

    def update(self, items: Sequence[TintMetrics], sort_key: str) -> None:
        if not self.enabled:
            self._turquoise_opacities = {}
            self._blue_opacities = {}
            return
        eligible = tuple(
            item
            for item in items
            if self.settings.scope == "all" or item.is_directory
        )
        self._turquoise_opacities = self._channel_opacities(
            eligible,
            self.settings.turquoise,
            sort_key,
        )
        self._blue_opacities = self._channel_opacities(
            eligible,
            self.settings.blue,
            sort_key,
        )

    def _channel_opacities(
        self,
        items: Sequence[TintMetrics],
        channel: TintChannelSettings,
        sort_key: str,
    ) -> dict[str, float]:
        source = self._resolved_source(channel.source, sort_key)
        if channel.source == "none":
            return {}
        if source is None:
            return {item.uri: NEUTRAL_OPACITY for item in items}

        opacities: dict[str, float] = {}
        kinds = (True, False) if self.settings.scope == "all" else (True,)
        for is_directory in kinds:
            group = tuple(
                item
                for item in items
                if item.is_directory == is_directory
            )
            values = {
                item.uri: getattr(item, source.replace("-", "_"))
                for item in group
            }
            opacities.update(ranked_opacities(values))
        return opacities

    @staticmethod
    def _resolved_source(source: str, sort_key: str) -> str | None:
        if source == "follow-sort":
            return sort_key if sort_key in RANKED_SORT_KEYS else None
        return source

    def requires_recursive_metadata(self, sort_key: str) -> bool:
        if not self.enabled:
            return False
        for channel in (self.settings.turquoise, self.settings.blue):
            source = self._resolved_source(channel.source, sort_key)
            if channel.source != "none" and source in {"size", "deep-modified"}:
                return True
        return False

    def opacity_for(self, uri: str, colour: str = "turquoise") -> float:
        if not self.enabled:
            return 0.0
        opacities = (
            self._blue_opacities
            if colour == "blue"
            else self._turquoise_opacities
        )
        return opacities.get(uri, 0.0)

    def opacities_for(self, uri: str) -> tuple[float, float]:
        return (
            self.opacity_for(uri, "turquoise"),
            self.opacity_for(uri, "blue"),
        )


class DirectoryTintIcon(Gtk.Overlay):
    """
    Display themed artwork beneath two independently masked gradients.

    The public methods mirror the small portion of Gtk.Image used by Dakkar.
    Zero opacity leaves the original image visually unchanged.
    """

    __gtype_name__ = "DakkarDirectoryTintIcon"

    def __init__(self, pixel_size: int) -> None:
        super().__init__()
        self._image = Gtk.Image(pixel_size=pixel_size)
        self._image.set_icon_size(Gtk.IconSize.LARGE)
        self.set_child(self._image)
        self._turquoise_opacity = 0.0
        self._blue_opacity = 0.0
        self._turquoise_edge = "right"
        self._blue_edge = "right"

    def set_from_gicon(self, icon: Gio.Icon) -> None:
        self._image.set_from_gicon(icon)
        self.queue_draw()

    def set_from_icon_name(self, icon_name: str) -> None:
        self._image.set_from_icon_name(icon_name)
        self.queue_draw()

    def set_from_paintable(self, paintable: Gdk.Paintable | None) -> None:
        self._image.set_from_paintable(paintable)
        self.queue_draw()

    def set_pixel_size(self, pixel_size: int) -> None:
        self._image.set_pixel_size(pixel_size)
        self.queue_resize()

    def set_tints(
        self,
        turquoise_opacity: float,
        turquoise_edge: str,
        blue_opacity: float,
        blue_edge: str,
    ) -> None:
        state = (
            max(0.0, min(1.0, turquoise_opacity)),
            turquoise_edge if turquoise_edge in TINT_EDGES else "right",
            max(0.0, min(1.0, blue_opacity)),
            blue_edge if blue_edge in TINT_EDGES else "right",
        )
        current = (
            self._turquoise_opacity,
            self._turquoise_edge,
            self._blue_opacity,
            self._blue_edge,
        )
        if state != current:
            (
                self._turquoise_opacity,
                self._turquoise_edge,
                self._blue_opacity,
                self._blue_edge,
            ) = state
            self.queue_draw()

    def set_tint_opacity(self, opacity: float) -> None:
        self.set_tints(opacity, "right", 0.0, "right")

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        Gtk.Overlay.do_snapshot(self, snapshot)
        if (
            not MASKED_GRADIENT_AVAILABLE
            or (self._turquoise_opacity <= 0.0 and self._blue_opacity <= 0.0)
            or self.get_width() <= 0
            or self.get_height() <= 0
        ):
            return

        mask_snapshot = Gtk.Snapshot.new()
        self.snapshot_child(self._image, mask_snapshot)
        mask_node = mask_snapshot.to_node()
        if mask_node is None:
            return
        self._append_tint(
            snapshot,
            mask_node,
            TURQUOISE,
            self._turquoise_opacity,
            self._turquoise_edge,
        )
        self._append_tint(
            snapshot,
            mask_node,
            BLUE,
            self._blue_opacity,
            self._blue_edge,
        )

    def _append_tint(
        self,
        snapshot: Gtk.Snapshot,
        mask_node: Gsk.RenderNode,
        colour: str,
        opacity: float,
        edge: str,
    ) -> None:
        if opacity <= 0.0:
            return
        bounds = Graphene.Rect().init(
            0.0,
            0.0,
            float(self.get_width()),
            float(self.get_height()),
        )
        start = Graphene.Point().init(0.0, 0.0)
        end = Graphene.Point().init(float(self.get_width()), 0.0)
        gradient_snapshot = Gtk.Snapshot.new()
        gradient_snapshot.append_linear_gradient(
            bounds,
            start,
            end,
            [
                self._colour_stop(
                    0.0,
                    colour,
                    opacity if edge == "left" else 0.0,
                ),
                self._colour_stop(
                    1.0,
                    colour,
                    opacity if edge == "right" else 0.0,
                ),
            ],
        )
        gradient_node = gradient_snapshot.to_node()
        if gradient_node is None:
            return
        snapshot.append_node(
            Gsk.MaskNode.new(
                gradient_node,
                mask_node,
                Gsk.MaskMode.ALPHA,
            )
        )

    @staticmethod
    def _colour_stop(
        offset: float,
        colour_value: str,
        opacity: float,
    ) -> Gsk.ColorStop:
        colour = Gdk.RGBA()
        colour.parse(colour_value)
        colour.alpha = opacity
        stop = Gsk.ColorStop()
        stop.offset = offset
        stop.color = colour
        return stop
