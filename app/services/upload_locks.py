"""Kunci baca-tulis per upload (asyncio, satu proses).

- Penulisan chunk memegang kunci BERSAMA: banyak chunk dari satu upload tetap bisa
  dikirim paralel.
- complete / cancel memegang kunci EKSKLUSIF: menunggu semua chunk yang sedang ditulis
  selesai, lalu mengubah status. Chunk yang datang sesudahnya melihat status baru dan
  ditolak, sehingga tidak ada byte yang ditulis ke file setelah checksum dihitung.

Prioritas penulis (exclusive) mencegah complete menunggu selamanya bila chunk terus datang.
Server dijalankan dengan 1 worker, jadi kunci di memori proses sudah cukup.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class _RWLock:
    def __init__(self) -> None:
        self.cond = asyncio.Condition()
        self.readers = 0
        self.writer = False
        self.writers_waiting = 0
        self.users = 0

    async def acquire_shared(self) -> None:
        async with self.cond:
            while self.writer or self.writers_waiting:
                await self.cond.wait()
            self.readers += 1

    async def release_shared(self) -> None:
        async with self.cond:
            self.readers -= 1
            if self.readers == 0:
                self.cond.notify_all()

    async def acquire_exclusive(self) -> None:
        async with self.cond:
            self.writers_waiting += 1
            try:
                while self.writer or self.readers:
                    await self.cond.wait()
            finally:
                self.writers_waiting -= 1
            self.writer = True

    async def release_exclusive(self) -> None:
        async with self.cond:
            self.writer = False
            self.cond.notify_all()


class UploadLocks:
    def __init__(self) -> None:
        self._locks: dict[str, _RWLock] = {}

    def _get(self, key: str) -> _RWLock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = _RWLock()
        lock.users += 1
        return lock

    def _put(self, key: str, lock: _RWLock) -> None:
        lock.users -= 1
        if lock.users == 0 and self._locks.get(key) is lock:
            del self._locks[key]

    @asynccontextmanager
    async def shared(self, key: str) -> AsyncIterator[None]:
        lock = self._get(key)
        try:
            await lock.acquire_shared()
            try:
                yield
            finally:
                await lock.release_shared()
        finally:
            self._put(key, lock)

    @asynccontextmanager
    async def exclusive(self, key: str) -> AsyncIterator[None]:
        lock = self._get(key)
        try:
            await lock.acquire_exclusive()
            try:
                yield
            finally:
                await lock.release_exclusive()
        finally:
            self._put(key, lock)

    def __len__(self) -> int:
        return len(self._locks)
