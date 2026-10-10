"""Generate OpenAPI for integration consumers; future routes are marked explicitly."""

import json
from pathlib import Path

from fastapi import Depends

from app.config import Settings
from app.contracts import (
    AuthorizeRequest,
    AuthorizeResponse,
    ClaimRequest,
    ClaimResponse,
    ResultRequest,
    ResultResponse,
)
from app.main import create_app
from app.schemas import AudioJobResponse, IntegrationError, TelegramCaptureResponse
from app.security import require_internal_service

app = create_app(Settings(auto_worker=False, database_url="sqlite:///:memory:"))
implemented = set(app.openapi()["paths"])


def claim(body: ClaimRequest) -> ClaimResponse:
    raise NotImplementedError


def authorize(delivery_id: str, body: AuthorizeRequest) -> AuthorizeResponse:
    raise NotImplementedError


def result(delivery_id: str, body: ResultRequest) -> ResultResponse:
    raise NotImplementedError



for path, endpoint in [
    ("/deliveries/claim", claim),
    ("/deliveries/{delivery_id}/authorize", authorize),
    ("/deliveries/{delivery_id}/result", result),
]:
    if "/internal/v1" + path in implemented:
        continue
    app.add_api_route(
        "/internal/v1" + path,
        endpoint,
        methods=["POST"],
        tags=["internal-v1"],
        dependencies=[Depends(require_internal_service)],
        responses={c: {"model": IntegrationError} for c in (400, 401, 403, 409, 413, 422, 429, 503)},
    )
spec = app.openapi()
spec["paths"]["/internal/v1/telegram/updates"]["post"]["responses"]["200"]["content"]["application/json"]["examples"] = {
    mode: {"value": json.loads(Path(f"docs/fixtures/telegram-save-{mode}.json").read_text())}
    for mode in ("ai", "manual")
}
spec["paths"]["/admin-api/v1/summary"]["get"]["responses"]["200"]["content"]["application/json"]["example"] = json.loads(
    Path("docs/fixtures/admin-summary.json").read_text()
)
for path, operations in spec["paths"].items():
    for method, operation in operations.items():
        if method in {"get", "post", "patch", "delete"}:
            operation["x-implementation-status"] = "implemented" if path in implemented else "contract-only"
            if path.startswith("/admin-api/") and path != "/admin-api/v1/login":
                operation["security"] = [{"AdminSession": []}]
spec["components"]["securitySchemes"]["BrowserSession"] = {
    "type": "apiKey",
    "in": "cookie",
    "name": "notes_session",
}
spec["components"]["securitySchemes"]["AdminSession"] = {
    "type": "apiKey",
    "in": "cookie",
    "name": "beresta_admin",
}
time_resolution = spec["paths"]["/api/v1/reminders/resolve-time"]["post"]
time_resolution["security"] = [{"BrowserSession": []}]
time_resolution["parameters"] = [{
    "name": "X-CSRF-Token", "in": "header", "required": True, "schema": {"type": "string"},
}]
spec["paths"]["/internal/v1/telegram/voice"] = {
    "post": {
        "operationId": "telegram_voice",
        "tags": ["internal-v1"],
        "x-implementation-status": "implemented" if "/internal/v1/telegram/voice" in implemented else "contract-only",
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
                                    "type": "string",
                                    "pattern": "^[0-9]+$",
                                    "description": "Decimal int64, maximum 9223372036854775807; "
                                    + ("zero allowed" if field == "update_id" else "positive"),
                                }
                                for field in ["bot_id", "update_id", "telegram_user_id", "chat_id"]
                            },
                            "audio": {
                                "type": "string", "format": "binary",
                                "description": "At most 10485760 bytes and 180 seconds; validate actual media on server",
                            },
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
                for c in (400, 401, 403, 409, 413, 422, 429, 503)
            },
        },
    }
}
spec["components"]["schemas"]["AudioJobResponse"] = AudioJobResponse.model_json_schema()
audio_schema = {
    "type": "string", "format": "binary",
    "description": "Original Ogg/Opus, WAV/PCM or WebM/Opus, verified by full decoding; "
    "at most 10485760 bytes and 180 seconds inclusive. Filename and declared MIME are not trusted.",
}
spec["paths"]["/internal/v1/telegram/voice"]["post"]["requestBody"]["content"]["multipart/form-data"][
    "schema"
]["properties"]["audio"] = audio_schema
browser_errors = {
    str(code): {
        "description": "Safe browser error",
        "content": {"application/json": {"schema": {
            "type": "object", "required": ["detail"],
            "properties": {"detail": {"type": "string"}},
        }}},
    }
    for code in (400, 401, 403, 404, 409, 413, 422, 429, 503)
}
spec["paths"]["/api/v1/captures/audio"] = {
    "post": {
        "operationId": "web_audio_capture",
        "tags": ["audio"],
        "x-implementation-status": "implemented" if "/api/v1/captures/audio" in implemented else "contract-only",
        "security": [{"BrowserSession": []}],
        "description": "Authenticate before multipart parsing. Reject repeated or extra fields. "
        "Commit the original and job before responding. Replay identical bytes without decoding again.",
        "parameters": [
            {"name": "X-CSRF-Token", "in": "header", "required": True, "schema": {"type": "string"}},
            {"name": "Idempotency-Key", "in": "header", "required": True, "schema": {
                "type": "string", "minLength": 1, "maxLength": 100, "pattern": "^[A-Za-z0-9_.:-]{1,100}$",
            }},
        ],
        "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
            "type": "object", "additionalProperties": False, "required": ["audio", "processing_mode"],
            "properties": {"audio": audio_schema, "processing_mode": {"type": "string", "enum": ["ai"]}},
        }}}},
        "responses": {
            "202": {"description": "Committed capture, current state of its job", "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/AudioJobResponse"}},
            }},
            **{code: response for code, response in browser_errors.items() if code != "404"},
        },
    },
}
spec["paths"]["/api/v1/captures/{capture_id}/audio"] = {
    "get": {
        "operationId": "download_audio_original",
        "tags": ["audio"],
        "x-implementation-status": "implemented" if "/api/v1/captures/{capture_id}/audio" in implemented else "contract-only",
        "security": [{"BrowserSession": []}],
        "description": "Check capture ownership before opening the file. STT failures do not remove access. "
        "Return 404 for missing, foreign or text captures. Never expose storage paths or keys.",
        "parameters": [{"name": "capture_id", "in": "path", "required": True, "schema": {"type": "string"}}],
        "responses": {
            "200": {
                "description": "Exact original bytes",
                "headers": {
                    "Content-Disposition": {"schema": {"type": "string"}, "description": "attachment; server filename"},
                    "Cache-Control": {"schema": {"type": "string", "const": "no-store"}},
                    "X-Content-Type-Options": {"schema": {"type": "string", "const": "nosniff"}},
                },
                "content": {media: {"schema": {"type": "string", "format": "binary"}}
                            for media in ("audio/ogg", "audio/wav", "audio/webm")},
            },
            **{code: browser_errors[code] for code in ("401", "404", "503")},
        },
    },
}
base = {"bot_id": 1234567890123, "update_id": 12, "telegram_user_id": 2345678901234, "chat_id": 2345678901234}
examples = {
    "reminder_time": {
        "request": {"local_time": "2090-10-08T12:00", "timezone": "Europe/Moscow"},
        "response": {
            "local_time": "2090-10-08T12:00", "timezone": "Europe/Moscow", "ambiguous": False,
            "choices": [{"scheduled_at": "2090-10-08T09:00:00+00:00", "local_at": "2090-10-08T12:00:00+03:00",
                         "utc_offset": "+03:00", "is_future": True}],
        },
    },
    "telegram_text": {
        mode: {
            "request": {**base, "text": "  Пример записи\n", "processing_mode": mode},
            "response": json.loads(Path(f"docs/fixtures/telegram-save-{mode}.json").read_text()),
        }
        for mode in ("ai", "manual")
    },
    "telegram_link": {
        "request": {**base, "code": "x" * 43},
        "response": {"link_request_id": "00000000-0000-4000-8000-000000000103", "status": "pending"},
    },
    "telegram_action": {
        "request": {
            "bot_id": base["bot_id"], "update_id": 13,
            "telegram_user_id": base["telegram_user_id"], "callback_token": "x" * 43,
        },
        "responses": [{"status": "completed"}, {"status": "already_completed"}],
    },
    "errors": json.loads(Path("docs/fixtures/integration-errors.json").read_text()),
}
for path, method in [
    ("/internal/v1/telegram/link-request", "post"),
    ("/internal/v1/telegram/updates", "post"),
    ("/internal/v1/telegram/actions", "post"),
    ("/internal/v1/telegram/voice", "post"),
]:
    for code, response in spec["paths"][path][method]["responses"].items():
        values = {
            item["response"]["error"]["code"]: {"value": item["response"]}
            for item in examples["errors"] if str(item["http_status"]) == code
        }
        if values and "application/json" in response.get("content", {}):
            response["content"]["application/json"]["examples"] = values
serialized = json.dumps(spec, ensure_ascii=False, indent=2) + "\n"
Path("docs/integration-v1.openapi.json").write_text(serialized)
# JSON is also valid YAML. Both consumer filenames are generated from the same object.
Path("docs/integration-v1.yaml").write_text(serialized)
Path("docs/integration-v1-examples.json").write_text(json.dumps(examples, ensure_ascii=False, indent=2) + "\n")
app.state.engine.dispose()
