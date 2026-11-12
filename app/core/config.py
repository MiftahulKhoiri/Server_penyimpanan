"""Konfigurasi aplikasi dari environment variable / file .env.

Semua lokasi penyimpanan dapat dikonfigurasi. Tidak ada path perangkat yang
di-hardcode di luar nilai default yang bisa ditimpa.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_PLACEHOLDER_SECRETS = {
    "change-me",
    "changeme",
    "ganti-dengan-secret-acak-minimal-32-karakter",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- Aplikasi ---
    APP_NAME: str = "Server_penyimpanan"
    ENVIRONMENT: Literal["production", "development", "test"] = "production"
    HOST: str = "0.0.0.0"
    PORT: int = Field(8000, ge=1, le=65535)
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    ENABLE_DOCS: bool = True
    # IP proxy yang dipercaya untuk header X-Forwarded-For (dipakai uvicorn).
    FORWARDED_ALLOW_IPS: str = "127.0.0.1"

    # --- Keamanan / JWT ---
    SECRET_KEY: str = Field(min_length=32)
    JWT_ALGORITHM: Literal["HS256", "HS384", "HS512"] = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(15, ge=1, le=1440)
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(30, ge=1, le=365)

    # --- Akun ---
    ALLOW_REGISTRATION: bool = False
    PASSWORD_MIN_LENGTH: int = Field(8, ge=8, le=128)

    # --- Argon2 (disetel hemat RAM untuk Raspberry Pi 3, 1 GB) ---
    ARGON2_TIME_COST: int = Field(2, ge=1, le=10)
    ARGON2_MEMORY_COST_KIB: int = Field(19456, ge=8192, le=262144)
    ARGON2_PARALLELISM: int = Field(1, ge=1, le=4)

    # --- Pembatasan percobaan login ---
    LOGIN_MAX_ATTEMPTS: int = Field(5, ge=1, le=100)
    LOGIN_WINDOW_SECONDS: int = Field(300, ge=10)
    LOGIN_LOCKOUT_SECONDS: int = Field(900, ge=10)

    # --- CORS ---
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Penyimpanan ---
    PRIMARY_STORAGE_PATH: Path = Path("/mnt/ssd/storage")
    BACKUP_STORAGE_PATH: Path = Path("/mnt/hdd/backup")
    DATABASE_PATH: Path = Path("/mnt/ssd/database/server.db")
    PRIMARY_MOUNT_POINT: Path = Path("/mnt/ssd")
    BACKUP_MOUNT_POINT: Path = Path("/mnt/hdd")
    # Jumlah thread pekerja untuk operasi blocking (disk/SQLite). Default anyio 40 terlalu
    # banyak untuk Pi 3 (4 core, 1 GB): tiap thread menambah fragmentasi memori.
    THREADPOOL_SIZE: int = Field(16, ge=2, le=100)

    # Jika True, server menolak menulis bila SSD/HDD tidak benar-benar ter-mount.
    # Hanya boleh False untuk pengembangan/testing.
    REQUIRE_MOUNTED_STORAGE: bool = True

    # --- Database ---
    AUTO_MIGRATE: bool = True

    # --- Batas body request untuk endpoint non-upload (JSON). Upload punya batasnya sendiri.
    MAX_REQUEST_BODY_BYTES: int = Field(1024 * 1024, ge=1024, le=64 * 1024 * 1024)

    # --- Upload ---
    MAX_UPLOAD_SIZE_BYTES: int = Field(64 * 1024**3, ge=1)  # batas per file (chunked)
    MAX_SIMPLE_UPLOAD_BYTES: int = Field(100 * 1024**2, ge=1)  # batas upload sekali kirim
    UPLOAD_CHUNK_SIZE_DEFAULT: int = Field(8 * 1024**2, ge=1024)
    UPLOAD_CHUNK_SIZE_MIN: int = Field(256 * 1024, ge=1024)
    UPLOAD_CHUNK_SIZE_MAX: int = Field(64 * 1024**2, ge=1024)
    UPLOAD_EXPIRE_HOURS: int = Field(24, ge=1, le=24 * 30)
    MAX_ACTIVE_UPLOADS_PER_USER: int = Field(10, ge=1, le=1000)
    # 0 = pembersihan otomatis di latar belakang dimatikan.
    UPLOAD_CLEANUP_INTERVAL_SECONDS: int = Field(1800, ge=0)
    # Ruang kosong minimum yang selalu disisakan di SSD.
    MIN_FREE_SPACE_BYTES: int = Field(1024**3, ge=0)
    # Daftar ekstensi tanpa titik, pisahkan koma. ALLOWED kosong = semua diizinkan.
    BLOCKED_EXTENSIONS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    ALLOWED_EXTENSIONS: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Trash ---
    TRASH_RETENTION_DAYS: int = Field(30, ge=1, le=3650)

    # --- Backup ke HDD ---
    # 0 = backup otomatis mati (default). Backup tidak pernah menghapus file asli.
    BACKUP_AUTO_INTERVAL_HOURS: int = Field(0, ge=0, le=24 * 30)
    # Jumlah salinan database yang disimpan di HDD. 0 = simpan semua (tidak ada yang dihapus).
    BACKUP_DB_KEEP: int = Field(0, ge=0, le=1000)
    BACKUP_MIN_FREE_SPACE_BYTES: int = Field(1024**3, ge=0)

    # --- Media / thumbnail ---
    THUMBNAIL_ENABLED: bool = True
    THUMBNAIL_SIZE: int = Field(320, ge=64, le=1024)
    THUMBNAIL_QUALITY: int = Field(80, ge=30, le=95)
    # Gambar di atas batas piksel ini tidak dibuatkan thumbnail (lindungi RAM 1 GB).
    THUMBNAIL_MAX_PIXELS: int = Field(80_000_000, ge=1_000)
    FFMPEG_PATH: str = "ffmpeg"
    FFPROBE_PATH: str = "ffprobe"
    MEDIA_TOOL_TIMEOUT_SECONDS: int = Field(30, ge=5, le=600)

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("BLOCKED_EXTENSIONS", "ALLOWED_EXTENSIONS", mode="before")
    @classmethod
    def _split_extensions(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.split(",")
        if isinstance(value, list):
            return [str(v).strip().lower().lstrip(".") for v in value if str(v).strip()]
        return value

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _no_wildcard(cls, value: list[str]) -> list[str]:
        for origin in value:
            if origin == "*":
                raise ValueError("CORS_ORIGINS tidak boleh '*'; tulis daftar origin yang diizinkan")
            if not origin.startswith(("http://", "https://")):
                raise ValueError(f"Origin CORS tidak valid: {origin!r}")
        return [o.rstrip("/") for o in value]

    @field_validator("SECRET_KEY")
    @classmethod
    def _secret_not_placeholder(cls, value: str) -> str:
        if value.strip().lower() in _PLACEHOLDER_SECRETS:
            raise ValueError("SECRET_KEY masih nilai contoh; buat secret acak baru")
        return value

    @model_validator(mode="after")
    def _validate_upload(self) -> "Settings":
        if not (self.UPLOAD_CHUNK_SIZE_MIN <= self.UPLOAD_CHUNK_SIZE_DEFAULT <= self.UPLOAD_CHUNK_SIZE_MAX):
            raise ValueError("Harus UPLOAD_CHUNK_SIZE_MIN <= UPLOAD_CHUNK_SIZE_DEFAULT <= UPLOAD_CHUNK_SIZE_MAX")
        return self

    @model_validator(mode="after")
    def _validate_paths(self) -> "Settings":
        paths = {
            "PRIMARY_STORAGE_PATH": self.PRIMARY_STORAGE_PATH,
            "BACKUP_STORAGE_PATH": self.BACKUP_STORAGE_PATH,
            "DATABASE_PATH": self.DATABASE_PATH,
            "PRIMARY_MOUNT_POINT": self.PRIMARY_MOUNT_POINT,
            "BACKUP_MOUNT_POINT": self.BACKUP_MOUNT_POINT,
        }
        for name, path in paths.items():
            if not path.is_absolute():
                raise ValueError(f"{name} harus path absolut")

        primary = self.PRIMARY_STORAGE_PATH.resolve()
        backup = self.BACKUP_STORAGE_PATH.resolve()
        if primary.is_relative_to(PROJECT_ROOT):
            raise ValueError("PRIMARY_STORAGE_PATH tidak boleh berada di dalam direktori kode aplikasi")
        if primary == backup or primary.is_relative_to(backup) or backup.is_relative_to(primary):
            raise ValueError("PRIMARY_STORAGE_PATH dan BACKUP_STORAGE_PATH harus terpisah")

        if self.REQUIRE_MOUNTED_STORAGE:
            for name in ("PRIMARY_MOUNT_POINT", "BACKUP_MOUNT_POINT"):
                if paths[name] == Path("/"):
                    raise ValueError(f"{name} tidak boleh '/' (root filesystem)")
            if not self.PRIMARY_STORAGE_PATH.is_relative_to(self.PRIMARY_MOUNT_POINT):
                raise ValueError("PRIMARY_STORAGE_PATH harus berada di bawah PRIMARY_MOUNT_POINT")
            if not self.DATABASE_PATH.is_relative_to(self.PRIMARY_MOUNT_POINT):
                raise ValueError("DATABASE_PATH harus berada di bawah PRIMARY_MOUNT_POINT")
            if not self.BACKUP_STORAGE_PATH.is_relative_to(self.BACKUP_MOUNT_POINT):
                raise ValueError("BACKUP_STORAGE_PATH harus berada di bawah BACKUP_MOUNT_POINT")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
