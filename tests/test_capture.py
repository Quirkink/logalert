"""
Logalert Tests Capture

Capture: hooks, filtering, and above all the absence of an amplification loop.

:copyright: (c) 2025 Aiko Sora
"""

import logging
import sys
import threading

from conftest import RecordingSink

import logalert
from logalert._guard import capture_guard, is_capturing, is_origin_denied, reset_thread_state
from logalert.capture import should_ignore
from logalert.options import Options


class DbTimeout(Exception):
    """
    Declared at module level: a nested class would carry a path in
    `__qualname__`
    """


class TestIgnoreRules:
    """
    Ignore rules: which exceptions are silently dropped
    """

    def test_keyboard_interrupt_is_ignored_by_default(self) -> None:
        """
        Ctrl-C on a developer's machine must not fly off into the shared chat
        """

        assert should_ignore(KeyboardInterrupt(), Options()) is True

    def test_keyboard_interrupt_can_be_enabled(self) -> None:
        """
        The setting lets those who want the interrupts have them
        """

        assert should_ignore(KeyboardInterrupt(), Options(capture_keyboard_interrupt=True)) is False

    def test_system_exit_is_ignored(self) -> None:
        """
        `excepthook` is never called for SystemExit, but we filter manual
        capture anyway
        """

        assert should_ignore(SystemExit(0), Options()) is True

    def test_cancelled_error_is_ignored(self) -> None:
        """
        A cancellation error is not an application failure
        """

        class CancelledError(Exception):
            """
            Stands in for the cancellation error of an async framework
            """

            pass

        assert should_ignore(CancelledError(), Options()) is True

    def test_qualified_name_matches(self) -> None:
        """
        The full dotted name of an ignored error is accepted
        """

        qualified = f'{DbTimeout.__module__}.DbTimeout'

        assert should_ignore(DbTimeout(), Options(ignore_errors=(qualified,))) is True

    def test_bare_name_matches(self) -> None:
        """
        A user will write the short name, not the full one - we have to accept
        both
        """

        assert should_ignore(DbTimeout(), Options(ignore_errors=('DbTimeout',))) is True

    def test_unrelated_exception_is_not_ignored(self) -> None:
        """
        An error that is not on the list goes through
        """

        assert should_ignore(DbTimeout(), Options(ignore_errors=('ValueError',))) is False

    def test_cause_chain_is_checked(self) -> None:
        """
        If the cause is on the list, we stay silent about the consequence as
        well
        """

        try:
            try:
                raise KeyboardInterrupt

            except KeyboardInterrupt as root:
                raise ValueError('consequence') from root

        except ValueError as exc:
            assert should_ignore(exc, Options()) is True


class TestOriginDenylist:
    """
    Origin denylist: errors raised inside the network stack are dropped
    """

    def test_network_libraries_are_denied(self) -> None:
        """
        Errors raised inside the network stack are dropped
        """

        assert is_origin_denied('httpx') is True
        assert is_origin_denied('httpx._client') is True
        assert is_origin_denied('logalert.handler') is True
        assert is_origin_denied('redis.connection') is True

    def test_application_loggers_are_allowed(self) -> None:
        """
        A name that merely looks similar must not be caught by the denylist
        """

        assert is_origin_denied('orders.service') is False
        assert is_origin_denied('httpxclient') is False
        assert is_origin_denied('mylogalert') is False


class TestReentrancyGuard:
    """
    Reentrancy guard: only the outermost capture goes through
    """

    def test_nested_entry_is_refused(self) -> None:
        """
        Only the outermost entry goes through
        """

        with capture_guard() as first:
            assert first is True

            with capture_guard() as second:
                assert second is False

        assert is_capturing() is False

    def test_flag_is_thread_local(self) -> None:
        """
        A global flag would silence legitimate alerts coming from other threads
        """

        inside = threading.Event()
        observed: list[bool] = []

        def other() -> None:
            observed.append(is_capturing())

        with capture_guard():
            thread = threading.Thread(target=other)

            thread.start()
            thread.join()

        # Checking that the other thread saw no capture in progress
        assert observed == [False]
        assert inside.is_set() is False

    def test_reset_clears_state(self) -> None:
        """
        After a fork the child process must not inherit a raised flag
        """

        with capture_guard():
            reset_thread_state()

            assert is_capturing() is False


class TestNoAmplification:
    """
    No amplification: a sink that logs must not breed further alerts
    """

    def test_sink_that_logs_does_not_loop(self) -> None:
        """
        A sink that logs from `send()` must not start an endless loop
        """

        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        deliveries: list[str] = []

        class LoggingSink(RecordingSink):
            """
            A sink that records the delivery and then logs, which is what must
            not loop
            """

            def send(self, alert: logalert.Alert, *, key: str, timeout: float) -> None:
                """
                Record the delivery, then log an error from inside the sink

                :param alert: The alert being delivered
                :param key: The idempotency key of the delivery
                :param timeout: How long the send is allowed to take

                :return None:
                """

                deliveries.append(alert.event_id)
                logging.getLogger('my.sink').error('failed to send')

        sink = LoggingSink()
        client = logalert.Client(logalert.Options(sinks=[sink], dedup_window=0))
        handler = logalert.AlertHandler(client=client)

        root.addHandler(handler)

        try:
            client.capture(ValueError('the original error'))
            client.flush(3.0)

            for _ in range(5):
                client.flush(0.2)

        finally:
            root.removeHandler(handler)
            client.close()

        # Checking that the log line written by the sink produced no further
        # alerts
        assert len(deliveries) == 1, f'amplification loop: {len(deliveries)} deliveries'


class TestHandlerRobustness:
    """
    Handler robustness: a hostile record neither raises nor prints
    """

    def test_hostile_record_does_not_raise_or_print(self, capsys: object) -> None:
        """
        A hostile record must neither raise nor print anything
        """

        class Hostile:
            """
            A value whose formatting blows up, the way a broken object's does
            """

            def __str__(self) -> str:
                """
                Raise when the record is formatted

                :return str:
                """

                raise RuntimeError('hostile __str__')

            def __repr__(self) -> str:
                """
                Raise when the record is represented

                :return str:
                """

                raise RuntimeError('hostile __repr__')

        sink = RecordingSink()
        client = logalert.Client(logalert.Options(sinks=[sink], dedup_window=0))
        handler = logalert.AlertHandler(client=client)
        record = logging.LogRecord(
            'orders', logging.ERROR, 'x.py', 1, 'value: %s', (Hostile(),), None
        )

        try:
            handler.emit(record)

        finally:
            client.close()

        # Checking that the failure was counted instead of being raised
        assert handler.errors == 1

    def test_alert_handler_rejects_unknown_kwargs(self) -> None:
        """
        A typo in dictConfig must not quietly do nothing
        """

        import pytest

        with pytest.raises(TypeError, match='does not know these parameters'):
            logalert.AlertHandler(levle='ERROR')  # type: ignore[call-arg]

    def test_breadcrumb_handler_fills_buffer(self) -> None:
        """
        A breadcrumb handler puts records into the scope buffer
        """

        sink = RecordingSink()
        client = logalert.Client(logalert.Options(sinks=[sink], breadcrumbs=10))
        handler = logalert.BreadcrumbHandler(client=client)

        try:
            handler.emit(
                logging.LogRecord('orders', logging.INFO, 'x.py', 1, 'step %s', ('one',), None)
            )

            crumbs = client.scope.breadcrumbs.snapshot()

        finally:
            client.close()

        # Checking the recorded crumb
        assert len(crumbs) == 1
        assert crumbs[0].message == 'step one'
        assert crumbs[0].data['logger'] == 'orders'


class TestExcepthook:
    """
    Excepthook: the hook that picks up what nobody caught
    """

    def test_excepthook_is_installed_and_removed(self) -> None:
        """
        Installing a hook takes over `sys.excepthook`, and uninstalling gives it
        back
        """

        original = sys.excepthook
        sink = RecordingSink()
        client = logalert.Client(logalert.Options(sinks=[sink], dedup_window=0))
        handle = logalert.install_hooks(client, client.options)

        try:
            assert sys.excepthook is not original

        finally:
            handle.uninstall()
            client.close()

        assert sys.excepthook is original

    def test_hook_capture_marks_source_and_level(self) -> None:
        """
        An event coming from a hook has to be marked as unhandled
        """

        captured: list[logalert.Alert] = []

        class Capturer:
            """
            Stands in for the client, capturing the alert the hook builds
            """

            def capture(self, exc=None, **kwargs):  # type: ignore[no-untyped-def]
                """
                Record the alert that the hook built from the exception

                :param exc: The exception the hook is reporting
                :param kwargs: The source, mechanism and level the hook passes
                    along

                :return None:
                """

                from logalert.capture import alert_from_exception

                captured.append(
                    alert_from_exception(
                        exc,
                        Options(),
                        source=kwargs.get('source', 'manual'),
                        mechanism=kwargs.get('mechanism', 'manual'),
                        level=kwargs.get('level', 'error'),
                    )
                )

                return 'id'

        sink = RecordingSink()
        client = logalert.Client(logalert.Options(sinks=[sink]))
        handle = logalert.install_hooks(Capturer(), client.options)

        try:
            sys.excepthook(ValueError, ValueError('crash'), None)

        finally:
            handle.uninstall()
            client.close()

        # Checking that the hook marked the event as an unhandled crash
        assert captured
        assert captured[0].source == 'excepthook'
        assert captured[0].level == 'fatal'
        assert captured[0].handled is False

    def test_hook_capture_reaches_a_real_client(self) -> None:
        """
        A hook drives a real client all the way to the sink
        """

        sink = RecordingSink()
        client = logalert.Client(logalert.Options(sinks=[sink], dedup_window=0))
        handle = logalert.install_hooks(client, client.options)

        try:
            sys.excepthook(ValueError, ValueError('crash'), None)

            client.flush(3.0)

            for _ in range(5):
                client.flush(0.2)

        finally:
            handle.uninstall()
            client.close()

        # Checking that the exception made it out of the hook and into the sink
        assert len(sink.sent) == 1, 'the hook captured nothing'
        assert sink.sent[0].level == 'fatal'
        assert sink.sent[0].handled is False


__all__ = (
    'TestHandlerRobustness',
    'TestNoAmplification',
    'TestReentrancyGuard',
    'TestOriginDenylist',
    'TestIgnoreRules',
    'TestExcepthook',
    'DbTimeout',
)
