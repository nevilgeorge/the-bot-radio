"""Runs every input service concurrently, turning failures into skipped readings."""

import asyncio
import logging
from collections.abc import Sequence

from .base import InputService, Reading, RunContext

log = logging.getLogger(__name__)

SERVICE_TIMEOUT = 15.0


async def _read_one(service: InputService, ctx: RunContext) -> Reading:
    """Fetch one service's reading; any exception or timeout becomes a `skipped` reading."""
    try:
        timeout_seconds = getattr(service, "timeout", SERVICE_TIMEOUT)
        return await asyncio.wait_for(service.fetch(ctx), timeout=timeout_seconds)
    except Exception as exc:  # a failed source is skipped, never blocks the run
        error_message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        log.warning("input %s skipped: %s", service.key, error_message)
        return ctx.reading(service, "", status="skipped", error=error_message)


async def read_all(
    ctx: RunContext, services: Sequence[InputService] | None = None
) -> list[Reading]:
    """Fetch every input concurrently; results come back in display order."""
    if services is None:
        from . import SERVICES

        services = SERVICES
    return list(await asyncio.gather(*(_read_one(service, ctx) for service in services)))
