from fastapi import APIRouter

from app.api.v1 import activity, admin, auth, files, folders, media, storage, system, trash, uploads

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(files.router)
api_router.include_router(folders.router)
api_router.include_router(uploads.router)
api_router.include_router(media.router)
api_router.include_router(storage.router)
api_router.include_router(trash.router)
api_router.include_router(activity.router)
api_router.include_router(admin.router)
api_router.include_router(system.router)
