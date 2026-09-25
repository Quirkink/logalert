"""
Logalert Sinks Stderr

The stderr sink

:copyright: (c) 2025 Aiko Sora
"""

import sys
from typing import Any, ClassVar, TextIO

from ..options import Options
from ..render import render_text
from ..types import Alert


class StderrSink:
    """
    Prints the alert to a stream. Sends nothing and cannot fail
    """

    name: ClassVar[str] = 'stderr'
    idempotent: ClassVar[bool] = True

    def __init__(self, stream: TextIO | None = None, **_: Any) -> None:
        """
        Configure the sink

        :param stream: Stream the alerts are written to; ``sys.stderr`` when
            omitted
        :param _: Further keys in the config section, accepted and ignored

        :return None:
        """

        self._stream = stream
        self._options = Options()

    def bind(self, options: Options) -> None:
        """
        Accept the resolved options

        :param options: The resolved options, used when rendering an alert

        :return None:
        """

        self._options = options

    def send(self, alert: Alert, *, key: str, timeout: float) -> None:
        """
        Print the alert to the stream, one chunk at a time

        :param alert: The event to print
        :param key: Delivery key, unused by this sink
        :param timeout: Unused: writing to a stream does not block

        :return None:
        """

        stream = self._stream if self._stream is not None else sys.stderr

        for chunk in render_text(alert, self._options):
            print(chunk, file=stream)
            print('-' * 40, file=stream)

        stream.flush()

    def healthcheck(self) -> None:
        """
        Do nothing: there is nothing here that can be unreachable

        :return None:
        """

        return None

    def close(self) -> None:
        """
        Do nothing: the stream belongs to the caller and is left open

        :return None:
        """

        return None


__all__ = ('StderrSink',)
