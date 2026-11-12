"""Thumbnail & metadata media (ringan, opsional).

- Foto: Pillow dengan mode `draft` (JPEG didekode langsung pada skala kecil,
  hemat RAM), rotasi EXIF diterapkan. Format yang tidak didukung Pillow (mis.
  HEIC) dicoba lewat ffmpeg bila tersedia.
- Video: satu frame diambil dengan ffmpeg (1 thread, prioritas rendah).
- Thumbnail dibuat saat pertama diminta lalu disimpan di
  PRIMARY_STORAGE_PATH/.thumbs, maksimal 1 proses pembuatan bersamaan.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.models import MediaType, StoredFile
from app.services.file_service import physical_path
from app.services.storage_paths import DIR_MODE, StoragePaths, UnsafePathError

logger = logging.getLogger("server_penyimpanan.media")

_generation_slot = threading.BoundedSemaphore(1)
_failed_lock = threading.Lock()
_failed_thumbnails: set[str] = set()
_failed_probes: set[str] = set()
_MAX_FAILED = 10_000

THUMBNAIL_MEDIA = (MediaType.IMAGE.value, MediaType.VIDEO.value)
STREAMABLE_MEDIA = (MediaType.VIDEO.value, MediaType.AUDIO.value, MediaType.IMAGE.value)


def _remember_failure(bucket: set[str], file_id: str) -> None:
    with _failed_lock:
        if len(bucket) >= _MAX_FAILED:
            bucket.clear()
        bucket.add(file_id)


def _has_failed(bucket: set[str], file_id: str) -> bool:
    with _failed_lock:
        return file_id in bucket


def reset_failure_cache() -> None:
    with _failed_lock:
        _failed_thumbnails.clear()
        _failed_probes.clear()


def tool_available(command: str) -> bool:
    return shutil.which(command) is not None


def thumbnail_relpath(owner_id: int, file_id: str) -> str:
    StoragePaths.file_relpath(owner_id, file_id)  # validasi UUID
    return f".thumbs/{int(owner_id)}/{file_id[:2]}/{file_id}.jpg"


def _lower_priority() -> None:  # dijalankan di proses anak sebelum exec
    try:
        os.nice(10)
    except OSError:
        pass


def _run_tool(settings: Settings, args: list[str]) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=settings.MEDIA_TOOL_TIMEOUT_SECONDS,
            preexec_fn=_lower_priority,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Tool media gagal dijalankan (%s): %s", args[0], exc.__class__.__name__)
        return None


# ---------------- Thumbnail ----------------
_OK, _UNSUPPORTED, _REJECTED = "ok", "unsupported", "rejected"


def _pillow_thumbnail(src: Path, dest: Path, settings: Settings) -> str:
    """Return _OK, _UNSUPPORTED (boleh dicoba ffmpeg), atau _REJECTED (jangan dicoba lagi)."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return _UNSUPPORTED
    size = settings.THUMBNAIL_SIZE
    try:
        with Image.open(src) as img:
            width, height = img.size
            if width * height > settings.THUMBNAIL_MAX_PIXELS:
                logger.info("Gambar terlalu besar untuk thumbnail (%dx%d)", width, height)
                return _REJECTED
            img.draft("RGB", (size * 2, size * 2))  # JPEG: dekode skala 1/2..1/8
            img = ImageOps.exif_transpose(img)
            img.thumbnail((size, size))
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.getchannel("A"))
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.save(dest, "JPEG", quality=settings.THUMBNAIL_QUALITY)
        return _OK
    except Image.DecompressionBombError:
        return _REJECTED
    except Image.UnidentifiedImageError:
        return _UNSUPPORTED  # mis. HEIC tanpa plugin: coba ffmpeg
    except Exception as exc:  # file rusak
        logger.info("Pillow tidak bisa membuat thumbnail: %s", exc.__class__.__name__)
        return _REJECTED


def _ffmpeg_thumbnail(src: Path, dest: Path, settings: Settings, seek: float | None) -> bool:
    if not tool_available(settings.FFMPEG_PATH):
        return False
    size = settings.THUMBNAIL_SIZE
    args = [settings.FFMPEG_PATH, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1"]
    if seek:
        args += ["-ss", f"{seek:.2f}"]
    args += [
        "-i", str(src), "-frames:v", "1",
        "-vf", f"scale={size}:{size}:force_original_aspect_ratio=decrease",
        "-c:v", "mjpeg", "-q:v", "5", "-f", "image2", "-y", str(dest),
    ]
    result = _run_tool(settings, args)
    return bool(result and result.returncode == 0 and dest.is_file() and dest.stat().st_size > 0)


def get_or_create_thumbnail(db: Session, settings: Settings, storage: StoragePaths, row: StoredFile) -> Path:
    unavailable = AppError(404, "thumbnail_unavailable", "Thumbnail tidak tersedia untuk file ini")
    if not settings.THUMBNAIL_ENABLED or row.media_type not in THUMBNAIL_MEDIA or row.mime_type == "image/svg+xml":
        raise unavailable
    if row.thumbnail_path:
        try:
            cached = storage.resolve(row.thumbnail_path)
            if cached.is_file():
                return cached
        except UnsafePathError:
            pass
    if _has_failed(_failed_thumbnails, row.id):
        raise unavailable

    src = physical_path(storage, row)
    relpath = thumbnail_relpath(row.owner_id, row.id)
    dest = storage.resolve(relpath)
    dest.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    temp = dest.with_name(dest.name + ".tmp")

    if not _generation_slot.acquire(timeout=60):
        raise AppError(503, "thumbnail_busy", "Server sedang membuat thumbnail lain, coba lagi", {"Retry-After": "5"})
    try:
        if row.media_type == MediaType.IMAGE.value:
            result = _pillow_thumbnail(src, temp, settings)
            ok = result == _OK or (result == _UNSUPPORTED and _ffmpeg_thumbnail(src, temp, settings, None))
        else:
            duration = row.duration_seconds
            seek = 1.0 if duration is None or duration >= 2 else 0.0
            ok = _ffmpeg_thumbnail(src, temp, settings, seek) or (
                bool(seek) and _ffmpeg_thumbnail(src, temp, settings, None)
            )
    finally:
        _generation_slot.release()

    if not ok:
        temp.unlink(missing_ok=True)
        _remember_failure(_failed_thumbnails, row.id)
        raise unavailable
    os.replace(temp, dest)
    row.thumbnail_path = relpath
    db.commit()
    return dest


def remove_thumbnail(storage: StoragePaths, row: StoredFile) -> None:
    if row.thumbnail_path:
        try:
            storage.resolve(row.thumbnail_path).unlink(missing_ok=True)
        except UnsafePathError:
            pass


# ---------------- Metadata ----------------
def probe_media(settings: Settings, path: Path) -> dict[str, float | int] | None:
    if not tool_available(settings.FFPROBE_PATH):
        return None
    result = _run_tool(settings, [
        settings.FFPROBE_PATH, "-v", "error", "-print_format", "json",
        "-show_entries", "format=duration:stream=codec_type,width,height", str(path),
    ])
    if not result or result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or b"{}")
    except ValueError:
        return None
    info: dict[str, float | int] = {}
    try:
        duration = float(data.get("format", {}).get("duration"))
        if duration >= 0:
            info["duration_seconds"] = round(duration, 3)
    except (TypeError, ValueError):
        pass
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and stream.get("width") and stream.get("height"):
            info["width"] = int(stream["width"])
            info["height"] = int(stream["height"])
            break
    return info


def ensure_media_info(db: Session, settings: Settings, storage: StoragePaths, row: StoredFile) -> StoredFile:
    """Lengkapi durasi/resolusi video & audio secara malas (sekali, lalu disimpan)."""
    if row.media_type not in (MediaType.VIDEO.value, MediaType.AUDIO.value):
        return row
    if row.duration_seconds is not None or _has_failed(_failed_probes, row.id):
        return row
    info = probe_media(settings, physical_path(storage, row))
    if not info:
        _remember_failure(_failed_probes, row.id)
        return row
    row.duration_seconds = info.get("duration_seconds")  # type: ignore[assignment]
    if "width" in info:
        row.width = int(info["width"])
        row.height = int(info["height"])
    db.commit()
    return row
