"""Logika folder: buat, daftar, detail, rename, pindah (anti-siklus), hapus ke trash."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, literal, select, update
from sqlalchemy.orm import Session, aliased

from app.core.config import Settings
from app.core.errors import conflict, unprocessable
from app.database.types import utcnow
from app.models import Folder, StoredFile, TrashItem, User
from app.services.activity_service import log_activity
from app.services.file_service import get_owned_folder, name_taken, resolve_folder_id
from app.utils.request_info import ClientInfo

MAX_FOLDER_DEPTH = 32


def ancestors(db: Session, folder: Folder) -> list[Folder]:
    """Daftar folder dari root sampai `folder` (inklusif)."""
    chain = [folder]
    current = folder
    for _ in range(MAX_FOLDER_DEPTH + 1):
        if current.parent_id is None:
            break
        parent = db.get(Folder, current.parent_id)
        if parent is None:
            break
        chain.append(parent)
        current = parent
    return list(reversed(chain))


def _depth(db: Session, folder_id: str | None) -> int:
    """Kedalaman folder (root=0, folder di root=1)."""
    if folder_id is None:
        return 0
    folder = db.get(Folder, folder_id)
    return len(ancestors(db, folder)) if folder else 0


def _subtree(folder_id: str):  # type: ignore[no-untyped-def]
    """CTE rekursif: folder + seluruh turunannya yang belum dihapus, beserta level relatifnya."""
    base = select(Folder.id.label("id"), literal(0).label("level")).where(Folder.id == folder_id)
    tree = base.cte("subtree", recursive=True)
    child = aliased(Folder)
    tree = tree.union_all(
        select(child.id, tree.c.level + 1).where(child.parent_id == tree.c.id, child.is_deleted.is_(False))
    )
    return tree


def create_folder(
    db: Session, user: User, name: str, parent_id: str | None, client: ClientInfo
) -> Folder:
    parent = resolve_folder_id(db, user.id, parent_id)
    if _depth(db, parent) + 1 > MAX_FOLDER_DEPTH:
        raise unprocessable("folder_too_deep", f"Kedalaman folder maksimal {MAX_FOLDER_DEPTH}")
    if name_taken(db, user.id, parent, name):
        raise conflict("name_conflict", "Sudah ada file/folder dengan nama itu di lokasi ini")
    folder = Folder(owner_id=user.id, parent_id=parent, name=name)
    db.add(folder)
    db.flush()
    log_activity(
        db, "folder.create", user_id=user.id, ip_address=client.ip_address, user_agent=client.user_agent,
        target_type="folder", target_id=folder.id, details={"name": name, "parent_id": parent},
    )
    db.commit()
    return folder


def list_folders(db: Session, owner_id: int, parent_id: str | None) -> list[Folder]:
    parent = resolve_folder_id(db, owner_id, parent_id)
    rows = db.scalars(
        select(Folder)
        .where(
            Folder.owner_id == owner_id,
            Folder.is_deleted.is_(False),
            Folder.parent_id.is_(None) if parent is None else Folder.parent_id == parent,
        )
        .order_by(func.lower(Folder.name), Folder.id)
    ).all()
    return list(rows)


def folder_stats(db: Session, owner_id: int, folder_id: str) -> tuple[int, int]:
    count, total = db.execute(
        select(func.count(StoredFile.id), func.coalesce(func.sum(StoredFile.size_bytes), 0)).where(
            StoredFile.owner_id == owner_id,
            StoredFile.folder_id == folder_id,
            StoredFile.is_deleted.is_(False),
        )
    ).one()
    return int(count), int(total)


def rename_folder(db: Session, user: User, folder: Folder, new_name: str, client: ClientInfo) -> Folder:
    if new_name == folder.name:
        return folder
    if name_taken(db, user.id, folder.parent_id, new_name, exclude_folder_id=folder.id):
        raise conflict("name_conflict", "Sudah ada file/folder dengan nama itu di lokasi ini")
    old = folder.name
    folder.name = new_name
    log_activity(
        db, "folder.rename", user_id=user.id, ip_address=client.ip_address, user_agent=client.user_agent,
        target_type="folder", target_id=folder.id, details={"old_name": old, "new_name": new_name},
    )
    db.commit()
    return folder


def move_folder(db: Session, user: User, folder: Folder, new_parent_id: str | None, client: ClientInfo) -> Folder:
    target = resolve_folder_id(db, user.id, new_parent_id)
    if target == folder.parent_id:
        return folder
    if target is not None:
        if target == folder.id or any(f.id == folder.id for f in ancestors(db, get_owned_folder(db, user.id, target))):
            raise conflict("folder_cycle", "Folder tidak bisa dipindah ke dalam dirinya sendiri atau turunannya")
    tree = _subtree(folder.id)
    subtree_height = int(db.scalar(select(func.max(tree.c.level))) or 0)
    if _depth(db, target) + 1 + subtree_height > MAX_FOLDER_DEPTH:
        raise unprocessable("folder_too_deep", f"Kedalaman folder maksimal {MAX_FOLDER_DEPTH}")
    if name_taken(db, user.id, target, folder.name, exclude_folder_id=folder.id):
        raise conflict("name_conflict", "Lokasi tujuan sudah berisi file/folder dengan nama yang sama")
    old = folder.parent_id
    folder.parent_id = target
    log_activity(
        db, "folder.move", user_id=user.id, ip_address=client.ip_address, user_agent=client.user_agent,
        target_type="folder", target_id=folder.id, details={"from_parent": old, "to_parent": target},
    )
    db.commit()
    return folder


def soft_delete_folder(
    db: Session, settings: Settings, user: User, folder: Folder, client: ClientInfo
) -> TrashItem:
    """Pindahkan folder beserta isinya ke trash. Tidak ada data fisik yang dihapus."""
    now = utcnow()
    tree = _subtree(folder.id)
    folder_ids = list(db.scalars(select(tree.c.id)).all())
    total_size = 0
    # Semua item ditandai dengan deleted_at yang sama sehingga bisa dipulihkan sebagai satu kesatuan.
    for start in range(0, len(folder_ids), 500):
        batch = folder_ids[start : start + 500]
        total_size += int(
            db.scalar(
                select(func.coalesce(func.sum(StoredFile.size_bytes), 0)).where(
                    StoredFile.folder_id.in_(batch), StoredFile.is_deleted.is_(False)
                )
            )
            or 0
        )
        db.execute(
            update(StoredFile)
            .where(StoredFile.folder_id.in_(batch), StoredFile.is_deleted.is_(False))
            .values(is_deleted=True, deleted_at=now)
            .execution_options(synchronize_session=False)
        )
        db.execute(
            update(Folder)
            .where(Folder.id.in_(batch), Folder.is_deleted.is_(False))
            .values(is_deleted=True, deleted_at=now)
            .execution_options(synchronize_session=False)
        )
    item = TrashItem(
        owner_id=user.id,
        folder_id=folder.id,
        original_name=folder.name,
        original_folder_id=folder.parent_id,
        size_bytes=total_size,
        deleted_at=now,
        purge_after=now + timedelta(days=settings.TRASH_RETENTION_DAYS),
    )
    db.add(item)
    log_activity(
        db, "folder.delete", user_id=user.id, ip_address=client.ip_address, user_agent=client.user_agent,
        target_type="folder", target_id=folder.id, details={"name": folder.name, "soft": True},
    )
    db.commit()
    db.expire_all()
    return item
