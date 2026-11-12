"""Pembatasan percobaan login (in-memory).

Disimpan di memori proses: cocok untuk deployment 1 worker uvicorn di
Raspberry Pi. Penghitung hilang ketika server restart (risiko diterima).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterable


class LoginRateLimiter:
    _MAX_KEYS = 10_000

    def __init__(
        self,
        max_attempts: int,
        window_seconds: int,
        lockout_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_attempts = max_attempts
        self.window = window_seconds
        self.lockout = lockout_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def keys(ip: str | None, username: str) -> tuple[str, str]:
        return f"ip:{ip or 'unknown'}", f"user:{username.lower()}"

    def _limit_for(self, key: str) -> int:
        # IP mendapat batas lebih longgar (bisa beberapa perangkat di balik NAT rumah).
        return self.max_attempts * 4 if key.startswith("ip:") else self.max_attempts

    def retry_after(self, keys: Iterable[str]) -> int | None:
        """Kembalikan detik tunggu jika salah satu key sedang terkunci."""
        now = self._clock()
        wait = 0.0
        with self._lock:
            for key in keys:
                until = self._locked_until.get(key)
                if until is None:
                    continue
                if until > now:
                    wait = max(wait, until - now)
                else:
                    del self._locked_until[key]
        return int(wait) + 1 if wait > 0 else None

    def register_failure(self, keys: Iterable[str]) -> None:
        now = self._clock()
        with self._lock:
            if len(self._failures) > self._MAX_KEYS:
                self._prune(now)
            for key in keys:
                attempts = self._failures.setdefault(key, deque())
                attempts.append(now)
                while attempts and attempts[0] < now - self.window:
                    attempts.popleft()
                if len(attempts) >= self._limit_for(key):
                    self._locked_until[key] = now + self.lockout
                    attempts.clear()

    def reset(self, keys: Iterable[str]) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)
                self._locked_until.pop(key, None)

    def _prune(self, now: float) -> None:
        for key in [k for k, v in self._failures.items() if not v or v[-1] < now - self.window]:
            del self._failures[key]
        for key in [k for k, until in self._locked_until.items() if until <= now]:
            del self._locked_until[key]
