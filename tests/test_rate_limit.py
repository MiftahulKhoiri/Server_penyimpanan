"""Rate limiter login: perlindungan brute-force tetap jalan, jumlah key & RAM terbatas pasti."""

from __future__ import annotations

import threading
import tracemalloc

from app.core.rate_limit import LoginRateLimiter, normalize_ip


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _limiter(clock=None, **kw) -> LoginRateLimiter:
    return LoginRateLimiter(5, 300, 900, clock=clock or FakeClock(), **kw)


def test_same_username_locked():
    lim = _limiter()
    for i in range(5):
        keys = lim.keys(f"10.0.0.{i}", "Budi")  # IP berbeda, username sama (beda huruf besar/kecil)
        assert lim.retry_after(keys) is None
        lim.register_failure(keys)
    assert lim.retry_after(lim.keys("10.9.9.9", "budi")) is not None


def test_same_ip_locked_after_4x():
    lim = _limiter()
    for i in range(19):
        lim.register_failure(lim.keys("192.168.1.5", f"user{i}"))
    assert lim.retry_after(lim.keys("192.168.1.5", "lain")) is None
    lim.register_failure(lim.keys("192.168.1.5", "user19"))
    assert lim.retry_after(lim.keys("192.168.1.5", "lain")) is not None
    assert lim.retry_after(lim.keys("192.168.1.6", "lain")) is None


def test_thousands_of_unique_usernames_bounded():
    lim = _limiter(max_keys=2_000)
    for i in range(50_000):
        lim.register_failure(lim.keys(f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}", f"u{i}"))
        assert lim.key_count() <= lim.capacity()
    assert lim.key_count() == lim.capacity() == 4_000


def test_flood_cannot_evict_targeted_account():
    """Membanjiri tabel username tidak boleh mereset penghitung akun yang sedang diserang."""
    lim = _limiter(max_keys=1_000)
    for _ in range(4):
        lim.register_failure(lim.keys("203.0.113.1", "admin"))
    for i in range(20_000):  # banjir dari banyak IP, 1 percobaan per username
        lim.register_failure(lim.keys(f"10.1.{i // 256 % 256}.{i % 256}", f"flood{i}"))
    assert lim.retry_after(lim.keys("198.51.100.7", "admin")) is None
    lim.register_failure(lim.keys("198.51.100.7", "admin"))  # percobaan ke-5 dari IP lain
    assert lim.retry_after(lim.keys("198.51.100.8", "admin")) is not None


def test_locked_entries_bounded_when_all_locked():
    lim = _limiter(max_keys=10)
    for i in range(30):
        for _ in range(5):
            lim.register_failure((f"user:u{i}",))
    assert lim.key_count() <= lim.capacity()
    assert lim.retry_after(("user:u29",)) is not None  # entri terbaru tetap terkunci


def test_expired_keys_and_lock_expiry():
    clock = FakeClock()
    lim = _limiter(clock)
    keys = lim.keys("10.0.0.1", "budi")
    for _ in range(4):
        lim.register_failure(keys)
    clock.now += 301  # percobaan lama keluar dari jendela
    lim.register_failure(keys)
    assert lim.retry_after(keys) is None
    for _ in range(4):
        lim.register_failure(keys)
    assert lim.retry_after(keys) is not None
    clock.now += 901
    assert lim.retry_after(keys) is None


def test_expired_keys_reclaimed_before_active_ones():
    clock = FakeClock()
    lim = _limiter(clock, max_keys=100)
    for i in range(100):
        lim.register_failure((f"user:old{i}",))
    clock.now += 1000
    lim.register_failure(("user:new",))
    assert lim.evicted_active() == 0  # yang dibuang hanya entri kedaluwarsa


def test_reset_clears_username():
    lim = _limiter()
    keys = lim.keys("10.0.0.1", "budi")
    for _ in range(4):
        lim.register_failure(keys)
    lim.reset(keys[1:])
    lim.register_failure(keys)
    assert lim.retry_after(keys[1:]) is None


def test_ipv6_grouped_by_prefix():
    assert normalize_ip("2001:db8:1:2:aaaa::1") == normalize_ip("2001:db8:1:2:bbbb::9") == "2001:db8:1:2::/64"
    assert normalize_ip("::ffff:192.0.2.1") == "192.0.2.1"
    assert normalize_ip(None) == "unknown"
    lim = _limiter()
    for i in range(20):
        lim.register_failure(lim.keys(f"2001:db8:1:2::{i + 1:x}", f"u{i}"))
    assert lim.retry_after(lim.keys("2001:db8:1:2::ffff", "x")) is not None


def test_concurrent_requests_safe_and_bounded():
    lim = LoginRateLimiter(5, 300, 900, max_keys=500)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(3_000):
                lim.register_failure(lim.keys(f"10.{n}.{i // 256 % 256}.{i % 256}", f"t{n}-{i}"))
                lim.register_failure(lim.keys(f"172.16.{n}.1", "korban"))
                lim.retry_after(lim.keys("1.1.1.1", "korban"))
        except BaseException as exc:  # noqa: BLE001  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert lim.key_count() <= lim.capacity()
    assert lim.retry_after(lim.keys("9.9.9.9", "korban")) is not None


def test_memory_stays_bounded():
    """RAM tidak tumbuh mengikuti jumlah username unik setelah tabel penuh."""
    lim = _limiter(max_keys=5_000)

    def flood(start: int, count: int) -> None:
        for i in range(start, start + count):
            lim.register_failure(lim.keys(f"10.{i // 65536 % 256}.{i // 256 % 256}.{i % 256}", f"name{i}"))

    tracemalloc.start()
    flood(0, 20_000)  # tabel penuh
    base, _ = tracemalloc.get_traced_memory()
    flood(20_000, 100_000)
    current, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert lim.key_count() <= 10_000
    # 100 ribu username baru setelah penuh: pertumbuhan bersih harus ~0 (toleransi 512 KiB).
    assert current - base < 512 * 1024, current - base
