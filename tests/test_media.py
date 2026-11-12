from __future__ import annotations

import io
import shutil
import subprocess

import pytest
from PIL import Image

from app.models import StoredFile
from app.services import media_service
from tests.conftest import auth_header, create_user_direct, login, storage_root, upload_simple

API = "/api/v1/media"
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe tidak terpasang")


@pytest.fixture(autouse=True)
def _reset_media_cache():
    media_service.reset_failure_cache()
    yield
    media_service.reset_failure_cache()


def _jpeg(width=1600, height=1200, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _png_rgba(width=500, height=300) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (width, height), (0, 128, 255, 100)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory) -> bytes:
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg tidak terpasang")
    path = tmp_path_factory.mktemp("vid") / "contoh.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=640x360:rate=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-y", str(path)],
        check=True, timeout=60,
    )
    return path.read_bytes()


# ---------------- Daftar media ----------------
def test_list_photos_and_videos(user_client, sample_video):
    c, h, _ = user_client
    folder = c.post("/api/v1/folders", headers=h, json={"name": "Album"}).json()
    upload_simple(c, h, "a.jpg", _jpeg(10, 10))
    upload_simple(c, h, "b.png", _png_rgba(10, 10), folder_id=folder["id"])
    upload_simple(c, h, "film.mp4", sample_video)
    upload_simple(c, h, "catatan.txt", b"bukan media")
    deleted = upload_simple(c, h, "hapus.jpg", _jpeg(10, 10)).json()
    c.delete(f"/api/v1/files/{deleted['id']}", headers=h)

    photos = c.get(f"{API}/photos", headers=h, params={"sort": "name", "order": "asc"}).json()
    assert [p["name"] for p in photos["items"]] == ["a.jpg", "b.png"]
    assert photos["items"][0]["width"] == 10
    in_album = c.get(f"{API}/photos", headers=h, params={"folder_id": folder["id"]}).json()
    assert [p["name"] for p in in_album["items"]] == ["b.png"]
    videos = c.get(f"{API}/videos", headers=h).json()
    assert [v["name"] for v in videos["items"]] == ["film.mp4"]

    create_user_direct(c, "siti")
    hs = auth_header(login(c, "siti"))
    assert c.get(f"{API}/photos", headers=hs).json()["total"] == 0


# ---------------- Info media ----------------
@needs_ffmpeg
def test_video_info_probed_once(user_client, sample_video):
    c, h, _ = user_client
    fid = upload_simple(c, h, "film.mp4", sample_video).json()["id"]
    info = c.get(f"{API}/{fid}", headers=h).json()
    assert info["width"] == 640 and info["height"] == 360
    assert 2.5 <= info["duration_seconds"] <= 3.5
    with c.app.state.database.session() as db:
        assert db.get(StoredFile, fid).duration_seconds == info["duration_seconds"]


def test_media_info_rejects_non_media(user_client):
    c, h, _ = user_client
    fid = upload_simple(c, h, "a.txt", b"x").json()["id"]
    resp = c.get(f"{API}/{fid}", headers=h)
    assert resp.status_code == 415 and resp.json()["code"] == "not_media"


# ---------------- Streaming ----------------
def test_stream_video_with_range(user_client, sample_video):
    c, h, _ = user_client
    fid = upload_simple(c, h, "film.mp4", sample_video).json()["id"]
    full = c.get(f"{API}/{fid}/stream", headers=h)
    assert full.status_code == 200 and full.content == sample_video
    assert full.headers["content-type"] == "video/mp4"
    assert full.headers["content-disposition"].startswith("inline")
    assert full.headers["accept-ranges"] == "bytes"
    # Pola yang dipakai pemutar video: minta awal, lalu loncat (seek) ke tengah.
    first = c.get(f"{API}/{fid}/stream", headers={**h, "Range": "bytes=0-1023"})
    assert first.status_code == 206 and first.content == sample_video[:1024]
    mid = len(sample_video) // 2
    seek = c.get(f"{API}/{fid}/stream", headers={**h, "Range": f"bytes={mid}-"})
    assert seek.status_code == 206 and seek.content == sample_video[mid:]
    assert seek.headers["content-range"] == f"bytes {mid}-{len(sample_video) - 1}/{len(sample_video)}"
    multi = c.get(f"{API}/{fid}/stream", headers={**h, "Range": "bytes=0-9,20-29"})
    assert multi.status_code == 206 and "multipart/byteranges" in multi.headers["content-type"]


def test_stream_access_control_and_non_media(user_client):
    c, h, _ = user_client
    photo = upload_simple(c, h, "a.jpg", _jpeg(10, 10)).json()["id"]
    doc = upload_simple(c, h, "a.pdf", b"%PDF-1.4 x").json()["id"]
    assert c.get(f"{API}/{photo}/stream", headers=h).status_code == 200
    assert c.get(f"{API}/{doc}/stream", headers=h).status_code == 415
    assert c.get(f"{API}/{photo}/stream").status_code == 401
    create_user_direct(c, "siti")
    hs = auth_header(login(c, "siti"))
    assert c.get(f"{API}/{photo}/stream", headers=hs).status_code == 404
    assert c.get(f"{API}/{photo}/thumbnail", headers=hs).status_code == 404


# ---------------- Thumbnail ----------------
def test_photo_thumbnail_generated_and_cached(user_client):
    c, h, _ = user_client
    fid = upload_simple(c, h, "besar.jpg", _jpeg(1600, 1200)).json()["id"]
    resp = c.get(f"{API}/{fid}/thumbnail", headers=h)
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(resp.content)) as thumb:
        assert max(thumb.size) == 320 and thumb.size == (320, 240)
    with c.app.state.database.session() as db:
        row = db.get(StoredFile, fid)
    cached = storage_root(c) / row.thumbnail_path
    assert cached.is_file() and row.thumbnail_path.startswith(".thumbs/")
    mtime = cached.stat().st_mtime_ns
    again = c.get(f"{API}/{fid}/thumbnail", headers=h)
    assert again.content == resp.content and cached.stat().st_mtime_ns == mtime
    assert c.get(f"/api/v1/files/{fid}", headers=h).json()["has_thumbnail"] is True
    etag = again.headers["etag"]
    assert c.get(f"{API}/{fid}/thumbnail", headers={**h, "If-None-Match": etag}).status_code == 304


def test_png_with_transparency_thumbnail(user_client):
    c, h, _ = user_client
    fid = upload_simple(c, h, "logo.png", _png_rgba()).json()["id"]
    resp = c.get(f"{API}/{fid}/thumbnail", headers=h)
    assert resp.status_code == 200
    with Image.open(io.BytesIO(resp.content)) as thumb:
        assert thumb.mode == "RGB" and thumb.size == (320, 192)


def test_exif_rotation_applied(user_client):
    c, h, _ = user_client
    img = Image.new("RGB", (400, 200), (10, 200, 10))
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: putar 90°
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    fid = upload_simple(c, h, "hp.jpg", buf.getvalue()).json()["id"]
    with Image.open(io.BytesIO(c.get(f"{API}/{fid}/thumbnail", headers=h).content)) as thumb:
        assert thumb.size[1] > thumb.size[0]  # menjadi potret


@needs_ffmpeg
def test_video_thumbnail(user_client, sample_video):
    c, h, _ = user_client
    fid = upload_simple(c, h, "film.mp4", sample_video).json()["id"]
    resp = c.get(f"{API}/{fid}/thumbnail", headers=h)
    assert resp.status_code == 200, resp.text
    with Image.open(io.BytesIO(resp.content)) as thumb:
        assert thumb.size == (320, 180)


def test_thumbnail_unavailable_cases(user_client):
    c, h, _ = user_client
    doc = upload_simple(c, h, "a.pdf", b"%PDF-1.4 x").json()["id"]
    broken = upload_simple(c, h, "rusak.jpg", b"\xff\xd8\xff\xe0" + b"bukan gambar" * 10).json()["id"]
    svg = upload_simple(c, h, "ikon.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>").json()["id"]
    for fid in (doc, broken, svg):
        resp = c.get(f"{API}/{fid}/thumbnail", headers=h)
        assert resp.status_code == 404 and resp.json()["code"] == "thumbnail_unavailable", fid
    # Kegagalan di-cache: percobaan kedua tidak menjalankan dekoder lagi.
    assert c.get(f"{API}/{broken}/thumbnail", headers=h).status_code == 404
    assert not list((storage_root(c) / ".thumbs").rglob("*.tmp")) if (storage_root(c) / ".thumbs").exists() else True


def test_thumbnail_pixel_limit_and_disabled(make_client):
    c = make_client(THUMBNAIL_MAX_PIXELS=1_000_000)
    create_user_direct(c, "budi")
    h = auth_header(login(c, "budi"))
    big = upload_simple(c, h, "raksasa.jpg", _jpeg(1500, 1000)).json()["id"]
    small = upload_simple(c, h, "kecil.jpg", _jpeg(800, 600)).json()["id"]
    assert c.get(f"{API}/{big}/thumbnail", headers=h).status_code == 404
    assert c.get(f"{API}/{small}/thumbnail", headers=h).status_code == 200
    c.app.state.settings.THUMBNAIL_ENABLED = False
    other = upload_simple(c, h, "lain.jpg", _jpeg(100, 100)).json()["id"]
    assert c.get(f"{API}/{other}/thumbnail", headers=h).status_code == 404


def test_video_thumbnail_without_ffmpeg(make_client, sample_video):
    c = make_client(FFMPEG_PATH="/tidak/ada/ffmpeg", FFPROBE_PATH="/tidak/ada/ffprobe")
    create_user_direct(c, "budi")
    h = auth_header(login(c, "budi"))
    fid = upload_simple(c, h, "film.mp4", sample_video).json()["id"]
    assert c.get(f"{API}/{fid}/thumbnail", headers=h).status_code == 404
    info = c.get(f"{API}/{fid}", headers=h)
    assert info.status_code == 200 and info.json()["duration_seconds"] is None
    assert c.get(f"{API}/{fid}/stream", headers=h).status_code == 200  # streaming tetap jalan
