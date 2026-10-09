"""Disposable loopback browser QA. Run: PYTHONPATH=. python tests/manual_admin_server.py.
Never deploy this harness. Synthetic sessions and controls intentionally bypass login.
"""
import asyncio
import copy
import json
import tempfile
import time
from pathlib import Path

import uvicorn
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import delete

from app.config import Settings
from app.db import Base
from app.main import create_app
from app.models import LoginSession, User
from app.security import hash_token


def build(root):
    app = create_app(Settings(database_url="sqlite:///" + str(root / "qa.db"), auto_worker=False,
                              audio_storage_path=str(root / "audio"), error_log_file=str(root / "errors.log"),
                              public_origin="http://127.0.0.1:8765"))
    Base.metadata.create_all(app.state.engine)
    with app.state.sessions() as db:
        user = User(id="qa-admin", username="synthetic-admin", password_hash="unusable", role="admin", is_test=True)
        db.add(user)
        db.commit()
    fixture = json.loads((Path(__file__).parents[1] / "docs/fixtures/admin-summary.json").read_text())
    state = {"mode": "real"}

    @app.middleware("http")
    async def display_cases(request, call_next):
        response = await call_next(request)
        # Only this disposable harness permits framing its own synthetic admin page.
        if request.url.path == "/admin" and request.query_params.get("qa_embed") == "1":
            for header in ("x-frame-options", "content-security-policy"):
                if header in response.headers:
                    del response.headers[header]
        if request.url.path != "/api/admin/summary" or response.status_code != 200:
            return response
        if state["mode"] == "error":
            return JSONResponse({}, status_code=503)
        if state["mode"] == "loading":
            await asyncio.sleep(2)
        if state["mode"] in {"fixture", "loading"}:
            data = copy.deepcopy(fixture)
            rows = data["usage"]
            data["usage"] = [dict(rows[0], model="synthetic-model-" + "long-" * 30) for _ in range(50)] if rows else []
            offset = int(request.query_params.get("usage_offset", 0))
            headers = {"X-Next-Usage-Offset": "50"} if offset == 0 else {}
            return JSONResponse(data, headers=headers)
        return response

    @app.get("/qa/narrow")
    def narrow():
        return HTMLResponse("""<h1>Browser viewport 360 px</h1>
<iframe id="screen" src="/admin?qa_embed=1" width="360" height="780"></iframe>
""")

    @app.get("/qa")
    def controls():
        return HTMLResponse('<h1>Synthetic browser QA only</h1>' + ''.join(
            f'<form method="post" action="/qa/{mode}"><button>{mode}</button></form>'
            for mode in ("real", "fixture", "loading", "error", "revoke", "expire")) + '<a href="/admin">Admin</a>')

    @app.post("/qa/{mode}")
    def configure(mode: str):
        assert mode in {"real", "fixture", "loading", "error", "revoke", "expire"}
        with app.state.sessions() as db:
            user = db.get(User, "qa-admin")
            user.role = "user" if mode == "revoke" else "admin"
            db.execute(delete(LoginSession))
            if mode != "expire":
                db.add(LoginSession(token_hash=hash_token("synthetic-browser-session"), user_id=user.id,
                                    csrf_token="synthetic-csrf", expires_at=time.time() + 3600))
            db.commit()
        if mode not in {"revoke", "expire"}:
            state["mode"] = mode
        response = RedirectResponse("/admin" if mode not in {"revoke", "expire"} else "/qa", status_code=303)
        response.set_cookie("notes_session", "synthetic-browser-session", httponly=True, samesite="strict")
        return response
    return app


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="beresta-browser-qa-") as temp:
        uvicorn.run(build(Path(temp)), host="127.0.0.1", port=8765, access_log=False)
