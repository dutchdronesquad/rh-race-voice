"""Cheap, dependency-free correlation telemetry for the race-audio pipeline."""

from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)


def record(event_id: str, stage: str, /, **fields: object) -> None:
    """Log one single-line JSON record correlating an event with a pipeline stage.

    Never raises: a bad field (non-finite float, non-serializable value) must not
    break the audio pipeline just because INFO logging happens to be enabled.
    """
    if not logger.isEnabledFor(logging.INFO):
        return
    payload = {"event_id": event_id, "stage": stage, "t": time.monotonic(), **fields}
    try:
        line = json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError):
        logger.warning("Race Voice telemetry: unrecordable %s payload", stage)
        return
    logger.info("%s", line)
