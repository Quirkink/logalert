"""
Logalert Guard

Protection against recursion and amplification

:copyright: (c) 2025 Aiko Sora
"""

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

_local = threading.local()

ORIGIN_DENYLIST: tuple[str, ...] = (
    'logalert',
    'httpx',
    'httpcore',
    'h11',
    'anyio',
    'sniffio',
    'redis',
    'urllib3',
    'nio',
    'aiohttp',
    'asyncio',
)


class _OriginFilter(logging.Filter):
    """
    A filter attached only to **our** handlers
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """
        Keep the record unless its logger is on the denylist

        :param record: The record being filtered

        :return bool:
        """

        return not is_origin_denied(record.name)


_origin_filter = _OriginFilter()


def is_capturing() -> bool:
    """
    Whether a capture is in progress on the current thread right now

    :return bool:
    """

    return getattr(_local, 'capturing', False)


@contextmanager
def capture_guard() -> Iterator[bool]:
    """
    Enter the guarded section

    :return Iterator[bool]:
    """

    if getattr(_local, 'capturing', False):
        yield False

        return

    _local.capturing = True

    try:
        yield True

    finally:
        _local.capturing = False


def is_origin_denied(logger_name: str) -> bool:
    """
    Whether this logger is banned as an event origin

    :param logger_name: The name of the logger the record came from

    :return bool:
    """

    return any(
        logger_name == denied or logger_name.startswith(f'{denied}.') for denied in ORIGIN_DENYLIST
    )


def install_origin_filter(handler: logging.Handler) -> None:
    """
    Attach the origin filter to a handler

    :param handler: The handler the filter is added to

    :return None:
    """

    handler.addFilter(_origin_filter)


def reset_thread_state() -> None:
    """
    Reset the current thread's flag

    :return None:
    """

    _local.capturing = False


__all__ = (
    'install_origin_filter',
    'reset_thread_state',
    'is_origin_denied',
    'ORIGIN_DENYLIST',
    'capture_guard',
    'is_capturing',
)
