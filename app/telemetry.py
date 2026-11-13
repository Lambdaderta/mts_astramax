"""Structured events whose callers supply only operational metadata."""

import json
import logging


logger = logging.getLogger("uvicorn.error")


def record(event: str, **fields: str | int | float | bool | None) -> None:
    logger.info("%s", json.dumps({"event": event, **fields}, ensure_ascii=False))
