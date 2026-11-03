"""Hourly EventBridge target: apply the pinned list, drop expired streamers, snapshot everyone still tracked."""
from __future__ import annotations

import json
import logging
from typing import Any

from .wiring import build_service

logger = logging.getLogger("clippulse.poller")
logger.setLevel(logging.INFO)


def handler(event: Any = None, context: Any = None) -> dict:
    summary = build_service().run_hourly()
    logger.info(json.dumps({"poll": summary}))
    return summary
