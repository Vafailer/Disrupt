from pathlib import Path

import httpx
import yaml

from app.config import PROGRAM_BASE_URL
from app.stt_probe import main

KEY = "synthetic-probe-secret-key"
ROOT = Path(__file__).resolve().parents[1]


def prepare(monkeypatch, tmp_path, *, live=True):
    key_file = tmp_path / "key"
    key_file.write_text(KEY + "\n", encoding="utf-8")
    for name in ("NOTES_CLOUDRU_API_KEY", "NOTES_DATABASE_URL", "NOTES_DATABASE_PASSWORD_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NOTES_CLOUDRU_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("NOTES_ALLOW_LIVE_REQUESTS", "true" if live else "false")
    monkeypatch.setenv("NOTES_CLOUDRU_BASE_URL", PROGRAM_BASE_URL)


def audio(tmp_path, name="voice.ogg"):
    path = tmp_path / name
    path.write_bytes(b"synthetic audio bytes")
    return str(path)


def assert_no_key(capsys):
    captured = capsys.readouterr()
    assert KEY not in captured.out and KEY not in captured.err
    return captured


def test_refuses_without_live_permission(monkeypatch, tmp_path, capsys):
    prepare(monkeypatch, tmp_path, live=False)
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200))
    assert main(["--list"], transport=transport) == 2
    captured = assert_no_key(capsys)
    assert "NOTES_ALLOW_LIVE_REQUESTS=true" in captured.err and not calls


def test_usage_without_arguments(monkeypatch, tmp_path, capsys):
    prepare(monkeypatch, tmp_path)
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_list_marks_audio_models(monkeypatch, tmp_path, capsys):
    prepare(monkeypatch, tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and str(request.url) == PROGRAM_BASE_URL + "/models"
        assert request.headers["Authorization"] == "Bearer " + KEY
        return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}, {"id": "Whisper-Large-V3"}]})

    assert main(["--list"], transport=httpx.MockTransport(handler)) == 0
    lines = assert_no_key(capsys).out.splitlines()
    assert len(calls) == 1
    assert any(line.startswith("Whisper-Large-V3") and "аудио" in line for line in lines)
    assert any(line == "deepseek-v4-flash" for line in lines)


def test_file_success_prints_length_not_key(monkeypatch, tmp_path, capsys):
    prepare(monkeypatch, tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == PROGRAM_BASE_URL + "/audio/transcriptions"
        assert b"synthetic-stt" in request.content and b"synthetic audio bytes" in request.content
        return httpx.Response(200, json={"text": "Привет " * 100})

    transport = httpx.MockTransport(handler)
    code = main(["--file", audio(tmp_path), "--model", "synthetic-stt"], transport=transport)
    out = assert_no_key(capsys).out
    assert code == 0 and len(calls) == 1
    assert "700 символов" in out and ("Привет " * 28) in out and ("Привет " * 30) not in out


def test_model_not_found_maps_to_code(monkeypatch, tmp_path, capsys):
    prepare(monkeypatch, tmp_path)
    transport = httpx.MockTransport(lambda request: httpx.Response(404, text=KEY))
    assert main(["--file", audio(tmp_path), "--model", "missing"], transport=transport) == 1
    assert "stt_model_not_found" in assert_no_key(capsys).err


def test_list_error_body_is_trimmed_and_key_hidden(monkeypatch, tmp_path, capsys):
    prepare(monkeypatch, tmp_path)
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text=KEY + "x" * 1000))
    assert main(["--list"], transport=transport) == 1
    err = assert_no_key(capsys).err
    assert len(err) < 600


def test_unknown_extension_rejected(monkeypatch, tmp_path, capsys):
    prepare(monkeypatch, tmp_path)
    assert main(["--file", audio(tmp_path, "voice.mp3"), "--model", "m"]) == 2
    assert_no_key(capsys)


def test_worker_compose_passes_disabled_stt_settings():
    compose = yaml.safe_load((ROOT / "compose.production.yaml").read_text(encoding="utf-8"))
    names = ("NOTES_STT_ENABLED", "NOTES_STT_PROTOCOL_CONFIRMED", "NOTES_STT_MODEL")
    worker = compose["services"]["worker"]["environment"]
    assert worker["NOTES_STT_ENABLED"] == "${NOTES_STT_ENABLED:-false}"
    assert worker["NOTES_STT_PROTOCOL_CONFIRMED"] == "${NOTES_STT_PROTOCOL_CONFIRMED:-false}"
    assert worker["NOTES_STT_MODEL"] == "${NOTES_STT_MODEL:-}"
    for service in ("api", "scheduler", "migrate"):
        assert not set(names) & set(compose["services"][service].get("environment", {}))
