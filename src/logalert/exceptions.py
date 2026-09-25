"""
Logalert Exceptions

The library's exception hierarchy

:copyright: (c) 2025 Aiko Sora
"""


class LogAlertError(Exception):
    """
    Base exception for the library
    """


class InvalidOption(LogAlertError, ValueError):
    """
    An invalid ``init()`` option
    """


class MissingDependencyError(LogAlertError, ImportError):
    """
    A required extra is not installed
    """


class TransportError(LogAlertError):
    """
    Transport failure - the queue is unreachable or the message could not be
    enqueued
    """


class SinkError(LogAlertError):
    """
    Base class for delivery errors in a specific sink
    """

    idempotent: bool = False


class SinkTransientError(SinkError):
    """
    Transient failure: a timeout, a 5xx, a dropped connection
    """


class SinkPermanentError(SinkError):
    """
    Permanent failure: a bad token, a deleted chat, a room that is not reachable
    """


class SinkRateLimited(SinkError):
    """
    The recipient is asking us to wait
    """

    def __init__(self, message: str, *, retry_after: float) -> None:
        """
        Attach the pause the recipient asked for

        :param message: Error message
        :param retry_after: Seconds to wait, according to the recipient

        :return None:
        """

        super().__init__(message)

        self.retry_after = retry_after


class EncryptedRoomError(LogAlertError):
    """
    The Matrix room is encrypted, but the chosen backend cannot do E2E
    """


__all__ = (
    'MissingDependencyError',
    'EncryptedRoomError',
    'SinkPermanentError',
    'SinkTransientError',
    'SinkRateLimited',
    'TransportError',
    'InvalidOption',
    'LogAlertError',
    'SinkError',
)
