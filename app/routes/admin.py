"""Protected screen, aggregate API and CSV. The founder owns the screen files."""

import secrets
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.admin_metrics import aggregate, export_csv
from app.contracts import AdminSummary
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


def build_router(database, static, settings):
    router = APIRouter(tags=["admin"])
    key = settings.analytics_pseudonym_key.encode() if settings.analytics_pseudonym_key else secrets.token_bytes(32)

    def admin_database(request: Request, db=Depends(database)):
        # Authorisation and every aggregate share one read snapshot, including concurrent worker writes.
        if db.get_bind().dialect.name == "postgresql":
            db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        else:
            db.execute(text("BEGIN"))
        require_admin(request, db)
        return db

    errors = {401: {"description": "No browser session"}, 403: {"description": "Admin role required"}}

    @router.get("/api/admin/summary", response_model=AdminSummary, responses={
        **errors, 200: {"headers": {"X-Next-Usage-Offset": {
            "description": "Next offset, omitted on the last page", "schema": {"type": "integer"},
        }}},
    })
    def summary(
        response: Response,
        db=Depends(admin_database),
        start: str = Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
        end: str = Query(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
        channel: Literal["all", "web", "telegram"] = "all",
        source: str = Query("all", min_length=1, max_length=100),
        usage_limit: int = Query(50, ge=1, le=100),
        usage_offset: int = Query(0, ge=0),
    ):
        result, next_offset = aggregate(db, start, end, channel, source, key, usage_limit=usage_limit, usage_offset=usage_offset)
        if next_offset is not None:
            response.headers["X-Next-Usage-Offset"] = str(next_offset)
        return result

    @router.get("/api/admin/export", response_class=Response, responses={
        **errors, 200: {"description": "UTF-8 CSV of aggregates", "content": {"text/csv": {"schema": {"type": "string"}}}},
    })
    def export(
        db=Depends(admin_database),
        start: str = Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
        end: str = Query(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
        channel: Literal["all", "web", "telegram"] = "all",
        source: str = Query("all", min_length=1, max_length=100),
    ):
        result, _ = aggregate(db, start, end, channel, source, key, usage_limit=1)
        return Response(
            export_csv(result, {"from": start, "to": end, "channel": channel, "source": source}),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="beresta-{start}-{end}.csv"', "Cache-Control": "no-store"},
        )

    @router.get("/admin", include_in_schema=False)
    def admin(request: Request, db=Depends(database)):
        require_admin(request, db)
        screen = static / "admin.html"
        if not screen.is_file():
            raise HTTPException(503, "Экран админки ещё не подключён")
        return FileResponse(screen)

    return router
