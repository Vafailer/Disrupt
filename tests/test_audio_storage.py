import io
import logging
import os
import subprocess
import wave
from uuid import uuid4

import pytest

from app.audio_storage import (
    MAX_AUDIO_BYTES,
    AudioStorage,
    AudioStorageUnavailable,
    AudioTooLarge,
    UnsupportedAudio,
    _run,
)


def wav(seconds=0.1, rate=8000):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(b"\0\0" * int(seconds * rate))
    return stream.getvalue()


@pytest.fixture
def storage(tmp_path):
    return AudioStorage(tmp_path / "audio")


@pytest.mark.parametrize("fmt,media", [("wav", "audio/wav"), ("ogg", "audio/ogg"), ("webm", "audio/webm")])
def test_real_formats_and_immutable_original(storage, tmp_path, fmt, media):
    original = wav()
    if fmt != "wav":
        path = tmp_path / ("synthetic." + fmt)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "wav", "-i", "pipe:0", "-c:a", "libopus", str(path)], input=original, check=True)
        original = path.read_bytes()
    with storage.stage(io.BytesIO(original)) as staged:
        info = storage.inspect(staged)
        assert info.media_type == media
        assert info.duration_seconds == pytest.approx(0.1)
        capture_id = str(uuid4())
        saved = storage.publish(staged, capture_id, info)
        with pytest.raises(AudioStorageUnavailable):
            storage.publish(staged, capture_id, info)
    assert not list(storage.root.glob(".upload-*"))
    reader = AudioStorage(storage.root, read_only=True)
    with reader.open_original(saved.key) as source:
        assert source.read() == original
    assert (storage.root / saved.key).stat().st_mode & 0o777 == 0o600
    with pytest.raises(AudioStorageUnavailable):
        with reader.stage(io.BytesIO(original)):
            pass
    with pytest.raises(AudioStorageUnavailable):
        reader.inspect(staged)


@pytest.mark.parametrize("seconds,accepted", [(180, True), (180 + 1 / 8000, False)])
def test_real_sample_duration_limit(storage, seconds, accepted):
    with storage.stage(io.BytesIO(wav(seconds))) as staged:
        if accepted:
            assert storage.inspect(staged).duration_seconds == 180
        else:
            with pytest.raises(AudioTooLarge):
                storage.inspect(staged)


def test_size_is_actual_bytes(storage):
    with storage.stage(io.BytesIO(b"x" * MAX_AUDIO_BYTES)) as staged:
        assert staged.byte_count == MAX_AUDIO_BYTES
    with pytest.raises(AudioTooLarge):
        with storage.stage(io.BytesIO(b"x" * (MAX_AUDIO_BYTES + 1))):
            pass
    assert not list(storage.root.iterdir())


@pytest.mark.parametrize("payload", [b"", b"not audio", b"#EXTM3U\nhttp://example.test/private", wav()[:-500]])
def test_corrupt_or_non_audio(storage, payload):
    with storage.stage(io.BytesIO(payload)) as staged:
        with pytest.raises(UnsupportedAudio):
            storage.inspect(staged)


@pytest.mark.parametrize("key", ["../secret", "/tmp/secret.audio", "../" + str(uuid4()) + ".audio", "invalid.audio"])
def test_paths_are_not_accepted(storage, key):
    with pytest.raises(AudioStorageUnavailable):
        with storage.open_original(key):
            pass


def test_symlink_and_fifo_are_not_opened(storage, tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"private")
    key = str(uuid4()) + ".audio"
    (storage.root / key).symlink_to(secret)
    with pytest.raises(AudioStorageUnavailable):
        with storage.open_original(key):
            pass
    (storage.root / key).unlink()
    os.mkfifo(storage.root / key)
    with pytest.raises(AudioStorageUnavailable):
        with storage.open_original(key):
            pass


def test_missing_decoder_is_temporary(storage):
    storage.ffprobe_path = "/definitely/missing/ffprobe"
    with storage.stage(io.BytesIO(wav())) as staged:
        with pytest.raises(AudioStorageUnavailable):
            storage.inspect(staged)


def test_fsync_failure_never_returns_saved(storage, monkeypatch):
    with storage.stage(io.BytesIO(wav())) as staged:
        info = storage.inspect(staged)
        def fail(_):
            raise OSError("private disk detail")
        monkeypatch.setattr(os, "fsync", fail)
        with pytest.raises(AudioStorageUnavailable, match="storage_unavailable"):
            storage.publish(staged, str(uuid4()), info)
    assert not list(storage.root.iterdir())


def test_process_timeout_and_output_bound():
    import sys
    with pytest.raises(AudioStorageUnavailable, match="timeout"):
        _run([sys.executable, "-c", "import time; time.sleep(5)"], 10, timeout=0.1)
    with pytest.raises(AudioTooLarge):
        _run([sys.executable, "-c", "print('x'*100)"], 10)


@pytest.mark.parametrize("case", ["truncated_ogg", "vorbis", "matroska", "two_streams"])
def test_container_and_codec_restrictions(storage, tmp_path, case):
    path = tmp_path / "synthetic.bin"
    args = {"truncated_ogg": ["-c:a", "libopus", "-f", "ogg"],
            "vorbis": ["-c:a", "libvorbis", "-f", "ogg"],
            "matroska": ["-c:a", "libopus", "-f", "matroska"],
            "two_streams": ["-map", "0:a", "-map", "0:a", "-c:a", "libopus", "-f", "webm"]}[case]
    subprocess.run(["ffmpeg", "-v", "error", "-f", "wav", "-i", "pipe:0", *args, str(path)], input=wav(), check=True)
    data = path.read_bytes()
    if case == "truncated_ogg":
        data = data[:-1]
    with storage.stage(io.BytesIO(data)) as staged:
        with pytest.raises(UnsupportedAudio):
            storage.inspect(staged)


def test_busy_decoder_is_temporary_and_does_not_publish(storage):
    from app.audio_storage import _DECODE_SLOTS
    assert _DECODE_SLOTS.acquire(blocking=False)
    assert _DECODE_SLOTS.acquire(blocking=False)
    try:
        with storage.stage(io.BytesIO(wav())) as staged:
            with pytest.raises(AudioStorageUnavailable, match="decoder_busy"):
                storage.inspect(staged)
    finally:
        _DECODE_SLOTS.release()
        _DECODE_SLOTS.release()
    assert not list(storage.root.iterdir())


def ogg_crc(data):
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def last_page_start(data):
    start, pos = 0, 0
    while pos < len(data):
        assert data[pos:pos + 4] == b"OggS"
        start = pos
        count = data[pos + 26]
        pos += 27 + count + sum(data[pos + 27:pos + 27 + count])
    assert pos == len(data)
    return start


def clear_eos(data):
    """Rewrite the last Ogg page without the EOS flag and with a valid CRC."""
    start = last_page_start(data)
    page = bytearray(data[start:])
    assert page[5] & 4
    page[5] &= ~4
    page[22:26] = b"\0\0\0\0"
    page[22:26] = ogg_crc(page).to_bytes(4, "little")
    return data[:start] + bytes(page)


def opus_ogg(tmp_path, seconds=0.5):
    path = tmp_path / "synthetic.ogg"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "wav", "-i", "pipe:0", "-c:a", "libopus", "-f", "ogg", str(path)],
                   input=wav(seconds), check=True)
    return path.read_bytes()


def test_crc_helper_matches_ffmpeg_pages(tmp_path):
    data = opus_ogg(tmp_path)
    start = last_page_start(data)
    page = bytearray(data[start:])
    stored = bytes(page[22:26])
    page[22:26] = b"\0\0\0\0"
    assert ogg_crc(page).to_bytes(4, "little") == stored


def test_ogg_without_eos_flag_is_accepted(storage, tmp_path):
    data = clear_eos(opus_ogg(tmp_path))
    with storage.stage(io.BytesIO(data)) as staged:
        assert storage.inspect(staged).duration_seconds == pytest.approx(0.5, abs=0.05)


def test_ogg_cut_inside_a_page_is_still_rejected(storage, tmp_path):
    data = opus_ogg(tmp_path)
    for cut in (1, 10, (len(data) - last_page_start(data)) // 2):
        with storage.stage(io.BytesIO(data[:-cut])) as staged:
            with pytest.raises(UnsupportedAudio):
                storage.inspect(staged)


def test_rejection_reason_is_logged_without_private_data(storage, caplog):
    from app.integration import IntegrationRejection
    from app.routes.audio import audio_errors
    payload = b"#EXTM3U\nhttp://example.test/private"
    with caplog.at_level(logging.WARNING):
        with storage.stage(io.BytesIO(payload)) as staged:
            with pytest.raises(IntegrationRejection):
                with audio_errors("telegram"):
                    storage.inspect(staged)
    assert "audio_rejected reason=unsupported_container channel=telegram" in caplog.text
    assert "example.test" not in caplog.text and str(storage.root) not in caplog.text
