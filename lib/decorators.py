# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | If it compiles, ship it
import asyncio
import inspect
import time
from functools import wraps
from typing import Protocol

from .logger import logger


def locked(func):
    """Run at most one call to an async instance method at a time."""
    lock_attr = f"_{func.__name__}_lock"

    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        lock = getattr(self, lock_attr, None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(self, lock_attr, lock)
        async with lock:
            return await func(self, *args, **kwargs)

    return wrapper


def retry(max_attempts: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Retry decorator for async functions with exponential backoff."""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            wait = delay

            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_attempts:
                        logger.debug(
                            f"{func.__name__} failed (attempt {attempt}/{max_attempts}),"
                            f" retrying in {wait:.1f}s..."
                        )
                        await asyncio.sleep(wait)
                        wait *= backoff
                    else:
                        logger.error(f"{func.__name__} failed after {max_attempts} attempts")

            raise last_exception  # type: ignore

        return wrapper

    return decorator


def retry_on(exception: type[Exception], *, retries: int = 1):
    """Retry an async function immediately for one specific exception."""
    retries = max(0, retries)

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exception as e:
                    if attempt == retries:
                        raise
                    message = str(e) or f"{func.__name__}: retrying after {exception.__name__}"
                    logger.debug(message)

        return wrapper

    return decorator


def ttl_cache(ttl: int):
    def decorator(func):
        cache = {}
        timestamps = {}

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            now = time.time()
            key = (args, frozenset(kwargs.items()))

            if key not in cache or now - timestamps[key] > ttl:
                cache[key] = func(*args, **kwargs)
                timestamps[key] = now

            return cache[key]

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            now = time.time()
            key = (args, frozenset(kwargs.items()))

            if key not in cache or now - timestamps[key] > ttl:
                cache[key] = await func(*args, **kwargs)
                timestamps[key] = now

            return cache[key]

        return async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper

    return decorator


# MARK: Log context decorator


class HasName(Protocol):
    @property
    def name(self) -> str: ...


def bind_log_context[T: HasName](cls: type[T]) -> type[T]:
    for attr_name, attr in cls.__dict__.items():
        if attr_name.startswith("__") or not callable(attr) or isinstance(attr, staticmethod):
            continue

        if inspect.iscoroutinefunction(attr):

            @wraps(attr)
            async def async_wrapper(self, *args, _attr=attr, **kwargs):
                val = getattr(self, "name", None)
                val = str(self) if not isinstance(val, str) else val
                with logger.contextualize(account=val):
                    return await _attr(self, *args, **kwargs)

            setattr(cls, attr_name, async_wrapper)

        else:

            @wraps(attr)
            def sync_wrapper(self, *args, _attr=attr, **kwargs):
                val = getattr(self, "name", None)
                val = str(self) if not isinstance(val, str) else val
                with logger.contextualize(account=val):
                    return _attr(self, *args, **kwargs)

            setattr(cls, attr_name, sync_wrapper)

    return cls
