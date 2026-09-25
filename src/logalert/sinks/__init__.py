"""
Logalert Sinks

Where alerts get delivered

:copyright: (c) 2025 Aiko Sora
"""

from typing import Any

from .base import (
    Sink,
    bind_options,
    classify_http_status,
    require,
)
from .matrix import MatrixSink
from .stderr import StderrSink
from .telegram import TelegramSink


def _build_telegram(config: dict[str, Any]) -> Sink:
    """
    The Telegram sink, built from the config section

    :param config: The ``telegram`` section of the options

    :return Sink:
    """

    return TelegramSink(**config)


def _build_matrix(config: dict[str, Any]) -> Sink:
    """
    The Matrix sink: the plain one, or the one for an encrypted room

    :param config: The ``matrix`` section of the options

    :return Sink:
    """

    if config.get('e2ee'):
        from .matrix_e2ee import MatrixE2ESink

        return MatrixE2ESink(**{k: v for k, v in config.items() if k != 'e2ee'})

    return MatrixSink(**config)


def _build_stderr(config: dict[str, Any]) -> Sink:
    """
    The stderr sink, built from the config section

    :param config: The ``stderr`` section of the options

    :return Sink:
    """

    return StderrSink(**config)


__all__ = (
    'classify_http_status',
    'TelegramSink',
    'bind_options',
    'MatrixSink',
    'StderrSink',
    'require',
    'Sink',
)
