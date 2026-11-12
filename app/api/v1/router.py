from fastapi import APIRouter

from app.api.openapi_meta import COMMON_RESPONSES
from app.api.v1 import activity, admin, auth, files, folders, media, storage, system, trash, uploads

api_router = APIRouter(responses=COMMON_RESPONSES)
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
