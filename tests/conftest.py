"""
Logalert Tests Conftest

Shared fixtures, plus the reset of the global state the library deliberately
keeps.

:copyright: (c) 2025 Aiko Sora
"""

import logging
from collections.abc import Sequence
from typing import Any, ClassVar

import pytest

import logalert
from logalert.exceptions import SinkPermanentError, SinkRateLimited, SinkTransientError
from logalert.types import Alert


class RecordingSink:
    """
    A sink that records everything into a list, the basis of most tests
    """

    name: ClassVar[str] = 'recording'
    idempotent: ClassVar[bool] = True

    def __init__(
        self,
        *,
        fail_with: BaseException | None = None,
        delay: float = 0.0,
    ) -> None:
        """
        Prepare an empty record of what the sink will be given

        :param fail_with: The error to raise instead of recording the alert
        :param delay: How long every send sleeps before it does anything

        :return None:
        """

        self.sent: list[Alert] = []
        self.keys: list[str] = []
        self.closed = False

        self._fail_with = fail_with
        self._delay = delay

    def send(self, alert: Alert, *, key: str, timeout: float) -> None:
        """
        Record the alert, or fail the way the test asked for

        :param alert: The alert being delivered
        :param key: The idempotency key of the delivery
        :param timeout: How long the send is allowed to take

        :return None:
        """

        if self._delay:
            import time

            time.sleep(self._delay)

        if self._fail_with is not None:
            raise self._fail_with

        self.sent.append(alert)
        self.keys.append(key)

    def healthcheck(self) -> None:
        """
        Report the sink as healthy

        :return None:
        """

        return None

    def close(self) -> None:
        """
        Mark the sink as closed

        :return None:
        """

        self.closed = True


@pytest.fixture
def sink() -> RecordingSink:
    """
    A recording sink, fresh for every test

    :return RecordingSink:
    """

    return RecordingSink()


@pytest.fixture(autouse=True)
def _clean_global_state() -> Any:
    """
    Tear the global state down before and after every test

    :return Any:
    """

    root = logging.getLogger()
    before = list(root.handlers)
    logalert.shutdown()

    yield

    logalert.shutdown()

    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)


@pytest.fixture
def make_client(sink: RecordingSink) -> Any:
    """
    Build a client around a recording sink, bypassing the global state

    :param sink: The sink every client built here will use

    :return Any:
    """

    from logalert.client import Client
    from logalert.options import Options

    created: list[Client] = []

    def factory(**kwargs: Any) -> Client:
        kwargs.setdefault('sinks', [sink])

        if isinstance(kwargs['sinks'], Sequence) and sink not in kwargs['sinks']:
            kwargs['sinks'] = [sink, *kwargs['sinks']]

        client = Client(Options(**kwargs))

        created.append(client)

        return client

    yield factory

    for client in created:
        client.close()


def make_alert(**overrides: Any) -> Alert:
    """
    An event with sensible defaults

    :param overrides: The fields that replace the defaults

    :return Alert:
    """

    from datetime import UTC, datetime

    from logalert.types import new_event_id

    base: dict[str, Any] = {
        'event_id': new_event_id(),
        'timestamp': datetime.now(UTC),
        'level': 'error',
        'source': 'logging',
        'logger': 'tests',
        'message': 'something broke',
        'environment': 'test',
    }

    base.update(overrides)

    return Alert(**base)


__all__ = (
    'SinkPermanentError',
    'SinkTransientError',
    'SinkRateLimited',
    'RecordingSink',
    'make_client',
    'make_alert',
    'sink',
)
