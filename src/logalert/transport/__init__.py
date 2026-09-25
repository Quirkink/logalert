"""
Logalert Transport

Transports: how an event travels from the application to the sinks

:copyright: (c) 2025 Aiko Sora
"""

from .base import DirectTransport, Transport


def __getattr__(name: str) -> object:
    """
    The Redis transport is loaded lazily

    :param name: The attribute being looked up

    :return object:
    """

    if name in ('RedisStreamTransport', 'StreamConsumer'):
        from . import redis as _redis

        return getattr(_redis, name)

    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


__all__ = (
    'RedisStreamTransport',
    'DirectTransport',
    'StreamConsumer',
    'Transport',
)
