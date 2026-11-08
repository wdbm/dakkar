"""
Launch the installed archive manager.
"""

from __future__ import annotations

from collections.abc import Sequence
import shutil
import subprocess


def launch_archive_manager(arguments: Sequence[str]) -> None:
    """
    Launch the archive manager with provider-specific arguments.

    Raise `FileNotFoundError` with an installation hint when the configured
    archive manager is unavailable. Propagate launch failures as `OSError`.
    """

    executable_path = shutil.which("file-roller")
    if executable_path is None:
        raise FileNotFoundError(
            "File Roller is required for archive creation and extraction. "
            "Install the file-roller package."
        )
    subprocess.Popen([executable_path, *arguments])
