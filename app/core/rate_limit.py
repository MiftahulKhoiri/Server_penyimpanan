"""Pembatasan percobaan login (in-memory, RAM terbatas secara pasti).

Disimpan di memori proses: cocok untuk deployment 1 worker uvicorn di
Raspberry Pi. Penghitung hilang ketika server restart (risiko diterima).

Batas memori:
- Penghitung IP dan username disimpan di dua tabel terpisah, masing-masing
  dibatasi `max_keys` entri. Jumlah entri TIDAK PERNAH melebihi batas itu,
  bahkan ketika semua entri masih aktif (penyerang mencoba ribuan username unik).
- Tiap entri berukuran tetap (4 angka): penghitung "sliding window" dua-ember
  (jendela sekarang + jendela sebelumnya berbobot), bukan daftar timestamp.

Eviction yang aman (agar membanjiri tabel tidak bisa dipakai untuk "mereset"
penghitung akun yang sedang diserang):
1. Entri kedaluwarsa dibuang lebih dulu.
2. Bila tabel tetap penuh, dari sekumpulan kecil entri yang paling lama tidak
   tersentuh (LRU) dipilih yang percobaannya PALING SEDIKIT dan tidak sedang
   terkunci. Entri banjir (1 percobaan per username) selalu tergusur sebelum
   akun target yang sudah mengumpulkan beberapa percobaan.
3. Entri yang sedang terkunci hanya digusur bila seluruh sampel terkunci; yang
   kuncinya paling cepat habis yang dipilih.
Pembatasan per IP tetap berlaku untuk penyerang yang sama, sehingga membanjiri
tabel username dari satu IP justru mengunci IP itu (batas IP = 4x batas user).

IPv6 dikelompokkan per prefix /64: satu pelanggan biasanya mendapat /64 utuh,
jadi berganti alamat di dalam /64 tidak menghindari pembatasan IP.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from itertools import islice

_EVICTION_SAMPLE = 32
_EXPIRED_SCAN = 64


class _Entry:
    """Penghitung sliding-window dua-ember: perkiraan = cur + prev * sisa_bobot_jendela_lalu."""

    __slots__ = ("cur", "cur_start", "locked_until", "prev")

    def __init__(self, now: float) -> None:
        self.prev = 0
        self.cur = 0
        self.cur_start = now
        self.locked_until = 0.0

    def roll(self, now: float, window: float) -> None:
        elapsed = now - self.cur_start
        if elapsed < window:
            return
        self.prev = self.cur if elapsed < 2 * window else 0
        self.cur = 0
        self.cur_start = now - (elapsed % window)

    def estimate(self, now: float, window: float) -> float:
        weight = max(0.0, 1.0 - (now - self.cur_start) / window)
        return self.cur + self.prev * weight

    def idle(self, now: float, window: float) -> bool:
        return self.locked_until <= now and (now - self.cur_start >= 2 * window or self.prev + self.cur == 0)


class _BoundedTable:
    """OrderedDict LRU dengan kapasitas pasti. Tidak thread-safe; dipakai di bawah kunci limiter."""

    def __init__(self, capacity: int, limit: int, window: float) -> None:
        self.capacity = capacity
        self.limit = limit
        self.window = window
        self.data: OrderedDict[str, _Entry] = OrderedDict()
        self.evicted_active = 0  # statistik: berapa entri aktif yang terpaksa digusur

    def _expired(self, entry: _Entry, now: float) -> bool:
        return entry.idle(now, self.window)

    def get(self, key: str) -> _Entry | None:
        return self.data.get(key)

    def _make_room(self, now: float) -> None:
        # 1) buang entri kedaluwarsa dari ujung LRU (O(_EXPIRED_SCAN), bukan O(n))
        for key in list(islice(self.data, _EXPIRED_SCAN)):
            if self._expired(self.data[key], now):
                del self.data[key]
        if len(self.data) < self.capacity:
            return
        # 2) gusur satu entri dari sampel LRU: yang tidak terkunci dengan percobaan paling
        #    sedikit; bila semua terkunci, yang kuncinya paling cepat habis.
        best_key: str | None = None
        best_rank: tuple[int, float] | None = None
        for key, entry in islice(self.data.items(), _EVICTION_SAMPLE):
            rank = (1, entry.locked_until) if entry.locked_until > now else (0, entry.estimate(now, self.window))
            if best_rank is None or rank < best_rank:
                best_key, best_rank = key, rank
        if best_key is not None:
            del self.data[best_key]
            self.evicted_active += 1

    def touch(self, key: str, now: float) -> _Entry:
        entry = self.data.get(key)
        if entry is None:
            if len(self.data) >= self.capacity:
                self._make_room(now)
            entry = self.data[key] = _Entry(now)
        else:
            self.data.move_to_end(key)
        return entry

    def pop(self, key: str) -> None:
        self.data.pop(key, None)

    def __len__(self) -> int:
        return len(self.data)


def normalize_ip(ip: str | None) -> str:
    if not ip:
        return "unknown"
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip[:45]
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{addr}/64", strict=False))
    return str(addr)


class LoginRateLimiter:
    _MAX_KEYS = 10_000  # per tabel (IP dan username), batas pasti

    def __init__(
        self,
        max_attempts: int,
        window_seconds: int,
        lockout_seconds: int,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int | None = None,
    ) -> None:
        self.max_attempts = max_attempts
        self.window = window_seconds
        self.lockout = lockout_seconds
        self._clock = clock
        capacity = max_keys or self._MAX_KEYS
        self._tables = {
            "ip": _BoundedTable(capacity, max_attempts * 4, window_seconds),
            "user": _BoundedTable(capacity, max_attempts, window_seconds),
        }
        self._lock = threading.Lock()

    @staticmethod
    def keys(ip: str | None, username: str) -> tuple[str, str]:
        return f"ip:{normalize_ip(ip)}", f"user:{username.lower()[:64]}"

    def _table(self, key: str) -> _BoundedTable:
        return self._tables["ip" if key.startswith("ip:") else "user"]

    def _limit_for(self, key: str) -> int:
        # IP mendapat batas lebih longgar (bisa beberapa perangkat di balik NAT rumah).
        return self._table(key).limit

    def retry_after(self, keys: Iterable[str]) -> int | None:
        """Kembalikan detik tunggu jika salah satu key sedang terkunci."""
        now = self._clock()
        wait = 0.0
        with self._lock:
            for key in keys:
                entry = self._table(key).get(key)
                if entry is not None and entry.locked_until > now:
                    wait = max(wait, entry.locked_until - now)
        return int(wait) + 1 if wait > 0 else None

    def register_failure(self, keys: Iterable[str]) -> None:
        now = self._clock()
        with self._lock:
            for key in keys:
                table = self._table(key)
                entry = table.touch(key, now)
                entry.roll(now, self.window)
                entry.cur += 1
                if entry.estimate(now, self.window) >= table.limit:
                    entry.locked_until = now + self.lockout
                    entry.prev = entry.cur = 0
                    entry.cur_start = now

    def reset(self, keys: Iterable[str]) -> None:
        with self._lock:
            for key in keys:
                self._table(key).pop(key)

    # ---- observabilitas & pengujian ----
    def key_count(self) -> int:
        with self._lock:
            return sum(len(t) for t in self._tables.values())

    def capacity(self) -> int:
        return sum(t.capacity for t in self._tables.values())

    def evicted_active(self) -> int:
        with self._lock:
            return sum(t.evicted_active for t in self._tables.values())
