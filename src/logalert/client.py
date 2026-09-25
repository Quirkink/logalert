"""
Logalert Client

Client: the pipeline from event to queue

:copyright: (c) 2025 Aiko Sora
"""

import contextlib
import logging
import random
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from . import _privacy
from ._dedupe import build_dedupe
from ._delivery import Deliverer
from ._fingerprint import fingerprint as compute_fingerprint
from ._guard import capture_guard
from .capture import alert_from_exception, should_ignore
from .exceptions import InvalidOption
from .options import Options
from .scope import Scope
from .sinks.base import bind_options
from .transport.base import DirectTransport, Transport
from .types import Alert, new_event_id

_log = logging.getLogger('logalert.debug')


class Client:
    """
    A single configured logalert instance
    """

    def __init__(self, options: Options | None = None, **kwargs: Any) -> None:
        """
        Build a client from the configuration

        :param options: A ready configuration, built from ``kwargs`` when it is
            not given
        :param kwargs: Option fields, passed on to :class:`Options`

        :return None:
        """

        self.options = options or Options(**kwargs)
        self.scope = Scope(max_breadcrumbs=self.options.breadcrumbs)
        self._redis_client: Any = None
        self._sinks = build_sinks(self.options)

        if not self._sinks:
            raise InvalidOption(
                'no sink configured: pass telegram=..., matrix=... '
                "or sinks=[...]. If all you want is output to a stream, use sinks=['stderr']"
            )

        for sink in self._sinks:
            bind_options(sink, self.options)

        self._deliverer = Deliverer(self._sinks, self.options)
        self._transport: Transport = self._build_transport()
        self._dedupe = build_dedupe(
            window=self.options.dedup_window,
            maxsize=self.options.dedup_cache_size,
            redis_client=self._redis_client,
            project=self.options.project,
        )

        self._random = random.Random()
        self._closed = False

        if self.options.server_name is None:
            self.options.server_name = _hostname()

        if self.options.debug:
            _enable_debug_logging()

    def capture(
        self,
        exc: BaseException | None = None,
        *,
        message: str | None = None,
        source: str = 'manual',
        mechanism: str = 'manual',
        level: str | None = None,
        logger_name: str = '',
    ) -> str | None:
        """
        Capture an event

        :param exc: The exception to capture
        :param message: The event text, required when there is no exception
        :param source: Where the event came from - one of the ``EventSource``
            values
        :param mechanism: What produced the event
        :param level: The level name - ``error`` when it is not given
        :param logger_name: The logger the event is attributed to

        :return str | None:
        """

        if self._closed:
            return None

        with capture_guard() as allowed:
            if not allowed:
                return None

            try:
                return self._capture_inner(
                    exc,
                    message=message,
                    source=source,
                    mechanism=mechanism,
                    level=level,
                    logger_name=logger_name,
                )

            except Exception:
                _log.warning('failed to process the event', exc_info=True)

                return None

    def _capture_inner(
        self,
        exc: BaseException | None,
        *,
        message: str | None,
        source: str,
        mechanism: str,
        level: str | None,
        logger_name: str,
    ) -> str | None:
        """
        Build the event and run it through the pipeline

        :param exc: The exception to capture, when there is one
        :param message: The event text, used when there is no exception
        :param source: Where the event came from - one of the ``EventSource``
            values
        :param mechanism: What produced the event
        :param level: The level name - ``error`` when it is not given
        :param logger_name: The logger the event is attributed to

        :return str | None:
        """

        if message is None and exc is None:
            return None

        if exc is not None and should_ignore(exc, self.options):
            return None

        if exc is not None:
            alert = alert_from_exception(
                exc,
                self.options,
                source=source,
                mechanism=mechanism,
                level=level or 'error',
                logger_name=logger_name or _logger_from_exception(exc),
                message=message or '',
            )

        else:
            alert = Alert(
                event_id=new_event_id(),
                timestamp=datetime.now(UTC),
                level=(level or 'error'),  # type: ignore[arg-type]
                source=source,  # type: ignore[arg-type]
                logger=logger_name or 'manual',
                message=message or '',
                mechanism=mechanism,
                handled=True,
            )

        alert = self._attach_scope(alert)

        if self.options.filter is not None and not self._options_filter(alert):
            return None

        if self.options.enrich is not None:
            alert = self._options_enrich(alert)

        alert = _privacy.scrub(alert, self.options)
        digest = compute_fingerprint(alert, self.options)
        verdict = self._dedupe.check(digest)

        if not verdict.send:
            return None

        if verdict.occurrences > 1:
            alert = replace(alert, count=verdict.occurrences)

        if self.options.sample_rate < 1.0 and self._random.random() >= self.options.sample_rate:
            self._count_drop('sampled_out')

            return None

        self._transport.submit(alert)

        return alert.event_id

    def _attach_scope(self, alert: Alert) -> Alert:
        """
        Fold the scope into the event

        :param alert: The event the scope is folded into

        :return Alert:
        """

        return replace(
            alert,
            tags={**self.scope.tags(), **alert.tags},
            contexts={**self.scope.contexts(), **alert.contexts},
            user=alert.user or self.scope.user(),
            breadcrumbs=self.scope.breadcrumbs.snapshot(),
            fingerprint=alert.fingerprint or self.scope.fingerprint(),
            environment=self.options.environment,
            release=self.options.release,
            server_name=self.options.server_name,
        )

    def _options_filter(self, alert: Alert) -> bool:
        """
        Ask the user's ``filter`` hook about the event

        :param alert: The event the hook is asked about

        :return bool:
        """

        hook = self.options.filter

        if hook is None:
            return True

        try:
            return bool(hook(alert))

        except Exception:
            _log.warning('the filter hook blew up, passing the event through', exc_info=True)

            return True

    def _options_enrich(self, alert: Alert) -> Alert:
        """
        Let the user's ``enrich`` hook add to the event

        :param alert: The event the hook is given

        :return Alert:
        """

        hook = self.options.enrich

        if hook is None:
            return alert

        try:
            result = hook(alert)

            return result if isinstance(result, Alert) else alert

        except Exception:
            _log.warning('the enrich hook blew up, keeping the event as is', exc_info=True)

            return alert

    def _count_drop(self, reason: str) -> None:
        """
        Record a dropped event on the queue of the transport

        :param reason: The reason the event was dropped

        :return None:
        """

        counter = getattr(self._transport, '_queue', None)

        if counter is not None:
            with contextlib.suppress(Exception):
                counter.count(reason)

    def _build_transport(self) -> Transport:
        """
        Build the transport the events are handed to

        :return Transport:
        """

        if not self.options.redis_url:
            return DirectTransport(self._deliverer, self.options)

        from .transport.redis import RedisStreamTransport, build_client

        self._redis_client = build_client(self.options.redis_url)

        return RedisStreamTransport(
            self._redis_client,
            self.options.project or 'default',
            self.options,
            fallback=self._deliverer,
        )

    def flush(self, timeout: float | None = None) -> bool:
        """
        Wait for the queued events to go out

        :param timeout: How long the flush may take, ``shutdown_timeout`` when
            omitted

        :return bool:
        """

        budget = self.options.shutdown_timeout if timeout is None else timeout

        return self._transport.flush(budget)

    def close(self) -> None:
        """
        Close the transport and the dedup cache

        :return None:
        """

        if self._closed:
            return

        self._closed = True

        with contextlib.suppress(Exception):
            self._transport.close()

        with contextlib.suppress(Exception):
            self._dedupe.close()

    def restart_after_fork(self) -> None:
        """
        Bring the transport back up in the child process after ``fork()``

        :return None:
        """

        restart = getattr(self._transport, 'restart_after_fork', None)

        if callable(restart):
            restart()

    @property
    def sinks(self) -> Sequence[Any]:
        """
        The sinks this client delivers through

        :return Sequence[Any]:
        """

        return self._deliverer.sinks

    def __repr__(self) -> str:
        """
        The sink names and the options, with the secrets in them masked

        :return str:
        """

        names = ', '.join(self._deliverer.sink_names())

        return f'Client(sinks=[{names}], {self.options!r})'

    def add_breadcrumb(self, message: str, **kwargs: Any) -> None:
        """
        Record an event in the buffer that goes out with the next alert

        :param message: The breadcrumb message
        :param kwargs: Extra breadcrumb fields, passed on to
            :meth:`Scope.add_breadcrumb`

        :return None:
        """

        self.scope.add_breadcrumb(message, max_length=self.options.max_breadcrumb_length, **kwargs)


def build_sinks(options: Options) -> list[Any]:
    """
    Build the sinks from the configuration

    :param options: The configuration to build the sinks from

    :return list[Any]:
    """

    from .sinks import StderrSink, _build_matrix, _build_telegram

    result: list[Any] = []

    for entry in options.sinks:
        if isinstance(entry, str):
            raise InvalidOption(
                f'sinks takes sink objects, not names: got {entry!r}. Build the sink '
                f'explicitly instead, as in sinks=[TelegramSink(token=..., chat_id=...)]'
            )

        result.append(entry)

    seen = {getattr(sink, 'name', '') for sink in result}

    if options.telegram and 'telegram' not in seen:
        result.append(_build_telegram(dict(options.telegram)))

    if options.matrix and 'matrix' not in seen:
        result.append(_build_matrix(dict(options.matrix)))

    if options.debug and 'stderr' not in seen:
        result.append(StderrSink())

    return result


def _logger_from_exception(exc: BaseException) -> str:
    """
    A logger name for an exception caught by a hook

    :param exc: The exception the name is built from

    :return str:
    """

    module = getattr(type(exc), '__module__', '') or ''

    return f'{module}.{type(exc).__qualname__}' if module else type(exc).__qualname__


def _hostname() -> str:
    """
    The host name

    :return str:
    """

    import socket

    try:
        return socket.gethostname()
    except Exception:
        return 'unknown'


def _enable_debug_logging() -> None:
    """
    Show what is going on inside the library

    :return None:
    """

    logger = logging.getLogger('logalert.debug')
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('[logalert] %(levelname)s %(message)s'))
        logger.addHandler(handler)


__all__ = (
    'build_sinks',
    'Client',
)
