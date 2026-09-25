"""
Logalert Example Custom Sink

A sink of your own, handed to ``init`` as an object

:copyright: (c) 2025 Aiko Sora
"""

import json
import logging
import os
from typing import ClassVar

import logalert
from logalert import Alert

log = logging.getLogger('billing.worker')


class FileSink:
    """
    Appends every alert to a file, one JSON object per line
    """

    name: ClassVar[str] = 'file'

    # False, and not out of caution: appending is not idempotent. A retry after
    # a failed send would leave a second copy of the same event in the file, and
    # the consumer treats a sink that claims idempotency as one it may call
    # twice.
    idempotent: ClassVar[bool] = False

    def __init__(self, path: str) -> None:
        """
        Remember where the alerts go

        :param path: The file the alerts are appended to

        :return None:
        """

        self._path = path

    def send(self, alert: Alert, *, key: str, timeout: float) -> None:
        """
        Append one alert to the file

        :param alert: The event to deliver
        :param key: Delivery key, reused across retries
        :param timeout: How long the send may block

        :return None:
        """

        with open(self._path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(alert.to_dict(), default=str) + '\n')

    def healthcheck(self) -> None:
        """
        Check the directory is writable before the first alert needs it

        :return None:
        """

        directory = os.path.dirname(self._path) or '.'

        if not os.access(directory, os.W_OK):
            raise RuntimeError(f'{directory} is not writable')

    def close(self) -> None:
        """
        Nothing to release

        :return None:
        """

        return None


# The sink is passed ready-made, carrying its own configuration. There is no
# registry to put it in and no name to refer to it by: the library could only
# look a name up in its own options, which have no field for a sink that is not
# one of its own.
logalert.init(
    sinks=[FileSink('alerts.jsonl')],
    environment='production',
    release='billing@1.2.3',
    level='ERROR',
)


def charge(order_id: int) -> None:
    """
    Fail at something, for the sake of the example

    :param order_id: The order being charged

    :return None:
    """

    raise ConnectionError(f'payment gateway is not answering for order {order_id}')


def main() -> None:
    """
    Report one caught error and one that nobody catches

    :return None:
    """

    try:
        charge(4217)

    except ConnectionError:
        log.exception('failed to charge the order')

    logalert.flush(timeout=5.0)

    raise RuntimeError('the worker ran out of retries and gave up')


if __name__ == '__main__':
    main()
