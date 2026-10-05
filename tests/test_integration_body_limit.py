import asyncio

import pytest

from app.main import BodyLimit


@pytest.mark.parametrize(
    "path,method,content_type,size,status",
    [
        ("/internal/v1/telegram/voice", "POST", "multipart/form-data; boundary=test", 10 * 1024 * 1024, 200),
        ("/internal/v1/telegram/voice", "POST", "multipart/form-data; boundary=test", 10 * 1024 * 1024 + 65537, 413),
        ("/internal/v1/telegram/voice", "POST", "application/json", 131073, 413),
        ("/internal/v1/telegram/voice", "PATCH", "multipart/form-data; boundary=test", 131073, 413),
        ("/internal/v1/telegram/updates", "POST", "multipart/form-data; boundary=test", 131073, 413),
        ("/internal/v1/telegram/updates", "POST", "application/json", 131072, 200),
    ],
)
def test_body_limit_counts_actual_chunks_and_only_relaxes_voice(path, method, content_type, size, status):
    messages, received = [], []

    async def sink(scope, receive, send):
        message = await receive()
        received.append(len(message["body"]))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def run():
        remaining = size

        async def receive():
            nonlocal remaining
            chunk = min(remaining, 32768)
            remaining -= chunk
            return {"type": "http.request", "body": b"x" * chunk, "more_body": bool(remaining)}

        async def send(message):
            messages.append(message)

        await BodyLimit(sink)(
            {
                "type": "http", "path": path, "method": method,
                "headers": [(b"content-type", content_type.encode()), (b"content-length", b"1")],
            },
            receive, send,
        )

    asyncio.run(run())
    assert messages[0]["status"] == status
    if status == 200:
        assert received == [size]
    else:
        assert received == []
        assert b'"code":"input_too_large"' in messages[1]["body"]
