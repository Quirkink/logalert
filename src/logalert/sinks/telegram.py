"""
Logalert Sinks Telegram

Telegram sink over the Bot API

:copyright: (c) 2025 Aiko Sora
"""

import contextlib
import logging
import threading
import time
from typing import Any, ClassVar

from .._privacy import REDACTED
from ..exceptions import SinkPermanentError, SinkRateLimited, SinkTransientError
from ..options import Options
from ..render import render_telegram
from ..types import Alert
from .base import require

_log = logging.getLogger('logalert.debug')

DEFAULT_MIN_INTERVAL = 1.05
DEFAULT_API_BASE = 'https://api.telegram.org'
DEFAULT_MAX_RETRY_AFTER = 120.0
DEFAULT_RETRY_AFTER = 5.0


class _TokenRedactingFilter(logging.Filter):
    """
    Strips the bot token out of httpx log records
    """

    def __init__(self, token: str) -> None:
        """
        Remember the token that has to stay out of the log records

        :param token: The bot token to redact

        :return None:
        """

        super().__init__()

        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        """
        Rewrite a record that carries the token, and let every record through

        :param record: The log record about to be emitted

        :return bool:
        """

        try:
            if self._token and self._token in record.getMessage():
                record.msg = record.getMessage().replace(self._token, f'<token {REDACTED}>')
                record.args = ()

        except Exception:
            pass

        return True


class TelegramSink:
    """
    Delivers alerts to a chat or to a forum topic
    """

    name: ClassVar[str] = 'telegram'
    idempotent: ClassVar[bool] = False

    def __init__(
        self,
        token: str,
        chat_id: str | int,
        *,
        message_thread_id: int | None = None,
        disable_notification: bool = True,
        api_base: str = DEFAULT_API_BASE,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        max_retry_after: float = DEFAULT_MAX_RETRY_AFTER,
        proxy: str | None = None,
        **_: Any,
    ) -> None:
        """
        Configure the sink for one chat

        :param token: The bot token issued by BotFather
        :param chat_id: The target chat: an id, or an ``@channel`` name
        :param message_thread_id: Forum topic to send into, when there is one
        :param disable_notification: Send the messages silently
        :param api_base: Base URL of the Bot API
        :param min_interval: Smallest gap between two messages, in seconds
        :param max_retry_after: Longest pause a 429 may impose, in seconds
        :param proxy: Proxy URL for the HTTP client
        :param _: Further keys in the config section, accepted and ignored

        :return None:
        """

        if not token:
            raise ValueError('token is required')

        self._token = token
        self._chat_id = chat_id
        self._thread_id = message_thread_id
        self._silent = disable_notification
        self._api_base = api_base.rstrip('/')
        self._min_interval = min_interval
        self._max_retry_after = max_retry_after
        self._proxy = proxy
        self._options = Options()
        self._http_client: Any = None
        self._lock = threading.Lock()
        self._last_send = 0.0
        self._gated_until = 0.0
        self._log_filter: _TokenRedactingFilter | None = None

    def bind(self, options: Options) -> None:
        """
        Accept the resolved options

        :param options: The resolved options, used when rendering an alert

        :return None:
        """

        self._options = options

    def send(self, alert: Alert, *, key: str, timeout: float) -> None:
        """
        Deliver an alert to the chat

        :param alert: The event to deliver
        :param key: Delivery key, reused across retries
        :param timeout: How long the send may block

        :return None:
        """

        self._install_log_filter()

        messages = render_telegram(alert, self._options)

        for index, message in enumerate(messages):
            if index:
                self._throttle()

            self._send_one(message, timeout=timeout)

    def _send_one(self, text: str, *, timeout: float) -> None:
        """
        POST one rendered message to ``sendMessage``

        :param text: The rendered message body
        :param timeout: How long the request may take

        :return None:
        """

        self._respect_gate()
        httpx = require('httpx', 'telegram')
        payload: dict[str, Any] = {
            'chat_id': self._chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'link_preview_options': {'is_disabled': True},
        }

        if self._thread_id is not None:
            payload['message_thread_id'] = self._thread_id

        if self._silent:
            payload['disable_notification'] = True

        try:
            response = self._client(httpx).post(
                f'/bot{self._token}/sendMessage', json=payload, timeout=timeout
            )

        except httpx.HTTPError as exc:
            raise SinkTransientError(f'Telegram is unreachable: {exc}') from exc

        self._last_send = time.monotonic()

        if response.status_code == 429:
            retry_after = _parse_retry_after(response)
            self._gated_until = time.monotonic() + min(retry_after, self._max_retry_after)

            raise SinkRateLimited('Telegram asks us to wait', retry_after=retry_after)

        if response.status_code >= 500:
            raise SinkTransientError(f'Telegram returned {response.status_code}')

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
                kwargs: dict[str, Any] = {'base_url': self._api_base}

                if self._proxy:
                    kwargs['proxy'] = self._proxy

                self._http_client = httpx.Client(**kwargs)

            return self._http_client

    def _throttle(self) -> None:
        """
        Sleep for whatever is left of the minimum interval between two messages

        :return None:
        """

        elapsed = time.monotonic() - self._last_send

        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

    def _respect_gate(self) -> None:
        """
        Refuse to send while a 429 pause is still in force

        :return None:
        """

        remaining = self._gated_until - time.monotonic()

        if remaining > 0:
            raise SinkRateLimited('Telegram is paused after a 429', retry_after=remaining)

    def _install_log_filter(self) -> None:
        """
        Keep the token from leaking into the application's logs through httpx

        :return None:
        """

        with self._lock:
            if self._log_filter is not None:
                return

            self._log_filter = _TokenRedactingFilter(self._token)
            logging.getLogger('httpx').addFilter(self._log_filter)
            logging.getLogger('httpcore').addFilter(self._log_filter)

    def healthcheck(self) -> None:
        """
        Check the token and the chat's reachability without sending anything

        :return None:
        """

        httpx = require('httpx', 'telegram')

        try:
            me = self._client(httpx).get(f'/bot{self._token}/getMe', timeout=10.0)

        except httpx.HTTPError as exc:
            raise SinkTransientError(f'Telegram is unreachable: {exc}') from exc

        if me.status_code >= 400:
            raise SinkPermanentError(f'token rejected: {_describe(me)}')

        try:
            chat = self._client(httpx).get(
                f'/bot{self._token}/getChat',
                params={'chat_id': self._chat_id},
                timeout=10.0,
            )

        except httpx.HTTPError as exc:
            raise SinkTransientError(f'Telegram is unreachable: {exc}') from exc

        if chat.status_code >= 400:
            raise SinkPermanentError(
                f'chat {self._chat_id} is unreachable: {_describe(chat)}. '
                f'A bot cannot message first - the recipient has to press /start'
            )

    def close(self) -> None:
        """
        Remove the log filter and close the HTTP client

        :return None:
        """

        with self._lock:
            if self._log_filter is not None:
                logging.getLogger('httpx').removeFilter(self._log_filter)
                logging.getLogger('httpcore').removeFilter(self._log_filter)
                self._log_filter = None

            if self._http_client is not None:
                with contextlib.suppress(Exception):
                    self._http_client.close()

                self._http_client = None


def _parse_retry_after(response: Any) -> float:
    """
    How long Telegram says to wait

    :param response: The response that carried the 429

    :return float:
    """

    try:
        parameters = response.json().get('parameters') or {}
        value = parameters.get('retry_after')

        if isinstance(value, (int, float)) and value > 0:
            return float(value)

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
        description = body.get('description')

        if description:
            return f'{response.status_code}: {description}'

    except Exception:
        pass

    return f'HTTP {response.status_code}'


__all__ = ('TelegramSink',)
