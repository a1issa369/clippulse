"""Hourly EventBridge target: take a fresh view-count snapshot of every tracked streamer."""
from __future__ import annotations

import json
import logging
from typing import Any

from .wiring import build_service

logger = logging.getLogger("clippulse.poller")
logger.setLevel(logging.INFO)


def handler(event: Any = None, context: Any = None) -> dict:
    summary = build_service().poll_all()
    logger.info(json.dumps({"poll": summary}))
    return summary
