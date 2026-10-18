"""Keep one-time tokens out of the access log.

Emailed links carry their token in the query string (``/verify-email?token=...``) and
uvicorn's access log records the full request line, which on a host like Render ends up
in logs that more people can read than the user's inbox. This filter replaces the value.
"""

from __future__ import annotations

import logging
import re

_TOKEN_QUERY = re.compile(r"([?&]token=)[^&\s\"']+", re.IGNORECASE)
REDACTED = "[redacted]"


def redact_tokens(text: str) -> str:
    return _TOKEN_QUERY.sub(r"\1" + REDACTED, text)


class RedactTokensFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_tokens(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_tokens(a) if isinstance(a, str) else a for a in record.args)
        return True


def install_token_redaction() -> None:
    """Attach the filter to uvicorn's access logger (once, however often this is called)."""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactTokensFilter) for f in logger.filters):
        logger.addFilter(RedactTokensFilter())
