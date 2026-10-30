import logging
import re
import traceback
from typing import Any


_SECRET_PATTERNS = (
    re.compile(r"(https?://api\.telegram\.org/(?:file/)?bot)[^/\s\"']+", re.IGNORECASE),
    re.compile(r"(\bAuthorization\s*:\s*Bearer\s+)[^\s,;\"']+", re.IGNORECASE),
    re.compile(
        r"(\b(?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;\"']+",
        re.IGNORECASE,
    ),
)


def redact_secrets(value: Any) -> str:
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1<redacted>", text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Render the record once, redact it, then prevent unsafe arg reformatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secrets(record.getMessage())
        record.args = ()
        if record.exc_info:
            # Exception messages and chained provider payloads may contain the
            # original prompt. Keep stack locations and exception type only.
            kind, _, tb = record.exc_info
            record.exc_text = '\n'.join(
                f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}'
                for frame in traceback.extract_tb(tb)
            ) + '\n' + kind.__name__
            record.exc_info = None
        return True


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    redactor = SecretRedactingFilter()
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(item, SecretRedactingFilter) for item in handler.filters):
            handler.addFilter(redactor)
    # httpx logs every request URL at INFO, and Telegram puts the bot token in
    # the URL path; the per-poll getUpdates lines are noise anyway.
    logging.getLogger("httpx").setLevel(logging.WARNING)

