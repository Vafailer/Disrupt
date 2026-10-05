"""Protected entry point for the founder's screen. Aggregates arrive in day 3."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.security import require_admin


class ProtectedStaticFiles(StaticFiles):
    def __init__(self, *, sessions, **kwargs):
        super().__init__(**kwargs)
        self.sessions = sessions

    async def get_response(self, path, scope):
        if Path(path).name.startswith("admin."):
            with self.sessions() as db:
                require_admin(Request(scope), db)
        return await super().get_response(path, scope)


def build_router(database, static):
    router = APIRouter(tags=["admin"])

    @router.get("/admin", include_in_schema=False)
    def admin(request: Request, db=Depends(database)):
        require_admin(request, db)
        screen = static / "admin.html"
        if not screen.is_file():
            raise HTTPException(503, "Экран админки ещё не подключён")
        return FileResponse(screen)

    return router
