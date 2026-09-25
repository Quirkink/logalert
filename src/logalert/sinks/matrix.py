"""
Logalert Sinks Matrix

Matrix sink over the Client-Server REST API

:copyright: (c) 2025 Aiko Sora
"""

import contextlib
import threading
import time
from typing import Any, ClassVar

from ..exceptions import (
    EncryptedRoomError,
    InvalidOption,
    SinkPermanentError,
    SinkRateLimited,
    SinkTransientError,
)
from ..options import Options
from ..render import render_matrix
from ..types import Alert
from .base import require

DEFAULT_RETRY_AFTER = 5.0
DEFAULT_MAX_RETRY_AFTER = 120.0


class MatrixSink:
    """
    Delivers alerts to a Matrix room as ``m.notice``
    """

    name: ClassVar[str] = 'matrix'
    idempotent: ClassVar[bool] = True

    def __init__(
        self,
        homeserver: str,
        room_id: str,
        access_token: str,
        *,
        timeout: float = 10.0,
        max_retry_after: float = DEFAULT_MAX_RETRY_AFTER,
        **_: Any,
    ) -> None:
        """
        Configure the sink for one room

        :param homeserver: Base URL of the homeserver
        :param room_id: Internal room id, the one starting with ``!``
        :param access_token: Access token of the account the alerts are sent
            from
        :param timeout: How long one HTTP request may take, in seconds
        :param max_retry_after: Longest pause a 429 may impose, in seconds
        :param _: Further keys in the config section, accepted and ignored

        :return None:
        """

        if not access_token:
            raise InvalidOption('matrix.access_token is required')

        if not room_id.startswith('!'):
            raise InvalidOption(
                f"matrix.room_id must start with '!', got {room_id!r}. "
                f"That looks like an alias: take the room's internal id from the settings"
            )

        self._homeserver = homeserver.rstrip('/')
        self._room_id = room_id
        self._token = access_token
        self._timeout = timeout
        self._max_retry_after = max_retry_after
        self._options = Options()
        self._http_client: Any = None
        self._lock = threading.Lock()
        self._gated_until = 0.0

    def bind(self, options: Options) -> None:
        """
        Accept the resolved options

        :param options: The resolved options, used when rendering an alert

        :return None:
        """

        self._options = options

    def send(self, alert: Alert, *, key: str, timeout: float) -> None:
        """
        Deliver an alert to the room

        :param alert: The event to deliver
        :param key: Delivery key, reused across retries
        :param timeout: How long the send may block

        :return None:
        """

        messages = render_matrix(alert, self._options)

        for index, message in enumerate(messages):
            txn_id = key if len(messages) == 1 else f'{key}.{index}'
            self._send_one(message, txn_id=txn_id, timeout=timeout)

    def _send_one(self, text: str, *, txn_id: str, timeout: float) -> None:
        """
        PUT one rendered message into the room under its transaction id

        :param text: The rendered message body
        :param txn_id: Transaction id, reused across retries
        :param timeout: How long the request may take

        :return None:
        """

        remaining = self._gated_until - time.monotonic()

        if remaining > 0:
            raise SinkRateLimited('Matrix is paused after M_LIMIT_EXCEEDED', retry_after=remaining)

        httpx = require('httpx', 'matrix')
        path = (
            f'/_matrix/client/v3/rooms/{_quote(self._room_id)}/send/m.room.message/{_quote(txn_id)}'
        )
        payload = {
            'msgtype': 'm.notice',
            'body': text,
            'format': 'org.matrix.custom.html',
            'formatted_body': text,
        }

        try:
            response = self._client(httpx).put(
                path,
                json=payload,
                headers={'Authorization': f'Bearer {self._token}'},
                timeout=timeout,
            )

        except httpx.HTTPError as exc:
            raise SinkTransientError(f'Matrix is unreachable: {exc}') from exc

        if response.status_code == 429:
            retry_after = _parse_retry_after(response)
            self._gated_until = time.monotonic() + min(retry_after, self._max_retry_after)

            raise SinkRateLimited('Matrix asks us to wait', retry_after=retry_after)

        if response.status_code >= 500:
            raise SinkTransientError(f'Matrix returned {response.status_code}')

        if response.status_code >= 400:
            raise SinkPermanentError(_describe(response))

    def _client(self, httpx: Any) -> Any:
        """
        The HTTP client, created on first use

        :param httpx: The imported httpx module

        :return Any:
        """

        with self._lock:
            if self._http_client is None:
                self._http_client = httpx.Client(base_url=self._homeserver)

            return self._http_client

    def is_encrypted(self) -> bool:
        """
        Whether the room is encrypted. Determined from ``m.room.encryption``
        state

        :return bool:
        """

        httpx = require('httpx', 'matrix')
        path = f'/_matrix/client/v3/rooms/{_quote(self._room_id)}/state/m.room.encryption/'

        try:
            response = self._client(httpx).get(
                path,
                headers={'Authorization': f'Bearer {self._token}'},
                timeout=self._timeout,
            )

        except httpx.HTTPError as exc:
            raise SinkTransientError(f'Matrix is unreachable: {exc}') from exc

        if response.status_code == 404:
            return False

        if response.status_code >= 400:
            raise SinkPermanentError(_describe(response))

        return True

    def healthcheck(self) -> None:
        """
        Check the token, access to the room, and that encryption is off

        :return None:
        """

        httpx = require('httpx', 'matrix')

        try:
            whoami = self._client(httpx).get(
                '/_matrix/client/v3/account/whoami',
                headers={'Authorization': f'Bearer {self._token}'},
                timeout=self._timeout,
            )

        except httpx.HTTPError as exc:
            raise SinkTransientError(f'Matrix is unreachable: {exc}') from exc

        if whoami.status_code >= 400:
            raise SinkPermanentError(f'token rejected: {_describe(whoami)}')

        if self.is_encrypted():
            raise EncryptedRoomError(
                f'room {self._room_id} is encrypted, while this sink can only send to '
                f'unencrypted rooms. Either create a separate unencrypted room for alerts, '
                f'or turn on the sink that handles encrypted rooms: '
                f'matrix={{..., "e2ee": True, "user_id": ..., "device_id": ..., '
                f'"store_path": ...}} (requires pip install logalert[matrix-e2ee])'
            )

    def close(self) -> None:
        """
        Close the HTTP client

        :return None:
        """

        with self._lock:
            if self._http_client is not None:
                with contextlib.suppress(Exception):
                    self._http_client.close()

                self._http_client = None


def _quote(value: str) -> str:
    """
    Percent-encode a path segment

    :param value: The room id or transaction id to encode

    :return str:
    """

    from urllib.parse import quote

    return quote(value, safe='')


def _parse_retry_after(response: Any) -> float:
    """
    How long Matrix says to wait

    :param response: The response that carried the 429

    :return float:
    """

    try:
        body = response.json()
        value = body.get('retry_after_ms')

        if isinstance(value, (int, float)) and value > 0:
            return float(value) / 1000.0

    except Exception:
        pass

    return DEFAULT_RETRY_AFTER


def _describe(response: Any) -> str:
    """
    The error text of a response, for the exception messages

    :param response: The response that carried the error

    :return str:
    """

    try:
        body = response.json()
        errcode = body.get('errcode')
        error = body.get('error')

        if errcode or error:
            return f'{response.status_code}: {errcode or ""} {error or ""}'.strip()

    except Exception:
        pass

    return f'HTTP {response.status_code}'


__all__ = ('MatrixSink',)
