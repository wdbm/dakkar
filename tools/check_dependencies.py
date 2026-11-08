#!/usr/bin/env python3
"""
Validate distribution-managed dependencies before installation.
"""

from __future__ import annotations

import sys


def fail(message: str) -> None:
    print(f"dakkar installer: {message}", file=sys.stderr)
    raise SystemExit(1)


if sys.version_info < (3, 10):
    fail("Python 3.10 or later is required.")

try:
    import docopt  # noqa: F401
except ImportError:
    fail("python3-docopt is required.")

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("GdkPixbuf", "2.0")
    gi.require_version("GnomeDesktop", "4.0")
    from gi.repository import Gdk, GdkPixbuf, Gio, GnomeDesktop, Gtk  # noqa: F401
except (ImportError, ValueError) as error:
    fail(f"GTK/GNOME introspection dependencies are incomplete: {error}")

print("Required Dakkar dependencies are available.")
