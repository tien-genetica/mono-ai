from __future__ import annotations

from typing import Any, Optional
import time
from functools import wraps
from datetime import datetime, timezone
from dateutil import parser
import asyncio


def parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = parser.parse(s)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def attach_elapsed_to_result(result: Any, elapsed_ms: int) -> Any:
    try:
        if isinstance(result, (list, tuple)):
            for item in result:
                if hasattr(item, "metadata") and isinstance(
                    getattr(item, "metadata", None), dict
                ):
                    item.metadata["elapsed_ms"] = elapsed_ms
            return result
        if hasattr(result, "metadata") and isinstance(
            getattr(result, "metadata", None), dict
        ):
            result.metadata["elapsed_ms"] = elapsed_ms
    except Exception:
        pass
    return result


def measure_time(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        start_ts = time.perf_counter()
        result = await func(*args, **kwargs)
        elapsed_ms = int((time.perf_counter() - start_ts) * 1000)
        return attach_elapsed_to_result(result, elapsed_ms)

    return wrapper


def with_retry(
    coro_func,
    *args,
    retries: int = 3,
    initial_delay_s: float = 0.5,
    backoff: float = 2.0,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
    **kwargs,
):
    async def runner():
        attempt = 0
        delay = float(initial_delay_s)
        while True:
            try:
                return await coro_func(*args, **kwargs)
            except retry_exceptions:
                attempt += 1
                if attempt > retries:
                    raise
                try:
                    await asyncio.sleep(delay)
                except Exception:
                    pass
                delay = delay * backoff if backoff and backoff > 0 else delay

    return runner()
