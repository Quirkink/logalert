"""
Logalert Tests Matrix

The Matrix sink: idempotency, encryption, and response parsing.

:copyright: (c) 2025 Aiko Sora
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from conftest import make_alert

from logalert.exceptions import (
    EncryptedRoomError,
    InvalidOption,
    SinkPermanentError,
    SinkRateLimited,
)
from logalert.options import Options
from logalert.sinks.matrix import MatrixSink
from logalert.types import ExceptionInfo, Frame, Stacktrace

ROOM = '!abcdef:example.org'
TOKEN = 'syt_dGhpc19pc19hX3Rlc3RfdG9rZW4'


def _sink(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> MatrixSink:
    """
    Build a sink whose HTTP client answers from the given handler

    :param handler: The function that produces the response for every request
    :param kwargs: The extra options passed to the sink

    :return MatrixSink:
    """

    sink = MatrixSink(
        homeserver='https://matrix.example.org',
        room_id=ROOM,
        access_token=TOKEN,
        **kwargs,
    )

    sink._http_client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url='https://matrix.example.org'
    )

    return sink


def _ok(request: httpx.Request) -> httpx.Response:
    """
    A successful room send response

    :param request: The request being answered, unused here

    :return httpx.Response:
    """

    return httpx.Response(200, json={'event_id': '$abc'})


class TestValidation:
    """
    Validation: what the sink refuses at construction time
    """

    def test_alias_instead_of_room_id_is_rejected(self) -> None:
        """
        An alias looks similar, but it is no good in the API - better to say so
        at once
        """

        with pytest.raises(InvalidOption, match='must start with'):
            MatrixSink(
                homeserver='https://m.example.org', room_id='#alerts:m.org', access_token='x'
            )

    def test_missing_token_is_rejected(self) -> None:
        """
        An empty access token is refused
        """

        with pytest.raises(InvalidOption, match='access_token'):
            MatrixSink(homeserver='https://m.example.org', room_id=ROOM, access_token='')


class TestRequestShape:
    """
    Request shape: the method, the transaction id and the body
    """

    def test_put_with_txn_id_as_idempotency_key(self) -> None:
        """
        `txnId` is the delivery key, so a redelivery does not create a second
        message
        """

        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)

            return _ok(request)

        sink = _sink(handler)

        try:
            sink.send(make_alert(), key='msg-1', timeout=5.0)

        finally:
            sink.close()

        # Checking the shape of the request
        request = seen[0]

        assert request.method == 'PUT'
        assert f'/rooms/{httpx.URL(ROOM).path or ROOM}/send/m.room.message/msg-1' in str(
            request.url
        ) or 'send/m.room.message/msg-1' in str(request.url)
        assert request.headers['Authorization'] == f'Bearer {TOKEN}'

    def test_retry_reuses_the_same_txn_id(self) -> None:
        """
        A redelivery reuses the transaction id of the first attempt
        """

        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)

            return _ok(request)

        sink = _sink(handler)

        try:
            sink.send(make_alert(), key='msg-1', timeout=5.0)
            sink.send(make_alert(), key='msg-1', timeout=5.0)

        finally:
            sink.close()

        assert paths[0].endswith('/msg-1')
        assert paths[1].endswith('/msg-1'), 'the redelivery used a different txnId'

    def test_msgtype_is_notice(self) -> None:
        """
        `m.notice`, not `m.text`: otherwise other bots start replying
        automatically
        """

        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))

            return _ok(request)

        sink = _sink(handler)

        try:
            sink.send(make_alert(), key='k', timeout=5.0)

        finally:
            sink.close()

        # Checking the rendered body
        assert bodies[0]['msgtype'] == 'm.notice'
        assert bodies[0]['format'] == 'org.matrix.custom.html'
        assert bodies[0]['formatted_body']

    def test_chunks_get_distinct_txn_ids(self) -> None:
        """
        Each chunk is a separate event; a shared txnId would make Synapse
        collapse them
        """

        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)

            return _ok(request)

        exc = ExceptionInfo(
            type_name='ValueError',
            module='builtins',
            message='breakage',
            stacktrace=Stacktrace(
                frames=tuple(
                    Frame(
                        filename='/app/src/orders.py',
                        lineno=index,
                        function=f'handler_{index}',
                        source_line='    x = 1',
                    )
                    for index in range(600)
                )
            ),
        )

        sink = _sink(handler)
        sink.bind(Options(max_frames=600))

        try:
            sink.send(make_alert(exception=exc), key='msg-1', timeout=5.0)

        finally:
            sink.close()

        # Checking that every chunk carried an id of its own
        assert len(paths) > 1
        assert len(set(paths)) == len(paths), 'the chunks reused a txnId'


class TestEncryption:
    """
    Encryption: a REST client has no
    business in an encrypted room
    """

    def test_encrypted_room_fails_loudly_on_check(self) -> None:
        """
        Finding out about the problem at the moment of failure is the worst of
        moments
        """

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith('/state/m.room.encryption/'):
                return httpx.Response(200, json={'algorithm': 'm.megolm.v1.aes-sha2'})

            return httpx.Response(200, json={'user_id': '@bot:example.org'})

        sink = _sink(handler)

        try:
            with pytest.raises(EncryptedRoomError, match='matrix-e2ee'):
                sink.healthcheck()

        finally:
            sink.close()

    def test_unencrypted_room_passes_check(self) -> None:
        """
        A 404 on the state event means encryption is not enabled. That is normal
        """

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith('/state/m.room.encryption/'):
                return httpx.Response(404, json={'errcode': 'M_NOT_FOUND'})

            return httpx.Response(200, json={'user_id': '@bot:example.org'})

        sink = _sink(handler)

        try:
            sink.healthcheck()

        finally:
            sink.close()


class TestErrors:
    """
    Errors: how the responses turn into the sink's exceptions
    """

    def test_rate_limit_reads_retry_after_ms(self) -> None:
        """
        The 429 delay is read from `retry_after_ms` and converted to seconds
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                json={
                    'errcode': 'M_LIMIT_EXCEEDED',
                    'error': 'Too many requests',
                    'retry_after_ms': 2500,
                },
            )

        sink = _sink(handler)

        try:
            with pytest.raises(SinkRateLimited) as exc_info:
                sink.send(make_alert(), key='k', timeout=5.0)

        finally:
            sink.close()

        assert exc_info.value.retry_after == 2.5

    def test_rejected_token_is_permanent(self) -> None:
        """
        A rejected token is a permanent failure
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={'errcode': 'M_UNKNOWN_TOKEN'})

        sink = _sink(handler)

        try:
            with pytest.raises(SinkPermanentError):
                sink.send(make_alert(), key='k', timeout=5.0)

        finally:
            sink.close()


__all__ = (
    'TestRequestShape',
    'TestEncryption',
    'TestValidation',
    'TestErrors',
)
