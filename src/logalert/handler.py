"""
Logalert Handler

Logging handler - the primary capture path

:copyright: (c) 2025 Aiko Sora
"""

import logging
from collections.abc import Mapping
from typing import Any

from . import _state
from ._guard import is_origin_denied
from .client import Client


class AlertHandler(logging.Handler):
    """
    Sends log records at ``level`` and above to logalert
    """

    def __init__(
        self,
        level: int | str = logging.ERROR,
        *,
        client: Client | None = None,
        **unknown: Any,
    ) -> None:
        """
        Make a handler that sends records at ``level`` and above

        :param level: Level the handler is interested in
        :param client: The client to send to, the current one by default
        :param unknown: Parameters the handler does not know; any of them is an
            error

        :return None:
        """

        super().__init__(level=level)

        if unknown:
            raise TypeError(
                f'AlertHandler does not know these parameters: {", ".join(sorted(unknown))}'
            )

        self._client = client
        self.errors = 0
        self.captured = 0

    def _resolve(self) -> Client | None:
        """
        The client to send to: the one given to the constructor, else the
        current one

        :return Client | None:
        """

        return self._client or _state.get_client()

    def emit(self, record: logging.LogRecord) -> None:
        """
        Send a log record to the client

        :param record: The record to send

        :return None:
        """

        try:
            if is_origin_denied(record.name):
                return

            if record.levelno < self.level:
                return

            client = self._resolve()

            if client is None:
                return

            options = client.options
            exc = record.exc_info[1] if record.exc_info else None
            message = record.getMessage()

            client.capture(
                exc,
                message=message,
                source='logging',
                mechanism='logging',
                level=options.level_name(record.levelno),
                logger_name=record.name,
            )

            self.captured += 1

        except Exception:
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:
        """
        Never print, never raise

        :param record: The record that could not be handled

        :return None:
        """

        self.errors += 1


class BreadcrumbHandler(logging.Handler):
    """
    Buffers log records in a ring so you can see what came before an error
    """

    def __init__(
        self,
        level: int | str = logging.INFO,
        *,
        client: Client | None = None,
        category: str = 'log',
        **unknown: Any,
    ) -> None:
        """
        Make a handler that buffers records in a ring

        :param level: Level the handler is interested in
        :param client: The client to send to, the current one by default
        :param category: Category the buffered events are stored under
        :param unknown: Parameters the handler does not know; any of them is an
            error

        :return None:
        """

        super().__init__(level=level)

        if unknown:
            raise TypeError(
                f'BreadcrumbHandler does not know these parameters: {", ".join(sorted(unknown))}'
            )

        self._client = client
        self._category = category

    def _resolve(self) -> Client | None:
        """
        The client to store into: the one given to the constructor, else the
        current one

        :return Client | None:
        """

        return self._client or _state.get_client()

    def emit(self, record: logging.LogRecord) -> None:
        """
        Store a log record in the client's breadcrumb buffer

        :param record: The record to store

        :return None:
        """

        try:
            if is_origin_denied(record.name):
                return

            if record.levelno < self.level:
                return

            client = self._resolve()

            if client is None or not client.scope.breadcrumbs.enabled:
                return

            data: dict[str, Any] = {'logger': record.name}

            if record.exc_info:
                data['has_exception'] = True

            if extra := getattr(record, 'ctx', None):
                data['ctx'] = extra if isinstance(extra, Mapping) else str(extra)

            client.add_breadcrumb(
                record.getMessage(),
                category=self._category,
                level=client.options.level_name(record.levelno),
                data=data,
                origin='auto',
            )

        except Exception:
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:
        """
        Never print, never raise

        :param record: The record that could not be handled

        :return None:
        """

        return None


__all__ = (
    'BreadcrumbHandler',
    'AlertHandler',
)
