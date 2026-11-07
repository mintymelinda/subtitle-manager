from pathlib import Path
from typing import Iterable

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".webm"
}


def scan_media(root: Path) -> Iterable[Path]:
    """Yield supported media files under root without following directory symlinks."""
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Media root is not a directory: {root}")

    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                continue
            if resolved.suffix.lower() in VIDEO_EXTENSIONS:
                yield resolved
        except (OSError, RuntimeError):
            continue
