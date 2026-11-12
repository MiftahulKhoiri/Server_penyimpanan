from fastapi import APIRouter

from app.api.v1 import admin, auth, files, folders, system, uploads

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(files.router)
api_router.include_router(folders.router)
api_router.include_router(uploads.router)
api_router.include_router(admin.router)
api_router.include_router(system.router)
