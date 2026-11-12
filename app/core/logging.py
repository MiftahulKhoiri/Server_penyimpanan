"""Konfigurasi logging ke stdout (ditangkap journald saat jalan via systemd)."""

from __future__ import annotations

import logging

_CONFIGURED = False


def configure_logging(level: str) -> None:
    global _CONFIGURED
    root = logging.getLogger("server_penyimpanan")
    root.setLevel(level)
    if _CONFIGURED:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True
