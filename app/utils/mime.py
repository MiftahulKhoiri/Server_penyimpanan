"""Deteksi MIME type ringan dari isi file (magic bytes) + ekstensi.

MIME dari klien tidak pernah dipercaya. Isi file dibaca ~4 KB pertama saja.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass

from app.models import MediaType

SNIFF_BYTES = 4096

_EXTRA_TYPES = {
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    ".webp": "image/webp",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".3gp": "video/3gpp",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".opus": "audio/ogg",
    ".apk": "application/vnd.android.package-archive",
    ".epub": "application/epub+zip",
    ".7z": "application/x-7z-compressed",
}
for _ext, _type in _EXTRA_TYPES.items():
    mimetypes.add_type(_type, _ext)

_DOCUMENT_MIMES = {
    "application/pdf",
    "application/msword",
    "application/rtf",
    "application/epub+zip",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
}
_DOCUMENT_PREFIXES = (
    "text/",
    "application/vnd.openxmlformats-officedocument.",
    "application/vnd.oasis.opendocument.",
)

_FTYP_BRANDS = {
    b"heic": "image/heic", b"heix": "image/heic", b"hevc": "image/heic", b"heim": "image/heic",
    b"mif1": "image/heif", b"msf1": "image/heif", b"avif": "image/avif", b"avis": "image/avif",
    b"qt  ": "video/quicktime", b"M4A ": "audio/mp4", b"M4B ": "audio/mp4",
    b"3gp4": "video/3gpp", b"3gp5": "video/3gpp", b"3gp6": "video/3gpp", b"3g2a": "video/3gpp2",
}


_MP3_FRAME_SYNC = (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
_GENERIC = {"application/octet-stream", None}


def is_weak_signature(head: bytes) -> bool:
    """Tanda tangan 2 byte (MP3 frame sync tanpa ID3, gzip) cukup sering muncul secara kebetulan
    di data biner/acak (±1 dari 20.000 file). Tidak boleh dipakai untuk MENOLAK file."""
    return head[:2] in _MP3_FRAME_SYNC or head.startswith(b"\x1f\x8b")


def sniff_mime(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if head[:4] == b"RIFF" and len(head) >= 12:
        return {b"WEBP": "image/webp", b"AVI ": "video/x-msvideo", b"WAVE": "audio/wav"}.get(head[8:12])
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return _FTYP_BRANDS.get(head[8:12], "video/mp4")
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm" if b"webm" in head[:64] else "video/x-matroska"
    if head.startswith(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"):
        return "video/x-ms-asf"
    if head.startswith(b"FLV\x01"):
        return "video/x-flv"
    if head[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"):
        return "video/mpeg"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        return "application/zip"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "application/x-7z-compressed"
    if head.startswith(b"Rar!\x1a\x07"):
        return "application/vnd.rar"
    if head.startswith(b"\x1f\x8b"):
        return "application/gzip"
    if head.startswith(b"ID3") or head[:2] in _MP3_FRAME_SYNC:
        return "audio/mpeg"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    return None


_ENCODING_TYPES = {"gzip": "application/gzip", "bzip2": "application/x-bzip2", "xz": "application/x-xz"}


def mime_from_extension(name: str) -> str | None:
    guessed, encoding = mimetypes.guess_type(name, strict=False)
    if encoding in _ENCODING_TYPES:
        # .gz/.tar.gz dilaporkan Python sebagai "encoding", padahal isinya arsip terkompresi.
        return _ENCODING_TYPES[encoding]
    return guessed


def media_type_for(mime: str) -> str:
    if mime.startswith("image/"):
        return MediaType.IMAGE.value
    if mime.startswith("video/"):
        return MediaType.VIDEO.value
    if mime.startswith("audio/"):
        return MediaType.AUDIO.value
    if mime in _DOCUMENT_MIMES or mime.startswith(_DOCUMENT_PREFIXES):
        return MediaType.DOCUMENT.value
    return MediaType.OTHER.value


def _family(mime: str) -> str:
    major = mime.split("/")[0]
    # Kontainer MP4/Matroska/Ogg bisa berisi audio saja atau video: dianggap satu keluarga.
    return "av" if major in ("audio", "video") else major


class MimeMismatch(Exception):
    def __init__(self, claimed: str, detected: str) -> None:
        super().__init__(f"{claimed} != {detected}")
        self.claimed = claimed
        self.detected = detected


@dataclass(frozen=True)
class MimeDecision:
    mime_type: str
    media_type: str


def decide_mime(name: str, head: bytes) -> MimeDecision:
    """Tentukan MIME final. Tolak bila ekstensi dan isi jelas berbeda jenis
    (mis. 'foto.jpg' tetapi isinya PDF/ZIP)."""
    claimed = mime_from_extension(name)
    detected = sniff_mime(head)
    if claimed is None or claimed in _GENERIC:
        # Ekstensi tidak menyatakan jenis apa pun (.bin, tanpa ekstensi): tidak ada yang bisa
        # "tidak cocok". Pakai hasil deteksi bila tanda tangannya kuat.
        if detected and not is_weak_signature(head):
            return MimeDecision(mime_type=detected, media_type=media_type_for(detected))
        return MimeDecision(mime_type="application/octet-stream", media_type=media_type_for("application/octet-stream"))
    if detected and is_weak_signature(head) and _family(claimed) != _family(detected):
        detected = None  # kebetulan semata: percayai ekstensi
    if claimed and detected and _family(claimed) != _family(detected):
        raise MimeMismatch(claimed, detected)
    if detected:
        # ZIP adalah wadah untuk docx/xlsx/apk/epub: ekstensi lebih spesifik.
        mime = claimed if detected == "application/zip" and claimed else detected
        # .m4a dengan brand 'isom' terdeteksi video/mp4: percayai ekstensi audio.
        if claimed and claimed.split("/")[0] != mime.split("/")[0]:
            mime = claimed
    else:
        mime = claimed or "application/octet-stream"
    return MimeDecision(mime_type=mime, media_type=media_type_for(mime))
