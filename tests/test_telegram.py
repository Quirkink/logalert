"""
Logalert Tests Telegram

The Telegram sink: markup,
limits, and response parsing.

:copyright: (c) 2025 Aiko Sora
"""

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from conftest import make_alert

from logalert.exceptions import SinkPermanentError, SinkRateLimited, SinkTransientError
from logalert.sinks.telegram import TelegramSink
from logalert.types import ExceptionInfo, Frame, Stacktrace

TOKEN = '123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw'


def _sink(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> TelegramSink:
    """
    Build a sink whose HTTP client answers from the given handler

    :param handler: The function that produces the response for every request
    :param kwargs: The extra options passed to the sink

    :return TelegramSink:
    """

    sink = TelegramSink(token=TOKEN, chat_id='-100123', min_interval=0.0, **kwargs)
    sink._http_client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url='https://api.telegram.org'
    )

    return sink


def _ok(request: httpx.Request) -> httpx.Response:
    """
    A successful sendMessage response

    :param request: The request being answered, unused here

    :return httpx.Response:
    """

    return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}})


class TestRequestShape:
    """
    Request shape: the payload, the parse mode and the thread id
    """

    def test_payload_uses_html_and_disables_preview(self) -> None:
        """
        The payload asks for HTML and turns the link preview off
        """

        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))

            assert request.url.path == f'/bot{TOKEN}/sendMessage'

            return _ok(request)

        sink = _sink(handler)

        try:
            sink.send(make_alert(), key='k', timeout=5.0)

        finally:
            sink.close()

        # Checking the shape of the request body
        payload = seen[0]

        assert payload['parse_mode'] == 'HTML'
        assert payload['link_preview_options'] == {'is_disabled': True}
        assert payload['chat_id'] == '-100123'

    def test_thread_id_is_omitted_when_not_set(self) -> None:
        """
        For the General topic an explicit id produces `message thread not found`
        """

        def handler(request: httpx.Request) -> httpx.Response:
            assert 'message_thread_id' not in json.loads(request.content)

            return _ok(request)

        sink = _sink(handler)

        try:
            sink.send(make_alert(), key='k', timeout=5.0)

        finally:
            sink.close()

    def test_thread_id_is_forwarded_when_set(self) -> None:
        """
        A configured thread id reaches the payload
        """

        def handler(request: httpx.Request) -> httpx.Response:
            assert json.loads(request.content)['message_thread_id'] == 42

            return _ok(request)

        sink = _sink(handler, message_thread_id=42)

        try:
            sink.send(make_alert(), key='k', timeout=5.0)

        finally:
            sink.close()


class TestErrors:
    """
    Errors: a 429, a 403 and a 503 each mean something different
    """

    def test_rate_limit_uses_retry_after_from_body(self) -> None:
        """
        The value comes from `parameters.retry_after`.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                json={
                    'ok': False,
                    'error_code': 429,
                    'description': 'Too Many Requests',
                    'parameters': {'retry_after': 37},
                },
            )

        sink = _sink(handler)

        try:
            with pytest.raises(SinkRateLimited) as exc_info:
                sink.send(make_alert(), key='k', timeout=5.0)

        finally:
            sink.close()

        assert exc_info.value.retry_after == 37.0

    def test_rate_limit_gates_subsequent_messages(self) -> None:
        """
        Telegram rate-limits per bot, so a 429 affects the other messages too
        """

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append('request')

            return httpx.Response(429, json={'parameters': {'retry_after': 30}})

        sink = _sink(handler)

        try:
            with pytest.raises(SinkRateLimited):
                sink.send(make_alert(), key='k', timeout=5.0)

            with pytest.raises(SinkRateLimited):
                sink.send(make_alert(), key='k2', timeout=5.0)

        finally:
            sink.close()

        # Checking that the second send never reached the network
        assert len(calls) == 1, 'sending continued after the 429'

    def test_forbidden_is_permanent(self) -> None:
        """
        A 403 is a permanent failure
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={'description': 'bot was blocked by the user'})

        sink = _sink(handler)

        try:
            with pytest.raises(SinkPermanentError):
                sink.send(make_alert(), key='k', timeout=5.0)

        finally:
            sink.close()

    def test_server_error_is_transient(self) -> None:
        """
        A 503 is a transient failure
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text='upstream down')

        sink = _sink(handler)

        try:
            with pytest.raises(SinkTransientError):
                sink.send(make_alert(), key='k', timeout=5.0)

        finally:
            sink.close()


class TestFormatting:
    """
    Formatting: escaping, the 4096 limit, and the token hidden from the logs
    """

    def test_html_is_escaped(self) -> None:
        """
        Markup in the message is escaped before it is sent
        """

        bodies: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content)['text'])

            return _ok(request)

        alert = make_alert(message='a < b & c > d')
        sink = _sink(handler)

        try:
            sink.send(alert, key='k', timeout=5.0)

        finally:
            sink.close()

        assert 'a &lt; b &amp; c &gt; d' in bodies[0]

    def test_long_traceback_is_split_within_limit(self) -> None:
        """
        Telegram accepts 4096 characters; a long traceback is cut into several
        messages
        """

        bodies: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content)['text'])

            return _ok(request)

        exc = ExceptionInfo(
            type_name='ValueError',
            module='builtins',
            message='breakage',
            stacktrace=Stacktrace(
                frames=tuple(
                    Frame(
                        filename='/app/src/orders/service.py',
                        lineno=index * 3,
                        function=f'handler_{index}',
                        source_line=f'    result = process(item_{index})',
                    )
                    for index in range(400)
                )
            ),
        )

        sink = _sink(handler)

        try:
            sink.send(make_alert(exception=exc), key='k', timeout=5.0)

        finally:
            sink.close()

        # Checking that every message fits, and that the markup is balanced
        assert len(bodies) > 1, 'the long traceback went out as a single message'

        for body in bodies:
            assert len(body.encode('utf-16-le')) // 2 <= 4096
            assert body.count('<pre>') == body.count('</pre>')

    def test_token_never_reaches_application_logs(self) -> None:
        """
        httpx logs the URL, and the bot token sits in it
        """

        records: list[str] = []

        class Collector(logging.Handler):
            """
            Keeps the rendered text of every record that reaches it
            """

            def emit(self, record: logging.LogRecord) -> None:
                """
                Keep the text of the record

                :param record: The record a logger emitted

                :return None:
                """

                records.append(record.getMessage())

        collector = Collector()

        httpx_logger = logging.getLogger('httpx')
        httpx_logger.addHandler(collector)
        httpx_logger.setLevel(logging.INFO)

        sink = _sink(_ok)

        try:
            sink.send(make_alert(), key='k', timeout=5.0)
            httpx_logger.info(
                'HTTP Request: POST https://api.telegram.org/bot%s/sendMessage', TOKEN
            )

        finally:
            sink.close()
            httpx_logger.removeHandler(collector)

        # Checking that the token was filtered out of the log records
        assert TOKEN not in ''.join(records)
        assert any('<token' in record for record in records), 'the filter did not work'


__all__ = (
    'TestRequestShape',
    'TestFormatting',
    'TestErrors',
)
