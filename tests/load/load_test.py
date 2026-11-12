#!/usr/bin/env python3
"""Uji beban & konkurensi terhadap server yang sedang berjalan (bukan bagian pytest).

Mensimulasikan beberapa perangkat Android sekaligus: login, upload kecil, upload chunked
paralel (termasuk chunk dari satu upload yang dikirim bersamaan), listing, download,
thumbnail, refresh token, sementara backup berjalan di latar belakang.

Pemakaian:
    python tests/load/load_test.py --base http://127.0.0.1:8000 --admin admin --password ... \
        [--users 6] [--duration 60] [--pid <pid server untuk ukur RAM>]
Keluar dengan kode 1 bila ada error 5xx, "database is locked", atau data tidak cocok.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import random
import statistics
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

try:
    from PIL import Image
except ImportError:  # thumbnail dilewati bila Pillow tidak ada
    Image = None


JPEG_MAGIC = b"\xff\xd8\xff"


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.codes: Counter[str] = Counter()
        self.latency: dict[str, list[float]] = {}
        self.errors: list[str] = []

    def record(self, op: str, resp: httpx.Response, started: float) -> None:
        with self.lock:
            self.codes[f"{op} {resp.status_code}"] += 1
            self.latency.setdefault(op, []).append(time.monotonic() - started)
            if resp.status_code >= 500 or "locked" in resp.text[:500]:
                self.errors.append(f"{op} {resp.status_code}: {resp.text[:200]}")

    def fail(self, message: str) -> None:
        with self.lock:
            self.errors.append(message)


def call(stats: Stats, op: str, client: httpx.Client, method: str, url: str, **kw) -> httpx.Response:
    started = time.monotonic()
    resp = client.request(method, url, **kw)
    stats.record(op, resp, started)
    return resp


def jpeg_bytes() -> bytes:
    if Image is None:
        return b"\xff\xd8\xff\xe0" + os.urandom(2000)
    buf = io.BytesIO()
    Image.new("RGB", (1200, 900), tuple(random.randint(0, 255) for _ in range(3))).save(buf, "JPEG")
    return buf.getvalue()


def device(base: str, username: str, password: str, deadline: float, stats: Stats) -> None:
    client = httpx.Client(base_url=base, timeout=120)
    tokens = call(stats, "login", client, "POST", "/api/v1/auth/login",
                  json={"username": username, "password": password, "device_name": "beban"}).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    mine: dict[str, str] = {}
    while time.monotonic() < deadline:
        action = random.choices(["small", "chunked", "list", "download", "thumb", "refresh"],
                                weights=[3, 2, 3, 3, 2, 1])[0]
        if action == "small":
            data = jpeg_bytes() if random.random() < 0.5 else os.urandom(random.randint(1, 200_000))
            ext = "jpg" if data[:3] == JPEG_MAGIC else "bin"
            name = f"f{random.randint(0, 10**9)}.{ext}"
            resp = call(stats, "upload_small", client, "POST", "/api/v1/files", params={"filename": name},
                        headers=headers, content=data)
            if resp.status_code == 201:
                mine[resp.json()["id"]] = hashlib.sha256(data).hexdigest()
        elif action == "chunked":
            data = os.urandom(random.randint(3, 6) * 262_144 + random.randint(0, 1000))
            up = call(stats, "upload_init", client, "POST", "/api/v1/uploads/init", headers=headers,
                      json={"filename": f"c{random.randint(0, 10**9)}.bin", "total_size": len(data),
                            "chunk_size": 262_144, "sha256": hashlib.sha256(data).hexdigest()})
            if up.status_code != 201:
                continue
            up = up.json()
            indices = list(range(up["total_chunks"]))
            random.shuffle(indices)  # urutan acak + paralel = skenario terberat

            def send(i: int, data: bytes = data, up: dict = up, headers: dict = headers) -> None:
                piece = data[i * 262_144 : (i + 1) * 262_144]
                with httpx.Client(base_url=base, timeout=120) as c2:
                    call(stats, "upload_chunk", c2, "PUT", f"/api/v1/uploads/{up['id']}/chunks/{i}",
                         headers={**headers, "X-Chunk-SHA256": hashlib.sha256(piece).hexdigest()}, content=piece)

            with ThreadPoolExecutor(3) as pool:
                list(pool.map(send, indices + indices[:1]))  # satu chunk sengaja dikirim dua kali
            done = call(stats, "upload_complete", client, "POST", f"/api/v1/uploads/{up['id']}/complete", headers=headers)
            if done.status_code == 200:
                mine[done.json()["file"]["id"]] = hashlib.sha256(data).hexdigest()
            else:
                stats.fail(f"complete {done.status_code}: {done.text[:200]}")
        elif action == "list":
            call(stats, "list", client, "GET", "/api/v1/files", headers=headers,
                 params={"sort": random.choice(["name", "size", "created_at"]), "page_size": 50})
        elif action == "download" and mine:
            fid, sha = random.choice(list(mine.items()))
            resp = call(stats, "download", client, "GET", f"/api/v1/files/{fid}/download", headers=headers)
            if resp.status_code == 200 and hashlib.sha256(resp.content).hexdigest() != sha:
                stats.fail(f"download {fid}: isi tidak cocok")
        elif action == "thumb" and mine:
            fid = random.choice(list(mine))
            call(stats, "thumbnail", client, "GET", f"/api/v1/media/{fid}/thumbnail", headers=headers)
        elif action == "refresh":
            resp = call(stats, "refresh", client, "POST", "/api/v1/auth/refresh",
                        json={"refresh_token": tokens["refresh_token"]})
            if resp.status_code == 200:
                tokens = resp.json()
                headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--admin", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--users", type=int, default=6)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--pid", type=int, default=None)
    args = parser.parse_args()

    stats = Stats()
    admin = httpx.Client(base_url=args.base, timeout=60)
    ah = {"Authorization": "Bearer " + admin.post(
        "/api/v1/auth/login", json={"username": args.admin, "password": args.password}).json()["access_token"]}
    password = "BebanUji12345"
    users = []
    for i in range(args.users):
        name = f"beban{i}{random.randint(0, 99999)}"
        if admin.post("/api/v1/admin/users", headers=ah, json={"username": name, "password": password}).status_code == 201:
            users.append(name)

    peak = [0]
    stop = threading.Event()

    def watch() -> None:
        while args.pid and not stop.is_set():
            try:
                rss = int(open(f"/proc/{args.pid}/status").read().split("VmRSS:")[1].split()[0])
                peak[0] = max(peak[0], rss)
            except OSError:
                pass
            time.sleep(0.2)

    threading.Thread(target=watch, daemon=True).start()
    deadline = time.monotonic() + args.duration

    def backups() -> None:  # backup berulang selama uji
        while time.monotonic() < deadline:
            resp = admin.post("/api/v1/storage/backup", headers=ah)
            stats.codes[f"backup_start {resp.status_code}"] += 1
            time.sleep(5)

    started = time.monotonic()
    with ThreadPoolExecutor(len(users) + 1) as pool:
        futures = [pool.submit(device, args.base, u, password, deadline, stats) for u in users]
        futures.append(pool.submit(backups))
        for f in futures:
            try:
                f.result()
            except Exception as exc:  # error di klien juga dihitung
                stats.fail(f"klien: {exc!r}")
    stop.set()
    elapsed = time.monotonic() - started

    total = sum(v for k, v in stats.codes.items() if not k.startswith("backup_start"))
    print(f"\n{len(users)} perangkat, {elapsed:.0f} detik, {total} request ({total / elapsed:.1f}/s)")
    for op, values in sorted(stats.latency.items()):
        p95 = statistics.quantiles(values, n=20)[-1] if len(values) >= 20 else max(values)
        print(f"  {op:16s} n={len(values):5d}  median {statistics.median(values) * 1000:7.1f} ms  p95 {p95 * 1000:7.1f} ms")
    print("Kode status:", dict(sorted(stats.codes.items())))
    if args.pid:
        print(f"RAM server puncak: {peak[0] / 1024:.0f} MB")
    if stats.errors:
        print(f"\n{len(stats.errors)} MASALAH:")
        for err in stats.errors[:20]:
            print("  -", err)
        return 1
    print("\nTidak ada error 5xx, tidak ada 'database is locked', semua isi download cocok.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
