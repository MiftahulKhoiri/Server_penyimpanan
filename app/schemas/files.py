from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.models import StoredFile
from app.utils.filenames import validate_name

FileName = Annotated[str, Field(min_length=1, max_length=255), AfterValidator(validate_name)]
ObjectId = Annotated[str, Field(min_length=1, max_length=36)]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{64}$"), AfterValidator(str.lower)]

SortField = Literal["name", "size", "created_at", "updated_at"]
SortOrder = Literal["asc", "desc"]
MediaFilter = Literal["image", "video", "audio", "document", "other"]


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    folder_id: str | None
    extension: str | None
    mime_type: str
    media_type: str
    size_bytes: int
    sha256: str
    width: int | None
    height: int | None
    duration_seconds: float | None
    has_thumbnail: bool = False
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: StoredFile) -> "FileOut":
        out = cls.model_validate(row)
        out.has_thumbnail = row.thumbnail_path is not None
        return out


class FileList(BaseModel):
    items: list[FileOut]
    total: int
    page: int
    page_size: int


class RenameRequest(BaseModel):
    name: FileName


class MoveFileRequest(BaseModel):
    folder_id: ObjectId | None = Field(description="Folder tujuan; null = root")


class CopyFileRequest(BaseModel):
    folder_id: ObjectId | None = Field(default=None, description="Folder tujuan; tidak dikirim = folder yang sama")
    name: FileName | None = Field(default=None, description="Nama salinan; tidak dikirim = nama otomatis")


class FolderCreate(BaseModel):
    name: FileName
    parent_id: ObjectId | None = None


class FolderMoveRequest(BaseModel):
    parent_id: ObjectId | None = Field(description="Folder induk baru; null = root")


class FolderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    parent_id: str | None
    created_at: datetime
    updated_at: datetime


class BreadcrumbItem(BaseModel):
    id: str
    name: str


class FolderDetail(FolderOut):
    path: list[BreadcrumbItem] = Field(description="Jalur dari root sampai folder ini")
    folders: list[FolderOut]
    file_count: int
    total_size_bytes: int


class UploadInitRequest(BaseModel):
    filename: FileName
    total_size: int = Field(ge=1, description="Ukuran total file (byte)")
    chunk_size: int | None = Field(default=None, ge=1, description="Ukuran chunk; kosong = default server")
    folder_id: ObjectId | None = None
    sha256: Sha256Hex | None = Field(default=None, description="SHA-256 seluruh file (disarankan)")
    mime_type: str | None = Field(default=None, max_length=127, description="Hanya petunjuk; server mendeteksi sendiri")


class UploadOut(BaseModel):
    id: str
    filename: str
    folder_id: str | None
    total_size: int
    chunk_size: int
    total_chunks: int
    status: str
    received_bytes: int
    received_chunks: list[int]
    missing_chunks: int
    expected_sha256: str | None
    file_id: str | None
    error_message: str | None
    created_at: datetime
    expires_at: datetime
    duplicate_file_id: str | None = Field(
        default=None, description="ID file milik Anda yang isinya identik (bila sha256 dikirim)"
    )


class ChunkResult(BaseModel):
    upload_id: str
    chunk_index: int
    size_bytes: int
    sha256: str
    received_bytes: int
    received_chunks_count: int
    total_chunks: int
    all_received: bool


class UploadCompleteOut(BaseModel):
    upload: UploadOut
    file: FileOut
