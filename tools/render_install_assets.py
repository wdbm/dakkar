#!/usr/bin/env python3
"""
Render path-dependent installation assets.

Usage:
    render_install_assets.py <data_directory> <executable> <applications_directory> <application_id>
    render_install_assets.py (-h | --help)

Options:
    -h --help  Show this help text.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import sys

from docopt import docopt


def desktop_exec_argument(executable: str) -> str:
    """
    Quote an executable path for a freedesktop desktop-entry `Exec` field.

    Line breaks are rejected because they cannot be represented safely in a
    desktop entry. Percent signs are doubled so that paths cannot introduce a
    field code.
    """

    if "\n" in executable or "\r" in executable:
        raise ValueError("The executable path must not contain line breaks.")
    escaped = (
        executable.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace("%", "%%")
    )
    return f'"{escaped}"'


def main(arguments: Sequence[str] | None = None) -> int:
    options = docopt(__doc__, argv=arguments)
    data_directory = Path(options["<data_directory>"])
    executable = options["<executable>"]
    applications_directory = Path(options["<applications_directory>"])
    application_id = options["<application_id>"]
    template = (data_directory / f"{application_id}.desktop.in").read_text(
        encoding="utf-8"
    )
    rendered = template.replace(
        "@EXECUTABLE@",
        desktop_exec_argument(executable),
    )
    destination = applications_directory / f"{application_id}.desktop"
    destination.write_text(rendered, encoding="utf-8")
    destination.chmod(0o644)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
