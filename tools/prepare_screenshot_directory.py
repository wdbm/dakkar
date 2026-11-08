#!/usr/bin/env python3
"""
Create a synthetic directory for repository screenshots.

Usage:
    prepare_screenshot_directory.py [--directory=<path>] [--reset]
    prepare_screenshot_directory.py (-h | --help)
    prepare_screenshot_directory.py --version

Options:
    -h --help           Show this help text.
    --version           Show the version.
    --directory=<path>  Select the showcase directory
                        [default: /tmp/Dakkar-showcase].
    --reset             Recreate a showcase previously made by this tool.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import struct
import zlib

from docopt import docopt


__VERSION__ = "2026-11-08T0050Z"
MARKER_FILENAME = ".dakkar-showcase.json"
IMAGE_WIDTH = 480
IMAGE_HEIGHT = 300


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    """
    Return one encoded PNG chunk.
    """

    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(data, checksum)
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", checksum & 0xFFFFFFFF)
    )


def create_gradient_png(
    path: Path,
    start_colour: tuple[int, int, int],
    end_colour: tuple[int, int, int],
    accent_colour: tuple[int, int, int],
) -> None:
    """
    Create a deterministic abstract PNG without external image libraries.
    """

    rows = bytearray()
    for y_position in range(IMAGE_HEIGHT):
        rows.append(0)
        vertical = y_position / max(IMAGE_HEIGHT - 1, 1)
        for x_position in range(IMAGE_WIDTH):
            horizontal = x_position / max(IMAGE_WIDTH - 1, 1)
            blend = 0.72 * horizontal + 0.28 * vertical
            colour = [
                round(start + (end - start) * blend)
                for start, end in zip(start_colour, end_colour)
            ]
            wave = math.sin(horizontal * math.tau * 2.5 + vertical * 5.0)
            centre_x = 0.72 + wave * 0.035
            centre_y = 0.38
            distance = math.hypot(horizontal - centre_x, vertical - centre_y)
            accent_strength = max(0.0, 1.0 - distance / 0.32) ** 2
            for channel in range(3):
                colour[channel] = round(
                    colour[channel] * (1.0 - accent_strength)
                    + accent_colour[channel] * accent_strength
                )
            rows.extend(colour)

    header = struct.pack(">IIBBBBB", IMAGE_WIDTH, IMAGE_HEIGHT, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(bytes(rows), level=8))
        + png_chunk(b"IEND", b"")
    )


def create_project_map(path: Path) -> None:
    """
    Create a synthetic vector project map.
    """

    path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" width="960" height="600" viewBox="0 0 960 600">
<defs><linearGradient id="background"><stop stop-color="#052f38"/><stop offset="1" stop-color="#176b87"/></linearGradient></defs>
<rect width="960" height="600" rx="36" fill="url(#background)"/>
<g fill="none" stroke="#d8ffff" stroke-width="10" opacity=".82"><path d="M170 300h180M610 300h180M480 155v95M480 350v95"/></g>
<g fill="#39e6d0" stroke="#f5ffff" stroke-width="5"><circle cx="170" cy="300" r="62"/><circle cx="480" cy="130" r="62"/><circle cx="480" cy="300" r="78"/><circle cx="790" cy="300" r="62"/><circle cx="480" cy="470" r="62"/></g>
</svg>
""",
        encoding="utf-8",
    )


def create_text_file(path: Path, title: str, line_count: int) -> None:
    """
    Create deterministic synthetic prose with a selected approximate size.
    """

    lines = [f"# {title}", ""]
    for index in range(1, line_count + 1):
        lines.append(
            f"{index:04d}. Synthetic showcase entry for layout, sorting and search."
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_sparse_file(path: Path, size_bytes: int) -> None:
    """
    Create a labelled sparse file with a selected logical size.
    """

    with path.open("wb") as output:
        output.write(b"Synthetic Dakkar showcase data.\n")
        output.truncate(size_bytes)


def set_staggered_times(paths: list[Path], root: Path) -> None:
    """
    Apply varied recent modification times without recording private metadata.
    """

    now = datetime.now(timezone.utc).replace(microsecond=0)
    files = [path for path in paths if path.is_file()]
    directories = sorted(
        (path for path in paths if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for index, path in enumerate(files):
        modified = now - timedelta(hours=(index * 7) % (24 * 18))
        timestamp = modified.timestamp()
        os.utime(path, (timestamp, timestamp))
    for index, path in enumerate(directories):
        modified = now - timedelta(hours=3 + index * 11)
        timestamp = modified.timestamp()
        os.utime(path, (timestamp, timestamp))
    os.utime(root, (now.timestamp(), now.timestamp()))


def populate_showcase(root: Path) -> None:
    """
    Populate a showcase using invented directories, files and artwork.
    """

    directory_names = (
        "Archive",
        "Design assets",
        "Documents",
        "Field recordings",
        "Photography",
        "Project notes",
        "Reference material",
        "Shared",
    )
    for name in directory_names:
        (root / name).mkdir(parents=True)
    (root / "Design assets" / "Drafts").mkdir()
    (root / "Documents" / "Reports").mkdir()
    (root / "Photography" / "Selections").mkdir()
    (root / "Project notes" / "Ideas").mkdir()

    create_gradient_png(
        root / "Aurora study.png",
        (4, 55, 68),
        (20, 220, 195),
        (225, 255, 250),
    )
    create_gradient_png(
        root / "Cobalt geometry.png",
        (8, 25, 88),
        (42, 128, 230),
        (115, 245, 255),
    )
    create_gradient_png(
        root / "Coral horizon.png",
        (70, 20, 62),
        (255, 116, 104),
        (255, 232, 174),
    )
    create_gradient_png(
        root / "Verdant field.png",
        (5, 56, 49),
        (77, 190, 112),
        (220, 255, 166),
    )
    create_project_map(root / "Project map.svg")
    create_text_file(root / "Overview.md", "Showcase overview", 18)
    create_text_file(root / "Release notes.txt", "Release notes", 34)
    create_text_file(
        root / "Comparative layout notes for the autumn review.md",
        "Comparative layout notes",
        52,
    )
    create_text_file(root / "Sample catalogue.csv", "Sample catalogue", 140)

    nested_files = (
        ("Archive/Checksums.txt", 90),
        ("Design assets/Palette notes.md", 36),
        ("Design assets/Drafts/Interface sketch.txt", 58),
        ("Documents/Agenda.md", 22),
        ("Documents/Reports/Quarterly summary.txt", 110),
        ("Field recordings/Recording log.txt", 74),
        ("Photography/Contact sheet notes.md", 44),
        ("Photography/Selections/Captions.txt", 67),
        ("Project notes/Milestones.md", 83),
        ("Project notes/Ideas/Experiments.txt", 96),
        ("Reference material/Index.txt", 125),
        ("Shared/Review comments.md", 48),
    )
    for relative_path, line_count in nested_files:
        path = root / relative_path
        create_text_file(path, path.stem, line_count)

    create_sparse_file(root / "Archive" / "Release bundle.dat", 2_400_000)
    create_sparse_file(root / "Field recordings" / "Waveform sample.dat", 820_000)
    create_sparse_file(root / "Reference material" / "Reference index.dat", 310_000)
    create_sparse_file(root / "Shared" / "Transfer sample.dat", 96_000)

    marker = root / MARKER_FILENAME
    marker.write_text(
        json.dumps(
            {
                "format": 1,
                "purpose": "Synthetic Dakkar repository screenshot data",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    set_staggered_times(list(root.rglob("*")), root)


def prepare_directory(directory_path: Path, reset: bool) -> None:
    """
    Validate, create and populate the selected showcase directory.
    """

    directory_path = directory_path.expanduser().absolute()
    marker = directory_path / MARKER_FILENAME
    if directory_path.exists():
        if not directory_path.is_dir():
            raise SystemExit(f"Showcase path is not a directory: {directory_path}")
        if marker.exists():
            if not reset:
                print(f"Synthetic showcase already exists at {directory_path}")
                print(f"Open it with: dakkar {shlex.quote(str(directory_path))}")
                return
            shutil.rmtree(directory_path)
        elif any(directory_path.iterdir()):
            raise SystemExit(
                "Refusing to alter a non-empty directory which was not created "
                f"by this tool: {directory_path}"
            )
    directory_path.mkdir(parents=True, exist_ok=True)
    populate_showcase(directory_path)
    print(f"Synthetic showcase created at {directory_path}")
    print(f"Open it with: dakkar {shlex.quote(str(directory_path))}")


def main() -> int:
    """
    Parse command-line options and prepare the showcase directory.
    """

    options = docopt(__doc__, version=__VERSION__)
    prepare_directory(
        Path(options["--directory"]),
        bool(options["--reset"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
