"""Private immutable originals. No database, provider, or network access."""

import hashlib
import json
import logging
import os
import selectors
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 180
logger = logging.getLogger(__name__)
_DECODE_SLOTS = threading.BoundedSemaphore(2)


class AudioTooLarge(ValueError):
    pass


class UnsupportedAudio(ValueError):
    pass


class AudioStorageUnavailable(OSError):
    pass


@dataclass(frozen=True)
class AudioInfo:
    sha256: str
    byte_count: int
    duration_seconds: float
    media_type: str


@dataclass(frozen=True)
class StoredAudio:
    key: str
    info: AudioInfo


@dataclass
class StagedAudio:
    path: Path
    sha256: str
    byte_count: int
    inspected: AudioInfo | None = None


def _key(capture_id):
    try:
        if str(UUID(capture_id)) != capture_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise AudioStorageUnavailable("invalid_audio_key") from None
    return capture_id + ".audio"


def _run(command, limit, *, collect=False, timeout=25):
    """Bound pipe output and wall time; child applies CPU/address-space limits."""
    wrapper = str(Path(__file__).with_name("audio_process.py"))
    try:
        process = subprocess.Popen(
            [sys.executable, wrapper, *command], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
            env={**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
        )
    except OSError:
        raise AudioStorageUnavailable("decoder_unavailable") from None
    total, parts, deadline = 0, [], time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AudioStorageUnavailable("decoder_timeout")
                if not selector.select(min(remaining, 0.2)):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise AudioTooLarge("audio_limit")
                if collect:
                    parts.append(chunk)
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if code == 127 or code < 0:
            raise AudioStorageUnavailable("decoder_unavailable")
        if code:
            raise UnsupportedAudio("invalid_audio")
        return b"".join(parts) if collect else total
    except subprocess.TimeoutExpired:
        raise AudioStorageUnavailable("decoder_timeout") from None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def _webm_header(data):
    def vint(pos, *, identifier=False):
        if pos >= len(data) or data[pos] == 0:
            raise UnsupportedAudio("invalid_container")
        length = 9 - data[pos].bit_length()
        if pos + length > len(data):
            raise UnsupportedAudio("invalid_container")
        value = int.from_bytes(data[pos:pos + length], "big")
        if not identifier:
            value &= (1 << (7 * length)) - 1
        return value, pos + length
    size, pos = vint(4)
    end = pos + size
    if end > len(data):
        raise UnsupportedAudio("invalid_container")
    doc_types = []
    while pos < end:
        tag, pos = vint(pos, identifier=True)
        size, pos = vint(pos)
        if pos + size > end:
            raise UnsupportedAudio("invalid_container")
        if tag == 0x4282:
            doc_types.append(data[pos:pos + size])
        pos += size
    if doc_types != [b"webm"]:
        raise UnsupportedAudio("unsupported_container")


def _ogg_complete(source):
    """Check every page is whole, one stream, contiguous numbering, nothing after EOS.

    The EOS flag on the last page is not required: several Telegram clients write
    voice notes without it. Truncation still shows as a page with missing body.
    """
    serial, sequence, ended = None, None, False
    while header := source.read(27):
        if len(header) != 27 or header[:5] != b"OggS\0" or ended:
            raise UnsupportedAudio("invalid_container")
        current = header[14:18]
        number = int.from_bytes(header[18:22], "little")
        if serial is None:
            serial = current
        if current != serial or (sequence is not None and number != sequence):
            raise UnsupportedAudio("unsupported_streams")
        sequence = number + 1
        lacing = source.read(header[26])
        if len(lacing) != header[26] or len(source.read(sum(lacing))) != sum(lacing):
            raise UnsupportedAudio("truncated_audio")
        ended = bool(header[5] & 4)


class AudioStorage:
    def __init__(self, root, *, ffmpeg_path="ffmpeg", ffprobe_path="ffprobe", read_only=False):
        self.root = Path(root).absolute()
        self.ffmpeg_path, self.ffprobe_path, self.read_only = str(ffmpeg_path), str(ffprobe_path), read_only
        try:
            if not read_only:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.is_symlink() or not self.root.is_dir():
                raise OSError()
        except OSError:
            raise AudioStorageUnavailable("storage_unavailable") from None

    def _writable(self):
        if self.read_only:
            raise AudioStorageUnavailable("storage_read_only")

    @contextmanager
    def stage(self, source):
        self._writable()
        path = None
        try:
            fd, name = tempfile.mkstemp(prefix=".upload-", dir=self.root)
            path = Path(name)
            size, digest = 0, hashlib.sha256()
            with os.fdopen(fd, "wb") as target:
                while chunk := source.read(65536):
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise AudioTooLarge("audio_limit")
                    digest.update(chunk)
                    target.write(chunk)
            # Empty input is classified by inspect inside the Inbox transaction.
            yield StagedAudio(path, digest.hexdigest(), size)
        except OSError:
            raise AudioStorageUnavailable("storage_unavailable") from None
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def inspect(self, staged):
        self._writable()
        if not _DECODE_SLOTS.acquire(blocking=False):
            raise AudioStorageUnavailable("decoder_busy")
        try:
            return self._inspect(staged)
        finally:
            _DECODE_SLOTS.release()

    def _inspect(self, staged):
        if staged.byte_count == 0:
            raise UnsupportedAudio("empty_audio")
        try:
            with staged.path.open("rb") as source:
                magic = source.read(16)
            if magic.startswith(b"OggS"):
                demuxer, media = "ogg", "audio/ogg"
                with staged.path.open("rb") as source:
                    _ogg_complete(source)
            elif magic[:4] == b"RIFF" and magic[8:12] == b"WAVE":
                demuxer, media = "wav", "audio/wav"
                if int.from_bytes(magic[4:8], "little") + 8 != staged.byte_count:
                    raise UnsupportedAudio("truncated_audio")
            elif magic.startswith(b"\x1a\x45\xdf\xa3"):
                demuxer, media = "matroska", "audio/webm"
                # ffprobe groups Matroska and WebM. Require EBML DocType=webm too.
                with staged.path.open("rb") as source:
                    header = source.read(4096)
                _webm_header(header)
            else:
                raise UnsupportedAudio("unsupported_container")
            common = ["-v", "error", "-max_alloc", "67108864", "-protocol_whitelist", "file", "-f", demuxer]
            metadata = json.loads(_run([
                self.ffprobe_path, *common, "-show_streams", "-show_format", "-of", "json", str(staged.path),
            ], 131072, collect=True))
            streams = metadata["streams"]
            if len(streams) != 1 or streams[0]["codec_type"] != "audio":
                raise UnsupportedAudio("unsupported_streams")
            stream = streams[0]
            codec = stream["codec_name"]
            if (demuxer == "wav" and not codec.startswith("pcm_")) or (demuxer != "wav" and codec != "opus"):
                raise UnsupportedAudio("unsupported_codec")
            rate = int(stream["sample_rate"])
            if not 1 <= rate <= 384000:
                raise UnsupportedAudio("unsupported_sample_rate")
            # Preserve the input sample rate; duration is a count of decoded samples.
            # Real Telegram Ogg files carry small muxer quirks that ffmpeg reports as
            # recoverable. Strict error detection stays on for WAV and WebM only.
            strict = [] if demuxer == "ogg" else ["-err_detect", "explode"]
            size = _run([
                self.ffmpeg_path, "-nostdin", "-xerror", *common, "-threads", "1", *strict,
                "-i", str(staged.path), "-map", "0:a:0", "-vn", "-sn", "-dn",
                "-threads", "1", "-filter_threads", "1", "-ac", "1", "-ar", str(rate),
                "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
            ], 2 * rate * MAX_AUDIO_SECONDS)
            if size == 0 or size % 2:
                raise UnsupportedAudio("invalid_audio")
            staged.inspected = AudioInfo(staged.sha256, staged.byte_count, size / (2 * rate), media)
            return staged.inspected
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, (AudioTooLarge, UnsupportedAudio)):
                raise
            raise UnsupportedAudio("invalid_audio") from None
        except OSError:
            raise AudioStorageUnavailable("storage_unavailable") from None

    def publish(self, staged, capture_id, info):
        self._writable()
        if info is not staged.inspected:
            raise AudioStorageUnavailable("audio_not_inspected")
        key = _key(capture_id)
        try:
            with staged.path.open("rb") as source:
                os.fsync(source.fileno())
            # Hard link is atomic and refuses overwriting an existing original.
            os.link(staged.path, self.root / key, follow_symlinks=False)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            raise AudioStorageUnavailable("storage_unavailable") from None
        return StoredAudio(key, info)

    @contextmanager
    def open_original(self, key):
        if not isinstance(key, str) or not key.endswith(".audio") or _key(key[:-6]) != key:
            raise AudioStorageUnavailable("invalid_audio_key")
        try:
            fd = os.open(self.root / key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise AudioStorageUnavailable("invalid_audio_file")
                yield source
        except OSError:
            raise AudioStorageUnavailable("storage_unavailable") from None
