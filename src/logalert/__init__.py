"""
Logalert

Capture errors and tracebacks, delivered to Telegram and Matrix

:copyright: (c) 2025 Aiko Sora
"""

import logging
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from ._state import get_client, is_initialized, set_client
from ._version import __version__
from .capture import HookHandle, install_hooks, install_loop_hooks
from .client import Client
from .exceptions import (
    EncryptedRoomError,
    InvalidOption,
    LogAlertError,
    MissingDependencyError,
    SinkError,
    SinkPermanentError,
    SinkRateLimited,
    SinkTransientError,
    TransportError,
)
from .handler import AlertHandler, BreadcrumbHandler
from .options import Options
from .scope import Scope
from .sinks import MatrixSink, Sink, StderrSink, TelegramSink
from .types import Alert, Breadcrumb, EventLevel

_log = logging.getLogger('logalert.debug')
_lock = threading.RLock()

_client: Client | None = None
_hooks: HookHandle | None = None
_handler: AlertHandler | None = None
_breadcrumb_handler: BreadcrumbHandler | None = None


def init(
    *,
    sinks: Sequence[str | Sink] = (),
    telegram: Mapping[str, Any] | None = None,
    matrix: Mapping[str, Any] | None = None,
    redis_url: str | None = None,
    project: str | None = None,
    options: Options | None = None,
    capture_logs: bool = True,
    **kwargs: Any,
) -> Client:
    """
    Configure logalert and switch automatic capture on

    :param sinks: Sink names or sink objects to deliver with
    :param telegram: The ``telegram`` config section
    :param matrix: The ``matrix`` config section
    :param redis_url: Redis URL for the durable queue
    :param project: Project name, isolates the keys in Redis
    :param options: A ready :class:`Options`, instead of the arguments above
    :param capture_logs: Install the logging handler that captures records
    :param kwargs: Extra option fields, passed on to :class:`Options`

    :return Client:
    """

    global _client, _hooks, _handler, _breadcrumb_handler

    with _lock:
        _teardown()

        if options is not None:
            opts = options

        else:
            opts = Options(
                sinks=sinks,
                telegram=dict(telegram) if telegram else None,
                matrix=dict(matrix) if matrix else None,
                redis_url=redis_url,
                project=project,
                **kwargs,
            )

        if redis_url and not opts.redis_url:
            opts.redis_url = redis_url

        if project and not opts.project:
            opts.project = project

        client = Client(opts)
        _client = client
        set_client(client)
        _hooks = install_hooks(client, opts)

        if capture_logs:
            _handler = AlertHandler(client=client)
            logging.getLogger().addHandler(_handler)

            if opts.breadcrumbs > 0 and opts.breadcrumb_level:
                _breadcrumb_handler = BreadcrumbHandler(
                    level=opts.resolved_level_for_breadcrumb(), client=client
                )

                logging.getLogger().addHandler(_breadcrumb_handler)

        if opts.default_integrations:
            root = logging.getLogger()

            if root.level > logging.ERROR or root.level == logging.NOTSET:
                root.setLevel(logging.INFO)

        _log.debug('logalert initialized: %r', client)

        return client


def alert(
    exc_or_message: BaseException | str | None = None,
    *,
    level: EventLevel | None = None,
    logger_name: str = '',
) -> str | None:
    """
    Send an event by hand

    :param exc_or_message: The exception, the message, or ``None``
    :param level: The event level, ``error`` unless stated otherwise
    :param logger_name: The logger name to record on the event

    :return str | None:
    """

    client = _require_client()

    if isinstance(exc_or_message, BaseException):
        return client.capture(exc_or_message, level=level, logger_name=logger_name)

    message = None if exc_or_message is None else str(exc_or_message)

    return client.capture(None, message=message, level=level or 'error', logger_name=logger_name)


def flush(timeout: float | None = None) -> bool:
    """
    Wait for everything buffered to be sent out

    :param timeout: How long to wait, in seconds

    :return bool:
    """

    client = get_client()

    if client is None:
        return True

    return client.flush(timeout)


def shutdown() -> None:
    """
    Stop the library: drain the queue, remove hooks and handlers

    :return None:
    """

    with _lock:
        _teardown()


def _teardown() -> None:
    """
    Remove what `init` installed and close the client

    :return None:
    """

    global _client, _hooks, _handler, _breadcrumb_handler

    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
        _handler = None

    if _breadcrumb_handler is not None:
        logging.getLogger().removeHandler(_breadcrumb_handler)
        _breadcrumb_handler = None

    if _hooks is not None:
        _hooks.uninstall()
        _hooks = None

    if _client is not None:
        try:
            _client.flush()

        finally:
            _client.close()

        _client = None

    set_client(None)


def _require_client() -> Client:
    """
    Return the current client, refusing to go on without one

    :return Client:
    """

    client = get_client()

    if client is None:
        raise LogAlertError('logalert is not initialized: call logalert.init(...) before sending')

    return client


def tag(key: str, value: str) -> None:
    """
    Add a tag to every subsequent event

    :param key: The tag name
    :param value: The tag value

    :return None:
    """

    client = get_client()

    if client is not None:
        client.scope.set_tag(key, value)


def context(name: str, value: Mapping[str, Any]) -> None:
    """
    Add a context block. Secrets inside it are scrubbed before sending

    :param name: The name of the context block
    :param value: The context payload

    :return None:
    """

    client = get_client()

    if client is not None:
        client.scope.set_context(name, value)


def user(value: Mapping[str, str] | None) -> None:
    """
    Set the user. With ``send_default_pii=False`` only ``id`` goes out

    :param value: The user fields, or ``None`` to clear the user

    :return None:
    """

    client = get_client()

    if client is not None:
        client.scope.set_user(value)


def breadcrumb(message: str, **kwargs: Any) -> None:
    """
    Record an event in the buffer that gets attached to the next alert

    :param message: The breadcrumb message
    :param kwargs: Extra breadcrumb fields

    :return None:
    """

    client = get_client()

    if client is not None:
        client.add_breadcrumb(message, **kwargs)


def override_fingerprint(fingerprint: Sequence[str] | None) -> None:
    """
    Set the grouping by hand, bypassing the fingerprint algorithm

    :param fingerprint: The fingerprint to use, or ``None`` for the algorithm

    :return None:
    """

    client = get_client()

    if client is not None:
        client.scope.set_fingerprint(tuple(fingerprint) if fingerprint else None)


def dict_config(**init_kwargs: Any) -> dict[str, Any]:
    """
    A configuration fragment for ``logging.config.dictConfig``

    :param init_kwargs: Accepted and ignored: the fragment does not depend on
        them

    :return dict[str, Any]:
    """

    handler: dict[str, Any] = {'class': 'logalert.AlertHandler', 'level': 'ERROR'}

    return {
        'version': 1,
        'handlers': {'logalert': handler},
        'loggers': {'': {'handlers': ['logalert'], 'level': 'INFO'}},
        'disable_existing_loggers': False,
    }


__all__ = (
    'MissingDependencyError',
    'override_fingerprint',
    'EncryptedRoomError',
    'SinkPermanentError',
    'SinkTransientError',
    'install_loop_hooks',
    'BreadcrumbHandler',
    'SinkRateLimited',
    'TransportError',
    'is_initialized',
    'InvalidOption',
    'LogAlertError',
    'AlertHandler',
    'TelegramSink',
    '__version__',
    'dict_config',
    'Breadcrumb',
    'EventLevel',
    'MatrixSink',
    'StderrSink',
    'breadcrumb',
    'get_client',
    'SinkError',
    'shutdown',
    'Options',
    'context',
    'Alert',
    'Scope',
    'alert',
    'flush',
    'Sink',
    'init',
    'user',
    'tag',
)
