"""Recycle bin: daftar, restore, hapus permanen, dan purge otomatis."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from app.core.errors import not_found
from app.database.types import utcnow
from app.models import Folder, StoredFile, TrashItem, User
from app.services.activity_service import log_activity
from app.services.file_service import unique_name
from app.services.storage_paths import StoragePaths, UnsafePathError
from app.utils.request_info import ClientInfo

logger = logging.getLogger("server_penyimpanan.trash")
_BATCH = 500


def list_trash(db: Session, owner_id: int, page: int, page_size: int) -> tuple[list[TrashItem], int]:
    total = db.scalar(select(func.count(TrashItem.id)).where(TrashItem.owner_id == owner_id)) or 0
    rows = db.scalars(
        select(TrashItem)
        .where(TrashItem.owner_id == owner_id)
        .order_by(TrashItem.deleted_at.desc(), TrashItem.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), int(total)


def get_item(db: Session, owner_id: int, target_id: str) -> TrashItem:
    """Cari item trash berdasarkan ID file atau ID folder."""
    item = None
    if len(target_id) <= 36:
        item = db.scalar(
            select(TrashItem).where(
                TrashItem.owner_id == owner_id,
                or_(TrashItem.file_id == target_id, TrashItem.folder_id == target_id),
            )
        )
    if item is None:
        raise not_found("trash_item_not_found", "Item tidak ada di trash")
    return item


def _subtree_ids(db: Session, root_id: str, *, deleted_at=None) -> list[str]:  # type: ignore[no-untyped-def]
    """ID folder + turunannya. Bila deleted_at diisi, hanya yang dihapus bersamaan (satu kesatuan)."""
    base = select(Folder.id.label("id")).where(Folder.id == root_id)
    tree = base.cte("tree", recursive=True)
    child = aliased(Folder)
    step = select(child.id).where(child.parent_id == tree.c.id)
    if deleted_at is not None:
        step = step.where(child.is_deleted.is_(True), child.deleted_at == deleted_at)
    tree = tree.union_all(step)
    return list(db.scalars(select(tree.c.id)).all())


def _alive_folder(db: Session, owner_id: int, folder_id: str | None) -> str | None:
    if folder_id is None:
        return None
    folder = db.get(Folder, folder_id)
    if folder is None or folder.owner_id != owner_id or folder.is_deleted:
        return None
    return folder.id


def restore(db: Session, user: User, item: TrashItem, client: ClientInfo) -> dict:
    """Kembalikan item ke folder asal (atau root bila folder asal sudah tidak ada).
    Nama yang bentrok otomatis diberi nomor."""
    target = _alive_folder(db, user.id, item.original_folder_id)
    if item.file_id:
        row = db.get(StoredFile, item.file_id)
        assert row is not None
        row.name = unique_name(db, user.id, target, row.name)
        row.folder_id = target
        row.is_deleted = False
        row.deleted_at = None
        result = {"type": "file", "id": row.id, "name": row.name, "folder_id": target}
    else:
        folder = db.get(Folder, item.folder_id)
        assert folder is not None
        batch_time = folder.deleted_at
        ids = _subtree_ids(db, folder.id, deleted_at=batch_time)
        # File yang sebelumnya dihapus sendiri-sendiri punya item trash sendiri: tetap di trash.
        individually_trashed = select(TrashItem.file_id).where(TrashItem.file_id.is_not(None))
        for start in range(0, len(ids), _BATCH):
            chunk = ids[start : start + _BATCH]
            db.execute(
                update(StoredFile)
                .where(StoredFile.folder_id.in_(chunk), StoredFile.is_deleted.is_(True),
                       StoredFile.deleted_at == batch_time, StoredFile.id.not_in(individually_trashed))
                .values(is_deleted=False, deleted_at=None)
                .execution_options(synchronize_session=False)
            )
            db.execute(
                update(Folder).where(Folder.id.in_(chunk)).values(is_deleted=False, deleted_at=None)
                .execution_options(synchronize_session=False)
            )
        db.expire(folder)
        folder.name = unique_name(db, user.id, target, folder.name)
        folder.parent_id = target
        result = {"type": "folder", "id": folder.id, "name": folder.name, "folder_id": target}
    db.delete(item)
    log_activity(
        db, "trash.restore", user_id=user.id, ip_address=client.ip_address, user_agent=client.user_agent,
        target_type=result["type"], target_id=result["id"], details={"name": result["name"], "to_folder": target},
    )
    db.commit()
    return result


def _unlink_all(storage: StoragePaths, relpaths: list[str]) -> int:
    removed = 0
    for rel in relpaths:
        try:
            path: Path = storage.resolve(rel)
            path.unlink(missing_ok=True)
            removed += 1
        except UnsafePathError:
            logger.error("Path tidak aman saat purge: %s", rel)
        except OSError as exc:
            logger.warning("Gagal menghapus data fisik (%s): %s", rel, exc.__class__.__name__)
    return removed


def purge_item(
    db: Session, storage: StoragePaths, item: TrashItem, *, actor_id: int | None, client: ClientInfo | None,
    reason: str,
) -> dict:
    """Hapus permanen: baris database + file fisik + thumbnail. Tidak bisa dibatalkan."""
    relpaths: list[str] = []
    if item.file_id:
        row = db.get(StoredFile, item.file_id)
        files = [row] if row else []
        target_type, target_id, name = "file", item.file_id, item.original_name
    else:
        folder_ids = _subtree_ids(db, item.folder_id)  # semua isi, apa pun statusnya
        files = []
        for start in range(0, len(folder_ids), _BATCH):
            files += db.scalars(select(StoredFile).where(StoredFile.folder_id.in_(folder_ids[start : start + _BATCH]))).all()
        target_type, target_id, name = "folder", item.folder_id, item.original_name
    for row in files:
        relpaths.append(row.storage_path)
        if row.thumbnail_path:
            relpaths.append(row.thumbnail_path)
    total_bytes = sum(r.size_bytes for r in files)
    file_ids = [r.id for r in files]

    for start in range(0, len(file_ids), _BATCH):
        db.execute(delete(StoredFile).where(StoredFile.id.in_(file_ids[start : start + _BATCH])))
    if target_type == "folder":
        db.execute(delete(Folder).where(Folder.id == target_id))  # subfolder ikut lewat ON DELETE CASCADE
    db.execute(delete(TrashItem).where(TrashItem.id == item.id))
    log_activity(
        db, "trash.purge", user_id=actor_id,
        ip_address=client.ip_address if client else None, user_agent=client.user_agent if client else None,
        target_type=target_type, target_id=target_id,
        details={"name": name, "files": len(file_ids), "bytes": total_bytes, "reason": reason},
    )
    db.commit()
    db.expire_all()
    # Data fisik dihapus SETELAH database commit: bila gagal, yang tersisa hanya file yatim
    # (aman), bukan catatan database yang menunjuk ke file yang sudah hilang.
    _unlink_all(storage, relpaths)
    return {"type": target_type, "id": target_id, "files": len(file_ids), "bytes": total_bytes}


def empty_trash(db: Session, storage: StoragePaths, user: User, client: ClientInfo) -> int:
    count = 0
    while True:
        item = db.scalar(select(TrashItem).where(TrashItem.owner_id == user.id).order_by(TrashItem.id).limit(1))
        if item is None:
            return count
        purge_item(db, storage, item, actor_id=user.id, client=client, reason="empty_trash")
        count += 1


def purge_expired(db: Session, storage: StoragePaths, limit: int = 500) -> int:
    """Hapus permanen item yang melewati masa simpan (TRASH_RETENTION_DAYS)."""
    items = db.scalars(
        select(TrashItem).where(TrashItem.purge_after <= utcnow()).order_by(TrashItem.purge_after).limit(limit)
    ).all()
    for item in items:
        purge_item(db, storage, item, actor_id=None, client=None, reason="retention_expired")
    return len(items)


def item_type(item: TrashItem) -> str:
    return "file" if item.file_id else "folder"

