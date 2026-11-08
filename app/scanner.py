from pathlib import Path
from typing import Iterable

VIDEO_EXTENSIONS = {".mkv"}

# The transactional patcher currently writes Matroska containers only. Other
# video formats must not enter candidate discovery until a safe patcher exists.


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
