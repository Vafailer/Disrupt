"""Administrator screen, sign-in, statistics, metrics archive, feedback inbox and audit log.

Everything here is reachable only from the private network (see app.admin_auth) and only with a
separate administrator session. A regular user session never works on these routes.
"""

import secrets
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text, update

from app import admin_auth as auth
from app.admin_export import build_archive
from app.admin_metrics import aggregate, export_csv
from app.admin_models import AdminAccount, AdminAudit
from app.contracts import AdminSummary
from app.feedback_models import Feedback
from app.routes.feedback import FeedbackUpdate, FeedbackView, view
from app.schemas import StrictModel


class ProtectedStaticFiles(StaticFiles):
    """Admin assets are not secret, but they exist only inside the private network."""

    def __init__(self, *, networks, **kwargs):
        super().__init__(**kwargs)
        self.networks = networks

    async def get_response(self, path, scope):
        if Path(path).name.startswith("admin.") and not auth.peer_allowed(scope, self.networks):
            raise HTTPException(404, "Not Found")
        return await super().get_response(path, scope)


class LoginBody(StrictModel):
    # Loose limits on purpose: any wrong value gets the same generic answer from the server.
    username: str
    password: str
    totp: str


DATE = r"^\d{4}-\d{2}-\d{2}$"


def build_router(database, static, settings):
    router = APIRouter(tags=["admin"])
    key = settings.analytics_pseudonym_key.encode() if settings.analytics_pseudonym_key else secrets.token_bytes(32)
    stable_key = bool(settings.analytics_pseudonym_key)

    def admin_database(request: Request, db=Depends(database)):
        request.state.admin_id = auth.authenticate(request, db).admin.id
        db.commit()  # End the sign-in check, so the snapshot below starts clean.
        # Every aggregate shares one read snapshot, including concurrent worker writes.
        if db.get_bind().dialect.name == "postgresql":
            db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        else:
            db.execute(text("BEGIN"))
        return db

    errors = {401: {"description": "No administrator session"}, 404: {"description": "Outside the private network"}}

    @router.get("/admin", include_in_schema=False)
    def admin_page():
        screen = static / "admin.html"
        if not screen.is_file():
            raise HTTPException(503, "Экран админки ещё не подключён")
        return FileResponse(screen)

    failures = {401: {"description": "Generic failure"}, 429: {"description": "Too many attempts"}}

    @router.post("/admin-api/v1/login", responses=failures)
    def login(body: LoginBody, request: Request, response: Response, db=Depends(database)):
        auth.check_same_origin(request)
        token, csrf, admin = auth.sign_in(
            db, username=body.username, password=body.password, code=body.totp, ip=auth.client_ip(request),
        )
        response.set_cookie(
            auth.COOKIE_NAME, token, httponly=True, samesite="strict", secure=settings.admin_cookie_secure,
            path="/", max_age=auth.SESSION_SECONDS,
        )
        return {"username": admin.username, "csrf_token": csrf, "expires_at": time.time() + auth.SESSION_SECONDS}

    @router.post("/admin-api/v1/logout", status_code=204, responses=errors)
    def logout(request: Request, db=Depends(database)):
        context = auth.authenticate(request, db, write=True)
        db.delete(context.session)
        auth.audit(db, "logout", admin_id=context.admin.id, ip=auth.client_ip(request))
        db.commit()
        response = Response(status_code=204)
        response.delete_cookie(
            auth.COOKIE_NAME, path="/", secure=settings.admin_cookie_secure, httponly=True, samesite="strict",
        )
        return response

    @router.get("/admin-api/v1/me", responses=errors)
    def me(request: Request, db=Depends(database)):
        context = auth.authenticate(request, db)
        return {
            "username": context.admin.username, "csrf_token": auth.csrf_for(context.token),
            "expires_at": context.session.expires_at, "idle_seconds": auth.IDLE_SECONDS,
        }

    @router.get("/admin-api/v1/summary", response_model=AdminSummary, responses={
        **errors, 200: {"headers": {"X-Next-Usage-Offset": {
            "description": "Next offset, omitted on the last page", "schema": {"type": "integer"},
        }}},
    })
    def summary(
        response: Response,
        db=Depends(admin_database),
        start: str = Query(alias="from", pattern=DATE),
        end: str = Query(alias="to", pattern=DATE),
        channel: Literal["all", "web", "telegram"] = "all",
        source: str = Query("all", min_length=1, max_length=100),
        usage_limit: int = Query(50, ge=1, le=100),
        usage_offset: int = Query(0, ge=0),
    ):
        result, next_offset = aggregate(db, start, end, channel, source, key, usage_limit=usage_limit, usage_offset=usage_offset)
        if next_offset is not None:
            response.headers["X-Next-Usage-Offset"] = str(next_offset)
        return result

    @router.get("/admin-api/v1/export", response_class=Response, responses={
        **errors, 200: {"description": "UTF-8 CSV of aggregates", "content": {"text/csv": {"schema": {"type": "string"}}}},
    })
    def export(
        request: Request,
        db=Depends(admin_database),
        start: str = Query(alias="from", pattern=DATE),
        end: str = Query(alias="to", pattern=DATE),
        channel: Literal["all", "web", "telegram"] = "all",
        source: str = Query("all", min_length=1, max_length=100),
    ):
        result, _ = aggregate(db, start, end, channel, source, key, usage_limit=1)
        body = export_csv(result, {"from": start, "to": end, "channel": channel, "source": source})
        auth.audit(db, "export_csv", admin_id=request.state.admin_id, ip=auth.client_ip(request),
                   details={"from": start, "to": end, "channel": channel})
        db.commit()
        return Response(
            body, media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="beresta-{start}-{end}.csv"', "Cache-Control": "no-store"},
        )

    @router.get("/admin-api/v1/export.zip", response_class=Response, responses={
        **errors, 200: {"description": "ZIP archive of pseudonymous metrics", "content": {
            "application/zip": {"schema": {"type": "string", "format": "binary"}}}},
    })
    def export_zip(
        request: Request,
        db=Depends(admin_database),
        start: str = Query(alias="from", pattern=DATE),
        end: str = Query(alias="to", pattern=DATE),
    ):
        data, stats = build_archive(db, start, end, key, stable_key=stable_key)
        auth.audit(db, "export_zip", admin_id=request.state.admin_id, ip=auth.client_ip(request),
                   details={"from": start, "to": end, **stats})
        db.commit()
        return Response(
            data, media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="beresta-metrics-{start}-{end}.zip"',
                     "Cache-Control": "no-store"},
        )

    @router.get("/admin-api/v1/feedback", response_model=list[FeedbackView], responses=errors)
    def inbox(request: Request, response: Response, db=Depends(database),
              status: Literal["all", "new", "in_progress", "resolved"] = "new",
              limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        auth.authenticate(request, db)
        query = select(Feedback)
        if status != "all":
            query = query.where(Feedback.status == status)
        rows = db.scalars(query.order_by(Feedback.created_at.desc(), Feedback.id.desc()).offset(offset).limit(limit + 1)).all()
        if len(rows) > limit:
            response.headers["X-Next-Feedback-Offset"] = str(offset + limit)
        response.headers["Cache-Control"] = "no-store"
        return [view(row) for row in rows[:limit]]

    @router.patch("/admin-api/v1/feedback/{feedback_id}", response_model=FeedbackView, responses=errors)
    def change(feedback_id: str, body: FeedbackUpdate, request: Request, db=Depends(database)):
        context = auth.authenticate(request, db, write=True)
        if db.get(Feedback, feedback_id) is None:
            raise HTTPException(404, "Обращение не найдено")
        changed = db.execute(update(Feedback).where(Feedback.id == feedback_id, Feedback.version == body.version).values(
            status=body.status, version=Feedback.version + 1, updated_at=time.time(),
        )).rowcount
        if not changed:
            db.rollback()
            raise HTTPException(409, "Статус уже изменился. Обновите список обращений.")
        auth.audit(db, "feedback_status", admin_id=context.admin.id, target=feedback_id,
                   ip=auth.client_ip(request), details={"status": body.status})
        db.commit()
        return view(db.get(Feedback, feedback_id))

    @router.get("/admin-api/v1/audit", responses=errors)
    def audit_log(request: Request, db=Depends(database), limit: int = Query(100, ge=1, le=100)):
        auth.authenticate(request, db)
        rows = db.scalars(select(AdminAudit).order_by(AdminAudit.created_at.desc(), AdminAudit.id.desc()).limit(limit)).all()
        names = dict(db.execute(select(AdminAccount.id, AdminAccount.username)).all())
        return [
            {"id": row.id, "created_at": row.created_at, "admin": names.get(row.admin_id), "action": row.action,
             "target": row.target, "ip": row.ip, "details": row.details}
            for row in rows
        ]

    return router
