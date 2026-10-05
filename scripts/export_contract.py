"""Generate OpenAPI for integration consumers; future routes are marked explicitly."""

import json
from pathlib import Path

from fastapi import Depends, Query

from app.config import Settings
from app.contracts import (
    ActionRequest,
    ActionResponse,
    AdminSummary,
    AuthorizeRequest,
    AuthorizeResponse,
    ClaimRequest,
    ClaimResponse,
    ResultRequest,
    ResultResponse,
)
from app.main import create_app
from app.schemas import IntegrationError, TelegramCaptureResponse
from app.security import require_internal_service

app = create_app(Settings(auto_worker=False, database_url="sqlite:///:memory:"))
implemented = set(app.openapi()["paths"])


def actions(body: ActionRequest) -> ActionResponse:
    raise NotImplementedError


def claim(body: ClaimRequest) -> ClaimResponse:
    raise NotImplementedError


def authorize(delivery_id: str, body: AuthorizeRequest) -> AuthorizeResponse:
    raise NotImplementedError


def result(delivery_id: str, body: ResultRequest) -> ResultResponse:
    raise NotImplementedError


def summary(
    start: str = Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end: str = Query(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    channel: str = Query("all", pattern="^(all|web|telegram)$"),
    source: str = Query("all", max_length=100),
    usage_limit: int = Query(50, ge=1, le=100),
    usage_offset: int = Query(0, ge=0),
) -> AdminSummary:
    raise NotImplementedError


for path, endpoint in [
    ("/telegram/actions", actions),
    ("/deliveries/claim", claim),
    ("/deliveries/{delivery_id}/authorize", authorize),
    ("/deliveries/{delivery_id}/result", result),
]:
    app.add_api_route(
        "/internal/v1" + path,
        endpoint,
        methods=["POST"],
        tags=["internal-v1"],
        dependencies=[Depends(require_internal_service)],
        responses={c: {"model": IntegrationError} for c in (401, 403, 409, 413, 422, 429, 503)},
    )
app.add_api_route(
    "/api/admin/summary",
    summary,
    methods=["GET"],
    tags=["admin"],
    responses={
        200: {
            "headers": {
                "X-Next-Usage-Offset": {
                    "description": "Next offset, or omitted on last page",
                    "schema": {"type": "integer"},
                }
            }
        },
        401: {"description": "No browser session"},
        403: {"description": "Admin role required"},
    },
)
spec = app.openapi()
spec["paths"]["/internal/v1/telegram/updates"]["post"]["responses"]["200"]["content"]["application/json"]["examples"] = {
    mode: {"value": json.loads(Path(f"docs/fixtures/telegram-save-{mode}.json").read_text())}
    for mode in ("ai", "manual")
}
spec["paths"]["/api/admin/summary"]["get"]["responses"]["200"]["content"]["application/json"]["example"] = json.loads(
    Path("docs/fixtures/admin-summary.json").read_text()
)
for path, operations in spec["paths"].items():
    for method, operation in operations.items():
        if method in {"get", "post", "patch", "delete"}:
            operation["x-implementation-status"] = "implemented" if path in implemented else "contract-only"
            if path == "/api/admin/summary":
                operation["security"] = [{"BrowserSession": []}]
spec["components"]["securitySchemes"]["BrowserSession"] = {
    "type": "apiKey",
    "in": "cookie",
    "name": "notes_session",
}
spec["paths"]["/api/admin/export"] = {
    "get": {
        "operationId": "admin_export",
        "tags": ["admin"],
        "x-implementation-status": "contract-only",
        "security": [{"BrowserSession": []}],
        "parameters": [
            p
            for p in spec["paths"]["/api/admin/summary"]["get"]["parameters"]
            if p["name"] not in {"usage_limit", "usage_offset"}
        ],
        "responses": {
            "200": {
                "description": "UTF-8 CSV of aggregates, same slice as summary",
                "content": {"text/csv": {"schema": {"type": "string"}}},
            },
            "401": {"description": "No browser session"},
            "403": {"description": "Admin role required"},
        },
    }
}
spec["paths"]["/internal/v1/telegram/voice"] = {
    "post": {
        "operationId": "telegram_voice",
        "tags": ["internal-v1"],
        "x-implementation-status": "contract-only",
        "security": [{"InternalServiceToken": []}],
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "bot_id",
                            "update_id",
                            "telegram_user_id",
                            "chat_id",
                            "audio",
                            "processing_mode",
                        ],
                        "properties": {
                            **{
                                field: {
                                    "type": "integer",
                                    "format": "int64",
                                    "minimum": 0 if field == "update_id" else 1,
                                }
                                for field in ["bot_id", "update_id", "telegram_user_id", "chat_id"]
                            },
                            "audio": {"type": "string", "format": "binary"},
                            "processing_mode": {"type": "string", "enum": ["ai"]},
                        },
                    }
                }
            },
        },
        "responses": {
            "200": {
                "description": "Committed original and queued processing",
                "content": {
                    "application/json": {
                        "schema": {
                            "$ref": "#/components/schemas/" + TelegramCaptureResponse.__name__,
                        }
                    }
                },
            },
            **{
                str(c): {
                    "description": "Safe integration error",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/IntegrationError"},
                        }
                    },
                }
                for c in (401, 403, 409, 413, 422, 429, 503)
            },
        },
    }
}
Path("docs/integration-v1.openapi.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n")
app.state.engine.dispose()
