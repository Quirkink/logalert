"""
Logalert State

The global current client

:copyright: (c) 2025 Aiko Sora
"""

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import Client


_lock = threading.Lock()
_client: 'Client | None' = None


def get_client() -> 'Client | None':
    """
    The client that is currently installed, if any

    :return Client | None:
    """

    return _client


def set_client(client: 'Client | None') -> 'Client | None':
    """
    Replace the current client and return the previous one

    :param client: Client to install, or None to clear the global one

    :return Client | None:
    """

    global _client

    with _lock:
        previous = _client
        _client = client

    return previous


def is_initialized() -> bool:
    """
    Whether a client has been installed

    :return bool:
    """

    return _client is not None


__all__ = (
    'is_initialized',
    'get_client',
    'set_client',
)
