"""
Construct terminal commands.
"""

from __future__ import annotations


def terminal_program_arguments(
    program_path: str,
    working_directory: str,
) -> list[str]:
    """
    Build a terminal command which sets the selected program working directory.

    The Debian GNOME Terminal wrapper silently ignores the
    non-standard `--working-directory` option. The child command therefore
    changes directory itself before the selected executable is started.
    Positional shell parameters keep both paths safe from shell interpolation.
    """

    return [
        "x-terminal-emulator",
        "-e",
        "/bin/sh",
        "-c",
        'cd -- "$1" && exec "$2"',
        "dakkar-run-program",
        working_directory,
        program_path,
    ]
