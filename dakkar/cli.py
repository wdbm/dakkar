"""
Dakkar is a GNOME file browser.

Usage:
    dakkar [options] [--] [<location>...]
    dakkar (-h | --help)
    dakkar --version

Options:
    -h --help               Show this help text.
    --version               Show the version.
    -n --new-window         Open every supplied location in a separate window.
    --high-contrast         Use thin borders around principal controls [default].
    --no-high-contrast      Disable the default high-contrast borders.
    --directory-tint        Enable colour ranking overlays on item icons.
    --no-directory-tint     Explicitly disable colour ranking overlays [default].
    --system-theme          Inherit colours and light or dark appearance from GTK.
    --no-index              Do not use the optional filename index.
    --gapplication-service  Run as a D-Bus application service.

Locations may be local paths or GIO URIs. With no locations, the current
directory is opened. Use -- before a location whose name starts with a hyphen.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Sequence
from urllib.parse import urlparse

from docopt import DocoptExit, docopt

from . import __VERSION__


def location_argument_to_uri(value: str) -> str:
    """
    Convert a command-line path or URI into a canonical URI.
    """

    parsed = urlparse(value)
    if parsed.scheme and not (len(parsed.scheme) == 1 and value[1:2] == ":"):
        return value
    return Path(value).expanduser().resolve().as_uri()


def high_contrast_from_options(options: dict[str, object]) -> bool:
    """
    Resolve the default-on high-contrast command-line options.
    """

    if options["--high-contrast"] and options["--no-high-contrast"]:
        raise ValueError("--high-contrast and --no-high-contrast cannot be used together")
    return not bool(options["--no-high-contrast"])


def directory_tint_from_options(options: dict[str, object]) -> bool:
    """
    Resolve the default-off directory-tint command-line options.
    """

    if options["--directory-tint"] and options["--no-directory-tint"]:
        raise ValueError(
            "--directory-tint and --no-directory-tint cannot be used together"
        )
    return bool(options["--directory-tint"])


def main(argv: Sequence[str] | None = None) -> int:
    try:
        options = docopt(__doc__, argv=argv, version=f"Dakkar {__VERSION__}")
    except DocoptExit as error:
        print(str(error), file=sys.stderr)
        return 2

    try:
        high_contrast = high_contrast_from_options(options)
        directory_tint = directory_tint_from_options(options)
    except ValueError as error:
        print(f"dakkar: {error}.", file=sys.stderr)
        return 2

    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk, Gio

        from .application import DakkarApplication
    except (ImportError, ValueError) as error:
        print(f"dakkar: missing dependency: {error}.", file=sys.stderr)
        return 1

    if Gdk.Display.get_default() is None:
        print(
            "dakkar: no graphical display is available. A desktop session is required.",
            file=sys.stderr,
        )
        return 1

    values = options["<location>"] or [str(Path.cwd())]
    try:
        locations = [Gio.File.new_for_uri(location_argument_to_uri(value)) for value in values]
    except (OSError, ValueError) as error:
        print(f"dakkar: invalid location: {error}", file=sys.stderr)
        return 2

    application = DakkarApplication(
        use_index=not options["--no-index"],
        open_separate_windows=options["--new-window"],
        service_mode=options.get("--gapplication-service", False),
        high_contrast=high_contrast,
        directory_tint=directory_tint,
        system_theme=options["--system-theme"],
    )
    try:
        arguments = ["dakkar"]
        if options.get("--gapplication-service", False):
            arguments.append("--gapplication-service")
        arguments.extend(location.get_uri() for location in locations)
        try:
            return application.run(arguments)
        except KeyboardInterrupt:
            return 130
    except Exception as error:
        # The command boundary converts unexpected application failures into
        # a concise diagnostic and a non-zero exit status.
        print(f"dakkar: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
