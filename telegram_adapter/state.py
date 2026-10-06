import json
import os
import tempfile
from pathlib import Path


class OffsetStore:
    """Only an offset and bot ID; no message content or credentials on local disk."""

    def __init__(self, path: Path, bot_id: int):
        self.path, self.bot_id = path, bot_id
        self.offset = None
        if path.exists():
            data = json.loads(path.read_text())
            if data.get("bot_id") != bot_id or type(data.get("offset")) is not int or data["offset"] < 0:
                raise ValueError("Invalid bot checkpoint; do not silently discard it")
            self.offset = data["offset"]

    def advance(self, update_id):
        offset = update_id + 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".offset-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"bot_id": self.bot_id, "offset": offset}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.offset = offset
        finally:
            Path(name).unlink(missing_ok=True)


class InstanceLock:
    """One poller per state volume on macOS/Linux; Docker must run a single replica."""

    def __init__(self, state_file: Path):
        self.path = state_file.with_suffix(".lock")
        self.stream = None

    def __enter__(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.stream = os.fdopen(fd, "w")
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            raise ValueError("Another bot process owns this state volume") from None
        return self

    def __exit__(self, *args):
        import fcntl

        fcntl.flock(self.stream, fcntl.LOCK_UN)
        self.stream.close()
