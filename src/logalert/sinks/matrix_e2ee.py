"""
Logalert Sinks Matrix E2EE

Matrix sink for an encrypted room

:copyright: (c) 2025 Aiko Sora
"""

import asyncio
import contextlib
import logging
import os
import secrets
import threading
from typing import Any, ClassVar

from ..exceptions import (
    EncryptedRoomError,
    InvalidOption,
    LogAlertError,
    SinkError,
    SinkPermanentError,
    SinkRateLimited,
    SinkTransientError,
)
from ..options import Options
from ..render import render_matrix
from ..types import Alert
from .base import require

_log = logging.getLogger('logalert.debug')

DEFAULT_RETRY_AFTER = 5.0
DEFAULT_MAX_RETRY_AFTER = 120.0
DEFAULT_START_TIMEOUT = 15.0
_PICKLE_KEY_FILE = '.pickle_key'


class _LoopThread:
    """
    A private event loop on a thread of its own
    """

    def __init__(self) -> None:
        """
        Start the event loop on a daemon thread of its own

        :return None:
        """

        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name='logalert-matrix', daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """
        Make the loop current for this thread, signal readiness and run it
        forever

        :return None:
        """

        asyncio.set_event_loop(self._loop)

        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    def wait_ready(self, timeout: float) -> bool:
        """
        Wait until the loop is running

        :param timeout: How long to wait, in seconds

        :return bool:
        """

        return self._ready.wait(timeout)

    def submit(self, coro: Any, timeout: float) -> Any:
        """
        Run a coroutine on the private loop and wait for the result

        :param coro: The coroutine to run on the private loop
        :param timeout: How long to wait for the result

        :return Any:
        """

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        return future.result(timeout)

    def spawn(self, coro: Any) -> None:
        """
        Start a coroutine in the background without waiting for it

        :param coro: The coroutine to start

        :return None:
        """

        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self) -> None:
        """
        Stop the loop and wait for its thread to finish

        :return None:
        """

        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)


class MatrixE2ESink:
    """
    Delivers alerts to an encrypted Matrix room
    """

    name: ClassVar[str] = 'matrix'
    idempotent: ClassVar[bool] = True

    def __init__(
        self,
        homeserver: str,
        room_id: str,
        access_token: str,
        *,
        user_id: str,
        device_id: str,
        store_path: str,
        pickle_key: str | None = None,
        require_encryption: bool = True,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        max_retry_after: float = DEFAULT_MAX_RETRY_AFTER,
        **_: Any,
    ) -> None:
        """
        Configure the sink and bring the encrypted client up

        :param homeserver: Base URL of the homeserver
        :param room_id: Internal room id, the one starting with ``!``
        :param access_token: Access token of the bot account
        :param user_id: Matrix id of the bot, ``@bot:server``
        :param device_id: Device id whose encryption keys are used
        :param store_path: Directory holding the Matrix store and its keys
        :param pickle_key: Key encrypting the store; generated into it when
            omitted
        :param require_encryption: Refuse to start when the room is not
            encrypted
        :param start_timeout: How long to wait for the first sync, in seconds
        :param max_retry_after: Longest pause a 429 may impose, in seconds
        :param _: Further keys in the config section, accepted and ignored

        :return None:
        """

        if not user_id.startswith('@'):
            raise InvalidOption(f'matrix.user_id must look like @bot:server, got {user_id!r}')

        if not device_id:
            raise InvalidOption(
                'matrix.device_id is mandatory: the device id is tied to the encryption '
                'keys, and changing it makes the whole history unreadable'
            )

        if not store_path:
            raise InvalidOption(
                'matrix.store_path is mandatory: the Megolm keys live there, and without it '
                'the encrypted conversation will not survive a restart'
            )

        self._homeserver = homeserver.rstrip('/')
        self._room_id = room_id
        self._token = access_token
        self._user_id = user_id
        self._device_id = device_id
        self._store_path = store_path
        self._require_encryption = require_encryption
        self._start_timeout = start_timeout
        self._max_retry_after = max_retry_after
        self._pickle_key = pickle_key or _load_or_create_pickle_key(store_path)
        self._options = Options()
        self._client: Any = None
        self._loop: _LoopThread | None = _LoopThread()
        self._sync_task: Any = None
        self._first_sync: Any = None
        self._started = False
        self._room_is_encrypted: bool | None = None
        self._start()

    def bind(self, options: Options) -> None:
        """
        Accept the resolved options

        :param options: The resolved options, used when rendering an alert

        :return None:
        """

        self._options = options

    def _require_loop(self) -> _LoopThread:
        """
        The event loop, provided the sink is still alive

        :return _LoopThread:
        """

        loop = self._loop

        if loop is None:
            raise SinkTransientError('the Matrix E2EE sink is already closed')

        return loop

    def _start(self) -> None:
        """
        Bring the client up, wait for the first sync, and check encryption

        :return None:
        """

        loop = self._require_loop()

        if not loop.wait_ready(self._start_timeout):
            raise LogAlertError('failed to start the event loop for Matrix')

        os.makedirs(self._store_path, exist_ok=True)

        try:
            loop.submit(self._connect(), self._start_timeout + 5.0)

        except TimeoutError as exc:
            # The outer timeout can win the race against the one inside
            # `_connect` when the event loop is busy with the client's own
            # retries. On its own that would surface as a bare `TimeoutError`
            # with no message, which says nothing about what to check.
            self.close()

            raise LogAlertError(
                f'Matrix did not answer within {self._start_timeout + 5.0:g} s. Check that '
                f'{self._homeserver} is reachable and the token is still valid'
            ) from exc

        except Exception:
            self.close()

            raise

    async def _connect(self) -> None:
        """
        Build the encrypted client and wait until it is actually usable

        :return None:
        """

        nio = require('nio', 'matrix-e2ee')

        AsyncClient = nio.AsyncClient
        AsyncClientConfig = nio.AsyncClientConfig

        SyncResponse = nio.SyncResponse
        SqliteStore = require('nio.store.database', 'matrix-e2ee').SqliteStore

        self._client = AsyncClient(
            self._homeserver,
            self._user_id,
            device_id=self._device_id,
            store_path=self._store_path,
            config=AsyncClientConfig(
                store=SqliteStore,
                encryption_enabled=True,
                pickle_key=self._pickle_key,
                store_sync_tokens=True,
            ),
        )

        self._client.restore_login(self._user_id, self._device_id, self._token)
        self._first_sync = asyncio.Event()
        self._client.add_response_callback(self._on_sync, SyncResponse)
        self._sync_task = asyncio.ensure_future(
            self._client.sync_forever(timeout=30_000, loop_sleep_time=500)
        )

        try:
            await asyncio.wait_for(self._first_sync.wait(), self._start_timeout)

        except TimeoutError as exc:
            raise LogAlertError(
                f'Matrix did not sync within {self._start_timeout:g} s. '
                f'Check that {self._homeserver} is reachable and that the token is still valid'
            ) from exc

        if getattr(self._client, 'olm', None) is None:
            raise LogAlertError(
                'the E2EE machine is not loaded (olm=None): matrix-nio is installed without '
                "the 'e2e' extra. Install it with: pip install logalert[matrix-e2ee]"
            )

        self._room_is_encrypted = await self._check_encryption()

        if self._require_encryption and not self._room_is_encrypted:
            raise EncryptedRoomError(
                f'room {self._room_id} is not encrypted, while the sink for encrypted rooms '
                f'is the one selected. Messages would go out as plaintext this way. Either '
                f'turn on encryption in the room, or use the plain matrix sink without e2ee'
            )

        self._started = True

    def _on_sync(self, response: Any) -> None:
        """
        Release the wait the constructor is sitting in

        :param response: The sync response nio just handled

        :return None:
        """

        self._first_sync.set()

    def _ensure_in_room(self) -> None:
        """
        Make sure the bot is in the room at all

        :return None:
        """

        if self._room_id not in self._client.rooms:
            raise LogAlertError(
                f'bot {self._user_id} is not in room {self._room_id} (or the room has not '
                f'appeared in the sync yet). Invite the bot and make sure it accepted the '
                f'invitation'
            )

    async def _check_encryption(self) -> bool:
        """
        Whether the room is encrypted. Determined from the ``m.room.encryption``
        state

        :return bool:
        """

        self._ensure_in_room()

        return bool(self._client.rooms[self._room_id].encrypted)

    def send(self, alert: Alert, *, key: str, timeout: float) -> None:
        """
        Deliver an alert to the encrypted room

        :param alert: The event to deliver
        :param key: Delivery key, reused across retries
        :param timeout: How long the send may block

        :return None:
        """

        if not self._started:
            raise SinkTransientError('the Matrix E2EE sink is not ready yet')

        messages = render_matrix(alert, self._options)

        for index, message in enumerate(messages):
            tx_id = key if len(messages) == 1 else f'{key}.{index}'
            self._send_one(message, tx_id=tx_id, timeout=timeout)

    def _send_one(self, text: str, *, tx_id: str, timeout: float) -> None:
        """
        Send one rendered message on the private loop and read what nio reported

        :param text: The rendered message body
        :param tx_id: Transaction id, reused across retries
        :param timeout: How long the send may block

        :return None:
        """

        try:
            response = self._require_loop().submit(
                self._room_send(text, tx_id), timeout=max(timeout, 1.0)
            )

        except TimeoutError as exc:
            raise SinkTransientError(f'Matrix did not answer within {timeout:g} s') from exc

        except SinkError:
            raise

        except Exception as exc:
            raise SinkTransientError(f'Matrix send failed: {exc!r}') from exc

        raise_for_response(response)

    async def _room_send(self, text: str, tx_id: str) -> Any:
        """
        Hand one message to nio for the room

        :param text: The rendered message body
        :param tx_id: Transaction id, reused across retries

        :return Any:
        """

        return await self._client.room_send(
            self._room_id,
            'm.room.message',
            {
                'msgtype': 'm.notice',
                'body': text,
                'format': 'org.matrix.custom.html',
                'formatted_body': text,
            },
            tx_id=tx_id,
            ignore_unverified_devices=True,
        )

    def healthcheck(self) -> None:
        """
        Check the sink without sending anything

        :return None:
        """

        if not self._started:
            raise SinkTransientError('the sink is not running')

        if getattr(self._client, 'olm', None) is None:
            raise LogAlertError('the E2EE machine is not loaded (olm=None)')

        if self._require_encryption and not self._room_is_encrypted:
            raise EncryptedRoomError(f'room {self._room_id} is not encrypted')

    def close(self) -> None:
        """
        Stop syncing and close the store

        :return None:
        """

        loop = getattr(self, '_loop', None)

        if loop is None:
            return

        async def shutdown() -> None:
            if self._sync_task is not None:
                self._sync_task.cancel()

            if self._client is not None:
                await self._client.close()

        with contextlib.suppress(Exception):
            if self._client is not None:
                loop.submit(shutdown(), 5.0)

        with contextlib.suppress(Exception):
            loop.stop()

        self._loop = None


def raise_for_response(response: Any) -> None:
    """
    A nio response - our exception hierarchy

    :param response: The nio response to translate

    :return None:
    """

    if not hasattr(response, 'status_code'):
        return

    status = getattr(response, 'status_code', None)
    message = getattr(response, 'message', '') or type(response).__name__

    if status == 429:
        retry_after = _retry_after_seconds(response) or DEFAULT_RETRY_AFTER

        raise SinkRateLimited('Matrix asks us to wait', retry_after=retry_after)

    if status is not None and status >= 500:
        raise SinkTransientError(f'Matrix returned {status}: {message}')

    raise SinkPermanentError(f'Matrix rejected the message: {message}')


def _retry_after_seconds(response: Any) -> float | None:
    """
    How long Matrix asks us to wait

    :param response: The response that carried the 429

    :return float | None:
    """

    for attribute, divisor in (('retry_after_ms', 1000.0), ('retry_after', 1.0)):
        value = getattr(response, attribute, None)

        if isinstance(value, (int, float)) and value > 0:
            return float(value) / divisor

    return None


def _load_or_create_pickle_key(store_path: str) -> str:
    """
    The store's encryption key - next to the store itself, mode 0600

    :param store_path: Directory holding the Matrix store

    :return str:
    """

    os.makedirs(store_path, exist_ok=True)
    path = os.path.join(store_path, _PICKLE_KEY_FILE)

    if os.path.exists(path):
        with open(path, encoding='utf-8') as handle:
            stored = handle.read().strip()

        if stored:
            return stored

    key = secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)

    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write(key)

    _log.debug('created the Matrix store encryption key: %s', path)

    return key


__all__ = ('MatrixE2ESink',)
