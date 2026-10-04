"""Запись ошибок без ключей и текста заметок."""

import json
import logging
import re
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger("notes.errors")
_file_lock = threading.Lock()
_safe_value = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")


def _safe(value: str) -> str:
    return value if _safe_value.fullmatch(value) else "redacted"


def log_error(
    event: str,
    code: str,
    *,
    log_file: str = "data/errors.log",
    job_id: str | None = None,
    request_id: str | None = None,
    http_status: int | None = None,
    method: str | None = None,
    exception: BaseException | None = None,
) -> None:
    """Сохраняет код, ID и место сбоя без текста исключения."""
    record = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": _safe(event),
        "code": _safe(code),
    }
    for key, value in (("job_id", job_id), ("request_id", request_id), ("method", method)):
        if value is not None:
            record[key] = _safe(value)
    if http_status is not None and 100 <= http_status <= 599:
        record["http_status"] = http_status
    if exception is not None:
        record["exception_type"] = _safe(type(exception).__name__)
        origin = exception.__traceback__
        if origin is not None:
            while origin.tb_next is not None:
                origin = origin.tb_next
            filename = _safe(Path(origin.tb_frame.f_code.co_filename).name)
            record["source"] = f"{filename}:{origin.tb_lineno}"

    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    logger.error("%s", line)  # Запись также видна в журнале процесса.
    if not log_file:
        return

    try:
        with _file_lock:
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            try:
                handler.emit(
                    logging.LogRecord(logger.name, logging.ERROR, __file__, 0, line, (), None)
                )
            finally:
                handler.close()
    except OSError:
        logger.error("Could not write the error log file")
