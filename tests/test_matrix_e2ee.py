"""
Logalert Tests Matrix E2ee

The Matrix sink for encrypted rooms: every way to silently lose encryption must
fail loudly.

:copyright: (c) 2025 Aiko Sora
"""

import asyncio
import os
import stat
import sys
import types
from pathlib import Path
from typing import Any, ClassVar

import pytest
from conftest import make_alert

from logalert.exceptions import (
    EncryptedRoomError,
    InvalidOption,
    LogAlertError,
    SinkPermanentError,
    SinkRateLimited,
    SinkTransientError,
)
from logalert.sinks import matrix_e2ee
from logalert.sinks.matrix_e2ee import (
    MatrixE2ESink,
    _load_or_create_pickle_key,
    raise_for_response,
)

ROOM = '!abcdef:example.org'


class FakeResponse:
    """
    An error response: the presence of `status_code` is itself the sign of a nio
    failure
    """

    def __init__(self, status_code: int, message: str = 'error', **extra: Any) -> None:
        """
        Build a failure response with the given status

        :param status_code: The HTTP status of the response
        :param message: The message the sink reads
        :param extra: The attributes the sink looks for, such as
            `retry_after_ms`

        :return None:
        """

        self.status_code = status_code
        self.message = message

        for key, value in extra.items():
            setattr(self, key, value)


class FakeOk:
    """
    A success response: it has no `status_code`, just like a real
    RoomSendResponse
    """

    event_id = '$abc'


class FakeAsyncClientConfig:
    """
    Stands in for the nio config, storing
    whatever keywords it is given
    """

    def __init__(self, **kwargs: Any) -> None:
        """
        Store every keyword it is given,
        without looking at any of them

        :param kwargs: The configuration keywords,kept as plain attributes

        :return None:
        """

        self.__dict__.update(kwargs)


class FakeSyncResponse:
    """
    Stands in for a sync response,
    which the callback only has to recognise
    """


class FakeClient:
    """
    Stands in for `nio.AsyncClient`,
    recording everything the sink sends
    """

    created: ClassVar[list['FakeClient']] = []
    olm_present: ClassVar[bool] = True
    room_present: ClassVar[bool] = True
    #: When set, `sync_forever` never reports a sync, to exercise the startup
    #: timeout.
    sync_silently: ClassVar[bool] = False
    encrypted_room: ClassVar[bool] = True

    def __init__(
        self,
        homeserver: str,
        user_id: str,
        *,
        device_id: str | None = None,
        store_path: str | None = None,
        config: Any = None,
    ) -> None:
        """
        Record the arguments, and register the client so the tests can reach it

        :param homeserver: The homeserver the client was pointed at
        :param user_id: The mxid the client logs in as
        :param device_id: The fixed device id
        :param store_path: The directory of the E2EE store
        :param config: The `AsyncClientConfig` the sink built

        :return None:
        """

        self.homeserver = homeserver
        self.user_id = user_id
        self.device_id = device_id
        self.store_path = store_path
        self.config = config
        self.olm = object() if self.olm_present else None
        self.rooms = (
            {ROOM: types.SimpleNamespace(encrypted=self.encrypted_room)}
            if self.room_present
            else {}
        )
        self.restore_login_calls: list[tuple[str, str, str]] = []
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._response_callback: Any = None
        self.send_response: Any = FakeOk()

        FakeClient.created.append(self)

    def restore_login(self, user_id: str, device_id: str, access_token: str) -> None:
        """
        Record the login instead of loading a store

        :param user_id: The mxid being logged in
        :param device_id: The device id of the login
        :param access_token: The access token of the login

        :return None:
        """

        self.restore_login_calls.append((user_id, device_id, access_token))

    def add_response_callback(self, callback: Any, kind: Any) -> None:
        """
        Keep the callback, which the sync loop then fires

        :param callback: The function the sink wants called on every sync
        :param kind: The kind of response it wants, unused here

        :return None:
        """

        self._response_callback = callback

    async def sync_forever(
        self, timeout: int | None = None, loop_sleep_time: int | None = None
    ) -> None:
        """
        Fire the callback once as if a sync had arrived, then stay in the loop

        :param timeout: The sync timeout the sink asked for
        :param loop_sleep_time: The pause between syncs the sink asked for

        :return None:
        """

        if self._response_callback is not None and not self.sync_silently:
            self._response_callback(FakeSyncResponse())

        while True:
            await asyncio.sleep(3600)

    async def room_send(
        self,
        room_id: str,
        message_type: str,
        content: dict[str, Any],
        tx_id: str | None = None,
        ignore_unverified_devices: bool = False,
    ) -> Any:
        """
        Record the event, then answer the way the test told it to

        :param room_id: The room the event is sent to
        :param message_type: The type of the event
        :param content: The body of the event
        :param tx_id: The transaction id, which is the delivery key
        :param ignore_unverified_devices: Whether nio may share the key with
            unverified devices

        :return Any:
        """

        self.sent.append(
            {
                'room_id': room_id,
                'type': message_type,
                'content': content,
                'tx_id': tx_id,
                'ignore_unverified': ignore_unverified_devices,
            }
        )

        if isinstance(self.send_response, BaseException):
            raise self.send_response

        return self.send_response

    async def close(self) -> None:
        """
        Mark the client as closed

        :return None:
        """

        self.closed = True


@pytest.fixture
def fake_nio(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    """
    Replace nio wholesale

    :param monkeypatch: The patcher of pytest

    :return type[FakeClient]:
    """

    FakeClient.created = []
    FakeClient.olm_present = True
    FakeClient.encrypted_room = True
    FakeClient.room_present = True
    FakeClient.sync_silently = False
    module = types.SimpleNamespace(
        AsyncClient=FakeClient,
        AsyncClientConfig=FakeAsyncClientConfig,
        SyncResponse=FakeSyncResponse,
    )

    store_module = types.SimpleNamespace(SqliteStore=object)

    def fake_require(name: str, extra: str) -> Any:
        if name == 'nio':
            return module

        if name == 'nio.store.database':
            return store_module

        raise AssertionError(f'unexpected import: {name}')

    monkeypatch.setattr(matrix_e2ee, 'require', fake_require)

    return FakeClient


@pytest.fixture
def store(tmp_path: Path) -> str:
    """
    An empty directory for the E2EE store

    :param tmp_path: The temporary directory of the test

    :return str:
    """

    path = tmp_path / 'matrix-store'
    path.mkdir()

    return str(path)


def _sink(store: str, **kwargs: Any) -> MatrixE2ESink:
    """
    Build an E2EE sink around a temporary store

    :param store: The path to the store
    :param kwargs: The options that replace the defaults

    :return MatrixE2ESink:
    """

    params: dict[str, Any] = {
        'homeserver': 'https://matrix.example.org',
        'room_id': ROOM,
        'access_token': 'syt_token',
        'user_id': '@bot:example.org',
        'device_id': 'LOGALERT',
        'store_path': store,
        'start_timeout': 5.0,
    }

    params.update(kwargs)

    return MatrixE2ESink(**params)


class TestValidation:
    """
    Validation: the configuration an encrypted sink will not start without
    """

    def test_device_id_is_required(self, fake_nio: Any, store: str) -> None:
        """
        Without a fixed device_id the keys come out different every time
        """

        with pytest.raises(InvalidOption, match='device_id'):
            _sink(store, device_id='')

    def test_store_path_is_required(self, fake_nio: Any, store: str) -> None:
        """
        An empty store path is refused
        """

        with pytest.raises(InvalidOption, match='store_path'):
            _sink(store, store_path='')

    def test_user_id_must_be_an_mxid(self, fake_nio: Any, store: str) -> None:
        """
        A user id that is not an mxid is refused
        """

        with pytest.raises(InvalidOption, match='user_id'):
            _sink(store, user_id='bot')


class TestSilentEncryptionLoss:
    """
    Three ways to silently lose encryption - all of them have to become loud
    """

    def test_missing_e2e_extra_fails_loudly(self, fake_nio: type[FakeClient], store: str) -> None:
        """
        Without the `e2e` extra `client.olm = None`, and it sends plaintext
        """

        fake_nio.olm_present = False

        with pytest.raises(LogAlertError, match='matrix-e2ee'):
            _sink(store)

    def test_restore_login_is_used_not_token_assignment(
        self, fake_nio: type[FakeClient], store: str
    ) -> None:
        """
        Assigning `access_token` does not load the store or the E2EE machine
        """

        sink = _sink(store)

        try:
            client = fake_nio.created[-1]

            # Checking that the credentials went through restore_login
            assert client.restore_login_calls == [('@bot:example.org', 'LOGALERT', 'syt_token')]

        finally:
            sink.close()

    def test_missing_room_reports_that_not_encryption(
        self, fake_nio: type[FakeClient], store: str
    ) -> None:
        """
        Being absent from the room is not the same thing as there being no
        encryption
        """

        fake_nio.room_present = False

        with pytest.raises(LogAlertError, match='is not in room'):
            _sink(store)

    def test_silent_homeserver_gives_a_readable_error(
        self, fake_nio: type[FakeClient], store: str
    ) -> None:
        """
        A homeserver that never syncs reports what to check
        """

        fake_nio.sync_silently = True

        with pytest.raises(LogAlertError, match='did not sync'):
            _sink(store, start_timeout=0.3)

    def test_losing_the_startup_timeout_race_is_still_readable(
        self,
        fake_nio: type[FakeClient],
        store: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The enclosing timeout can win the race against the one inside the
        connect step
        """

        def never_returns(self: object, coro: Any, timeout: float) -> None:
            # Close the coroutine before giving up on it, or Python warns that
            # it was never awaited and the suite fills with noise.
            coro.close()

            raise TimeoutError

        monkeypatch.setattr(matrix_e2ee._LoopThread, 'submit', never_returns)

        with pytest.raises(LogAlertError, match='did not answer'):
            _sink(store)

    def test_unencrypted_room_is_refused(self, fake_nio: type[FakeClient], store: str) -> None:
        """
        Encryption was asked for and the room is open - the messages would go as
        plaintext
        """

        fake_nio.encrypted_room = False

        with pytest.raises(EncryptedRoomError, match='is not encrypted'):
            _sink(store)

    def test_unencrypted_room_allowed_when_explicitly_asked(
        self, fake_nio: type[FakeClient], store: str
    ) -> None:
        """
        A deliberate choice by the user is respected - but only a deliberate one
        """

        fake_nio.encrypted_room = False
        sink = _sink(store, require_encryption=False)

        try:
            assert sink._room_is_encrypted is False

        finally:
            sink.close()


class TestSending:
    """
    Sending: the transaction id, the event shape and the chunking
    """

    def test_tx_id_is_the_delivery_key(self, fake_nio: type[FakeClient], store: str) -> None:
        """
        Idempotency: a redelivery with the same key does not create a second
        event
        """

        sink = _sink(store)

        try:
            sink.send(make_alert(), key='msg-1', timeout=5.0)
            client = fake_nio.created[-1]

            assert client.sent[0]['tx_id'] == 'msg-1'

        finally:
            sink.close()

    def test_message_shape(self, fake_nio: type[FakeClient], store: str) -> None:
        """
        The sent event has the shape Matrix expects
        """

        sink = _sink(store)

        try:
            sink.send(make_alert(), key='k', timeout=5.0)
            sent = fake_nio.created[-1].sent[0]

        finally:
            sink.close()

        # Checking the rendered body of the event
        assert sent['room_id'] == ROOM
        assert sent['type'] == 'm.room.message'
        assert sent['content']['msgtype'] == 'm.notice'
        assert sent['content']['format'] == 'org.matrix.custom.html'

    def test_unverified_devices_are_allowed(self, fake_nio: type[FakeClient], store: str) -> None:
        """
        Without this nio refuses to share the key, and the message never goes
        out at all
        """

        sink = _sink(store)

        try:
            sink.send(make_alert(), key='k', timeout=5.0)

            assert fake_nio.created[-1].sent[0]['ignore_unverified'] is True

        finally:
            sink.close()

    def test_chunks_get_distinct_tx_ids(self, fake_nio: type[FakeClient], store: str) -> None:
        """
        Every chunk is a separate event with a transaction id of its own
        """

        from logalert.options import Options
        from logalert.types import ExceptionInfo, Frame, Stacktrace

        exc = ExceptionInfo(
            type_name='ValueError',
            module='builtins',
            message='breakage',
            stacktrace=Stacktrace(
                frames=tuple(
                    Frame(filename='/app/src/orders.py', lineno=i, function=f'h_{i}')
                    for i in range(600)
                )
            ),
        )

        sink = _sink(store)
        sink.bind(Options(max_frames=600))

        try:
            sink.send(make_alert(exception=exc), key='msg-1', timeout=5.0)
            tx_ids = [entry['tx_id'] for entry in fake_nio.created[-1].sent]

        finally:
            sink.close()

        # Checking that the chunks were not collapsed into one
        assert len(tx_ids) > 1
        assert len(set(tx_ids)) == len(tx_ids)


class TestErrorClassification:
    """
    Error classification: every failure becomes the right sink error
    """

    def test_success_does_not_raise(self) -> None:
        """
        A success response raises nothing at all
        """

        raise_for_response(FakeOk())

    def test_rate_limit_reads_retry_after_ms(self) -> None:
        """
        The 429 delay is read from `retry_after_ms` and converted to seconds
        """

        with pytest.raises(SinkRateLimited) as info:
            raise_for_response(FakeResponse(429, retry_after_ms=2500))

        assert info.value.retry_after == 2.5

    def test_rate_limit_falls_back_to_seconds(self) -> None:
        """
        In some Synapse builds the field arrives in seconds
        """

        with pytest.raises(SinkRateLimited) as info:
            raise_for_response(FakeResponse(429, retry_after=7))

        assert info.value.retry_after == 7.0

    def test_server_error_is_transient(self) -> None:
        """
        A 503 is a transient failure
        """

        with pytest.raises(SinkTransientError):
            raise_for_response(FakeResponse(503))

    def test_client_error_is_permanent(self) -> None:
        """
        A 403 is a permanent failure
        """

        with pytest.raises(SinkPermanentError):
            raise_for_response(FakeResponse(403, 'not in the room'))

    def test_transport_error_becomes_transient(
        self, fake_nio: type[FakeClient], store: str
    ) -> None:
        """
        A dropped connection is a transient failure
        """

        sink = _sink(store)

        try:
            fake_nio.created[-1].send_response = ConnectionError('no connection')

            with pytest.raises(SinkTransientError):
                sink.send(make_alert(), key='k', timeout=5.0)

        finally:
            sink.close()

    def test_encryption_failure_becomes_transient(
        self, fake_nio: type[FakeClient], store: str
    ) -> None:
        """
        The key may not make it through on
        the first try - a retry often helps
        """

        class GroupEncryptionError(Exception):
            """
            Stands in for the error nio raises when no device can be given the
            key
            """

            pass

        sink = _sink(store)

        try:
            fake_nio.created[-1].send_response = GroupEncryptionError('no devices')

            with pytest.raises(SinkTransientError):
                sink.send(make_alert(), key='k', timeout=5.0)

        finally:
            sink.close()


class TestPickleKey:
    """
    Pickle key: the key the store is encrypted with, and who may read it
    """

    def test_key_is_created_and_reused(self, tmp_path: Path) -> None:
        """
        The key has to survive a restart:
        otherwise the store cannot be read
        """

        first = _load_or_create_pickle_key(str(tmp_path))
        second = _load_or_create_pickle_key(str(tmp_path))

        assert first == second
        assert len(first) > 20

    def test_key_file_is_readable_only_by_owner(self, tmp_path: Path) -> None:
        """
        The store holds the encryption key - effectively the bot's identity
        """

        _load_or_create_pickle_key(str(tmp_path))
        path = tmp_path / '.pickle_key'
        mode = stat.S_IMODE(os.stat(path).st_mode)

        assert mode == 0o600, f'mode {oct(mode)} instead of 0600'

    def test_key_is_not_a_well_known_default(self, tmp_path: Path) -> None:
        """
        A widely known key would mean the store is lying open
        """

        key = _load_or_create_pickle_key(str(tmp_path / 'one'))
        other = _load_or_create_pickle_key(str(tmp_path / 'two'))

        assert key != other


class TestSelection:
    """
    Selection: the `e2ee` flag picks this
    sink and stays out of its config
    """

    def test_e2ee_flag_selects_the_encrypted_sink(self, fake_nio: Any, store: str) -> None:
        """
        The `e2ee` flag selects the encrypted sink
        """

        from logalert.sinks import _build_matrix

        sink = _build_matrix(
            {
                'homeserver': 'https://m.example.org',
                'room_id': ROOM,
                'access_token': 'syt_token',
                'user_id': '@bot:example.org',
                'device_id': 'LOGALERT',
                'store_path': store,
                'e2ee': True,
            }
        )

        try:
            assert isinstance(sink, MatrixE2ESink)

        finally:
            sink.close()

    def test_e2ee_flag_is_not_forwarded_to_the_sink(self, fake_nio: Any, store: str) -> None:
        """
        The `e2ee` key is ours - the sink must not know about it
        """

        from logalert.sinks import _build_matrix

        sink = _build_matrix(
            {
                'homeserver': 'https://m.example.org',
                'room_id': ROOM,
                'access_token': 'syt_token',
                'user_id': '@bot:example.org',
                'device_id': 'LOGALERT',
                'store_path': store,
                'e2ee': True,
            }
        )

        sink.close()

    def test_without_the_flag_the_plain_sink_is_built(self) -> None:
        """
        The plain sink is what the section means unless `e2ee` says otherwise
        """

        from logalert.sinks import MatrixSink, _build_matrix

        sink = _build_matrix(
            {
                'homeserver': 'https://m.example.org',
                'room_id': ROOM,
                'access_token': 'syt_token',
            }
        )

        try:
            assert isinstance(sink, MatrixSink)

        finally:
            sink.close()

    def test_importing_logalert_does_not_pull_nio(self) -> None:
        """
        `import logalert` has to stay empty
        """

        import subprocess

        src = str(Path(__file__).resolve().parent.parent / 'src')
        result = subprocess.run(
            [
                sys.executable,
                '-c',
                'import sys; import logalert; '
                "print('nio' in sys.modules or 'asyncio' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            env={**os.environ, 'PYTHONPATH': src},
            timeout=60,
        )

        assert result.stdout.strip() == 'False', 'import logalert pulled in nio or asyncio'


__all__ = (
    'TestSilentEncryptionLoss',
    'TestErrorClassification',
    'FakeAsyncClientConfig',
    'FakeSyncResponse',
    'TestValidation',
    'TestPickleKey',
    'TestSelection',
    'FakeResponse',
    'TestSending',
    'FakeClient',
    'fake_nio',
    'FakeOk',
    'store',
)
