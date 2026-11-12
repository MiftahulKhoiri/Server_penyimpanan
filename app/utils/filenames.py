"""Validasi nama file/folder tampilan.

Nama dari pengguna HANYA dipakai sebagai label di database. Path fisik di
disk selalu dibangkitkan server dari UUID, sehingga path traversal lewat
nama file tidak mungkin terjadi. Validasi di sini tetap ketat untuk
mencegah nama aneh yang merusak tampilan/klien.
"""

from __future__ import annotations

import re
import unicodedata

MAX_NAME_BYTES = 255
_FORBIDDEN_CHARS = re.compile(r"[\x00-\x1f\x7f/\\]")


def validate_name(raw: str) -> str:
    name = unicodedata.normalize("NFC", raw).strip()
    if not name or name in {".", ".."}:
        raise ValueError("Nama tidak boleh kosong, '.' atau '..'")
    if _FORBIDDEN_CHARS.search(name):
        raise ValueError("Nama mengandung karakter terlarang (/, \\, atau karakter kontrol)")
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise ValueError(f"Nama terlalu panjang (maksimal {MAX_NAME_BYTES} byte)")
    return name


def split_extension(name: str) -> tuple[str, str | None]:
    """'foto.JPG' -> ('foto', 'jpg'); '.bashrc' -> ('.bashrc', None)."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or not ext or len(ext) > 32:
        return name, None
    return stem, ext.lower()


def numbered_name(name: str, number: int) -> str:
    """'foto.jpg', 2 -> 'foto (2).jpg'. Tetap dalam batas 255 byte."""
    stem, ext = split_extension(name)
    suffix = f" ({number})" + (f".{name.rpartition('.')[2]}" if ext else "")
    budget = MAX_NAME_BYTES - len(suffix.encode("utf-8"))
    stem_bytes = stem.encode("utf-8")[:budget]
    return stem_bytes.decode("utf-8", errors="ignore") + suffix
