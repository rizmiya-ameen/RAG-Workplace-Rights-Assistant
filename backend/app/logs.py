"""Structured logs (step 19): one JSON object per line, no personal information.

- Never logged: the question, the answer, raw client IPs. A client appears as a salted hash
  (`client_hash`), the salt is random per process, so a log line cannot be turned back into
  an IP and hashes don't link across restarts. Enough to see one client hammering the API.
- Fields are passed with `extra={...}` and become top-level keys, so log tools (Azure Log
  Analytics, Langfuse via request_id) can filter on them.
- httpx logs every request URL at INFO (Gemini, Langfuse); it is set to WARNING.
"""

import hashlib
import json
import logging
import os
import sys
import time

_SALT = os.urandom(16)
# Attributes every LogRecord has; anything else came from `extra=`.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


def client_hash(ip: str | None) -> str:
    return hashlib.sha256(_SALT + (ip or "unknown").encode()).hexdigest()[:12]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_"):
                data[key] = value
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
