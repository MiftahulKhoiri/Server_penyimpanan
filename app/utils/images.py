"""Info gambar ringan. Pillow opsional: tanpa Pillow fitur ini dilewati."""

from __future__ import annotations

from pathlib import Path


def image_dimensions(path: Path, mime: str) -> tuple[int | None, int | None]:
    if not mime.startswith("image/") or mime == "image/svg+xml":
        return None, None
    try:
        from PIL import Image
    except ImportError:
        return None, None
    try:
        # Image.open hanya membaca header (lazy), tidak mendekode piksel ke RAM.
        with Image.open(path) as img:
            width, height = img.size
            return int(width), int(height)
    except Exception:
        return None, None
