"""
Logalert Sinks Base

The public sink protocol

:copyright: (c) 2025 Aiko Sora
"""

import importlib
import logging
from typing import Any, ClassVar, Protocol, runtime_checkable

from ..exceptions import (
    MissingDependencyError,
    SinkError,
    SinkPermanentError,
    SinkRateLimited,
    SinkTransientError,
)
from ..options import Options
from ..types import Alert

_log = logging.getLogger('logalert.debug')

@runtime_checkable
class Sink(Protocol):
    """
    Where alerts get delivered. Implement this protocol to add your own
    messenger
    """

    name: ClassVar[str]
    idempotent: ClassVar[bool]

    def send(self, alert: Alert, *, key: str, timeout: float) -> None:
        """
        Deliver the alert. Report a failure with an exception, not a return
        value

        :param alert: The event to deliver
        :param key: Delivery key, reused across retries
        :param timeout: How long the send may block

        :return None:
        """

        ...

    def healthcheck(self) -> None:
        """
        Check reachability without sending anything. Raise on failure

        :return None:
        """

        ...

    def close(self) -> None:
        """
        Free resources. Must be idempotent and survive a call from atexit

        :return None:
        """

        ...


def require(module: str, extra: str) -> Any:
    """
    Import an optional dependency, failing with a message that makes sense

    :param module: Name of the module to import
    :param extra: Name of the extra that provides it

    :return Any:
    """

    try:
        return importlib.import_module(module)

    except ImportError as exc:
        raise MissingDependencyError(
            f'this sink needs the package {module!r}, and it is not installed. '
            f'Install it with: pip install logalert[{extra}]'
        ) from exc


def classify_http_status(status: int) -> type[SinkError] | None:
    """
    HTTP response - error class. ``None`` means success

    :param status: The HTTP status code to classify

    :return type[SinkError] | None:
    """

    if 200 <= status < 300:
        return None

    if status == 429:
        return SinkRateLimited

    if status in (408, 425) or 500 <= status < 600:
        return SinkTransientError

    return SinkPermanentError


def bind_options(sink: Sink, options: Options) -> None:
    """
    Hand the options to the sink, if it accepts them

    :param sink: The sink to hand the options to
    :param options: The resolved options

    :return None:
    """

    binder = getattr(sink, 'bind', None)

    if callable(binder):
        try:
            binder(options)

        except Exception:
            _log.debug('sink %r refused to accept options', getattr(sink, 'name', '?'))


__all__ = (
    'classify_http_status',
    'bind_options',
    'require',
    'Sink',
)
