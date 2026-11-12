"""Operasi administrasi pengguna."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import conflict, not_found
from app.core.security import SecurityService
from app.database.types import utcnow
from app.models import Folder, Role, StoredFile, Upload, User
from app.schemas.user import AdminUserUpdate
from app.services import auth_service


def get_user_or_404(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise not_found("user_not_found", "Pengguna tidak ditemukan")
    return user


def list_users(db: Session, *, page: int, page_size: int, search: str | None) -> tuple[list[User], int]:
    query = select(User)
    count_query = select(func.count(User.id))
    if search:
        pattern = f"%{search.lower()}%"
        cond = or_(User.username.like(pattern), User.email.like(pattern))
        query = query.where(cond)
        count_query = count_query.where(cond)
    total = db.scalar(count_query) or 0
    users = db.scalars(query.order_by(User.id).offset((page - 1) * page_size).limit(page_size)).all()
    return list(users), total


def _active_admin_count(db: Session) -> int:
    return db.scalar(
        select(func.count(User.id)).where(User.role == Role.ADMIN.value, User.is_active.is_(True))
    ) or 0


def update_user(
    db: Session,
    settings: Settings,
    security: SecurityService,
    actor: User,
    target: User,
    changes: AdminUserUpdate,
) -> list[str]:
    fields = changes.model_fields_set
    changed: list[str] = []
    revoke_sessions = False

    losing_admin = target.is_admin and (
        ("role" in fields and changes.role == Role.USER.value)
        or ("is_active" in fields and changes.is_active is False)
    )
    if losing_admin:
        if target.id == actor.id:
            raise conflict("cannot_demote_self", "Admin tidak boleh menonaktifkan/menurunkan dirinya sendiri")
        if _active_admin_count(db) <= 1:
            raise conflict("last_admin", "Tidak bisa menghapus admin aktif terakhir")

    if "email" in fields and changes.email != target.email:
        if changes.email and db.scalar(
            select(User.id).where(User.email == changes.email, User.id != target.id)
        ) is not None:
            raise conflict("email_taken", "Email sudah dipakai")
        target.email = changes.email
        changed.append("email")
    if "role" in fields and changes.role and changes.role != target.role:
        target.role = changes.role
        changed.append("role")
        revoke_sessions = True  # token lama membawa role lama
    if "is_active" in fields and changes.is_active is not None and changes.is_active != target.is_active:
        target.is_active = changes.is_active
        changed.append("is_active")
        if not changes.is_active:
            revoke_sessions = True
    if "storage_quota_bytes" in fields and changes.storage_quota_bytes != target.storage_quota_bytes:
        target.storage_quota_bytes = changes.storage_quota_bytes
        changed.append("storage_quota_bytes")
    if "password" in fields and changes.password:
        auth_service.validate_password_policy(settings, changes.password, target.username)
        target.password_hash = security.hash_password(changes.password)
        target.password_changed_at = utcnow()
        changed.append("password")
        revoke_sessions = True

    if revoke_sessions:
        auth_service.revoke_all_for_user(db, target.id, "admin_update")
    return changed


def delete_user(db: Session, actor: User, target: User) -> None:
    if target.id == actor.id:
        raise conflict("cannot_delete_self", "Admin tidak boleh menghapus akunnya sendiri")
    if target.is_admin and target.is_active and _active_admin_count(db) <= 1:
        raise conflict("last_admin", "Tidak bisa menghapus admin aktif terakhir")
    owns_data = any(
        db.scalar(select(model.id).where(model.owner_id == target.id).limit(1)) is not None
        for model in (StoredFile, Folder, Upload)
    )
    if owns_data:
        # Mencegah file fisik yatim di SSD. Nonaktifkan akun, atau kosongkan datanya dulu.
        raise conflict(
            "user_has_data",
            "Pengguna masih memiliki file/folder; nonaktifkan akun atau hapus datanya terlebih dahulu",
        )
    db.delete(target)
