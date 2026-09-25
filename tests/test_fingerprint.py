"""
Logalert Tests Fingerprint

The fingerprint is a public contract: changing it resets dedup windows for
everyone at once.

:copyright: (c) 2025 Aiko Sora
"""

from datetime import UTC, datetime
from pathlib import Path

from logalert._fingerprint import (
    FINGERPRINT_VERSION,
    fingerprint,
    normalize_message,
)
from logalert.capture import exception_to_info
from logalert.options import Options
from logalert.types import Alert, ExceptionInfo, Frame, Stacktrace

OPTIONS = Options()


def _info(exc: BaseException, **kwargs: object) -> ExceptionInfo:
    """
    Describe an exception for the fingerprint

    :param exc: The exception to describe
    :param kwargs: Accepted for symmetry with the callers, not used here

    :return ExceptionInfo:
    """

    return exception_to_info(exc, OPTIONS)


def _alert(exc: BaseException | None, **overrides: object) -> Alert:
    """
    Build an alert around an exception, with defaults for every other field

    :param exc: The exception to attach, if any
    :param overrides: The fields that replace the defaults

    :return Alert:
    """

    base: dict[str, object] = {
        'event_id': 'e' * 32,
        'timestamp': datetime.now(UTC),
        'level': 'error',
        'source': 'logging',
        'logger': 'orders',
        'message': 'message',
        'exception': _info(exc) if exc else None,
    }

    base.update(overrides)

    return Alert(**base)  # type: ignore[arg-type]


def _fake_exc(
    type_name: str = 'ValueError',
    frames: tuple[tuple[str, str], ...] = (('orders.py', 'handle'),),
) -> ExceptionInfo:
    """
    An exception with the given frames, for checks where only the fingerprint
    matters

    :param type_name: The name of the exception type
    :param frames: Filename and function of every frame

    :return ExceptionInfo:
    """

    return ExceptionInfo(
        type_name=type_name,
        module='builtins',
        message='',
        stacktrace=Stacktrace(
            frames=tuple(
                Frame(filename=name, lineno=index + 1, function=function)
                for index, (name, function) in enumerate(frames)
            )
        ),
    )


def test_line_numbers_do_not_affect_fingerprint() -> None:
    """
    Adding a line above the error site
    must not reset the suppression window
    """

    first = _fake_exc(frames=(('orders.py', 'handle'), ('db.py', 'query')))
    shifted = ExceptionInfo(
        type_name='ValueError',
        module='builtins',
        message='',
        stacktrace=Stacktrace(
            frames=(
                Frame(filename='orders.py', lineno=900, function='handle'),
                Frame(filename='db.py', lineno=950, function='query'),
            )
        ),
    )

    assert fingerprint(_alert(None, exception=first), OPTIONS) == fingerprint(
        _alert(None, exception=shifted), OPTIONS
    )


def test_exception_text_does_not_affect_fingerprint() -> None:
    """
    `KeyError: 'a'` and `KeyError: 'b'` are one breakage, not two
    """

    one = _alert(None, exception=_fake_exc())
    two = _alert(None, exception=_fake_exc())

    assert fingerprint(one, OPTIONS) == fingerprint(two, OPTIONS)


def test_different_exception_types_differ() -> None:
    """
    Two different exception types are two different breakages
    """

    assert fingerprint(_alert(None, exception=_fake_exc('ValueError')), OPTIONS) != fingerprint(
        _alert(None, exception=_fake_exc('TypeError')), OPTIONS
    )


def test_different_frames_differ() -> None:
    """
    The same error in another place is another breakage
    """

    one = _alert(None, exception=_fake_exc(frames=(('orders.py', 'handle'),)))
    two = _alert(None, exception=_fake_exc(frames=(('billing.py', 'charge'),)))

    assert fingerprint(one, OPTIONS) != fingerprint(two, OPTIONS)


def test_same_basename_in_different_packages_differs() -> None:
    """
    `a/models.py` and `b/models.py` are
    different places and must not be merged
    """

    one = _alert(None, exception=_fake_exc(frames=(('/app/a/models.py', 'save'),)))
    two = _alert(None, exception=_fake_exc(frames=(('/app/b/models.py', 'save'),)))

    assert fingerprint(one, OPTIONS) != fingerprint(two, OPTIONS)


def test_absolute_and_relative_paths_agree() -> None:
    """
    The fingerprint must not depend on where the project was cloned
    """

    one = _alert(None, exception=_fake_exc(frames=(('/srv/app/src/orders.py', 'run'),)))
    two = _alert(None, exception=_fake_exc(frames=(('src/orders.py', 'run'),)))

    assert fingerprint(one, OPTIONS) == fingerprint(two, OPTIONS)


def test_stdlib_and_logalert_frames_are_stripped() -> None:
    """
    Logging frames are the same for everyone and only blur the grouping
    """

    import logging as stdlib_logging

    import logalert.handler

    logging_file = str(Path(stdlib_logging.__file__))
    logalert_file = str(Path(logalert.handler.__file__))
    plain = _fake_exc(frames=(('orders.py', 'handle'),))

    noisy = ExceptionInfo(
        type_name='ValueError',
        module='builtins',
        message='',
        stacktrace=Stacktrace(
            frames=(
                Frame(filename=logging_file, lineno=1, function='error', module='logging'),
                Frame(
                    filename=logalert_file,
                    lineno=2,
                    function='emit',
                    module='logalert.handler',
                ),
                Frame(filename='orders.py', lineno=3, function='handle'),
            )
        ),
    )

    assert fingerprint(_alert(None, exception=plain), OPTIONS) == fingerprint(
        _alert(None, exception=noisy), OPTIONS
    )


def test_message_is_used_only_without_traceback() -> None:
    """
    With no traceback, the text is the only thing that tells events apart
    """

    first = _alert(None, message='payment timeout', exception=None)
    second = _alert(None, message='authorization failure', exception=None)

    assert fingerprint(first, OPTIONS) != fingerprint(second, OPTIONS)

    # Checking that the text stops mattering as soon as there is a traceback
    third = _alert(None, message='payment timeout', exception=_fake_exc())
    fourth = _alert(None, message='authorization failure', exception=_fake_exc())

    assert fingerprint(third, OPTIONS) == fingerprint(fourth, OPTIONS)


def test_logger_separates_otherwise_identical_errors() -> None:
    """
    The same error from two places is two places, and they are worth telling
    apart
    """

    one = _alert(None, exception=_fake_exc(), logger='orders')
    two = _alert(None, exception=_fake_exc(), logger='billing')

    assert fingerprint(one, OPTIONS) != fingerprint(two, OPTIONS)


def test_user_override_wins() -> None:
    """
    An explicit fingerprint replaces the computed one
    """

    one = _alert(None, exception=_fake_exc(), fingerprint=('my-group',))
    two = _alert(None, exception=_fake_exc('TypeError'), fingerprint=('my-group',))

    assert fingerprint(one, OPTIONS) == fingerprint(two, OPTIONS)


def test_version_is_part_of_the_hash() -> None:
    """
    A change of algorithm must not accidentally collide with old keys
    """

    assert FINGERPRINT_VERSION.startswith('logalert-fp-')
    assert fingerprint(_alert(None, exception=_fake_exc()), OPTIONS) != fingerprint(
        _alert(None, exception=_fake_exc(), fingerprint=(FINGERPRINT_VERSION,)), OPTIONS
    )


def test_exception_group_order_does_not_matter() -> None:
    """
    The order in which `except*` clauses fire must not affect grouping
    """

    first = ExceptionInfo(
        type_name='ExceptionGroup',
        module='builtins',
        message='',
        is_group=True,
        leaves=(_fake_exc('ValueError'), _fake_exc('TypeError')),
    )

    second = ExceptionInfo(
        type_name='ExceptionGroup',
        module='builtins',
        message='',
        is_group=True,
        leaves=(_fake_exc('TypeError'), _fake_exc('ValueError')),
    )

    assert fingerprint(_alert(None, exception=first), OPTIONS) == fingerprint(
        _alert(None, exception=second), OPTIONS
    )


class TestNormalizeMessage:
    """
    Normalization: the volatile parts of a message become placeholders
    """

    def test_numbers_become_placeholder(self) -> None:
        """
        Numbers collapse into one placeholder
        """

        assert normalize_message('order 12345 not found') == normalize_message(
            'order 98765 not found'
        )

    def test_uuids(self) -> None:
        """
        UUIDs collapse into one placeholder
        """

        assert normalize_message('id=550e8400-e29b-41d4-a716-446655440000') == normalize_message(
            'id=123e4567-e89b-12d3-a456-426614174000'
        )

    def test_hex_addresses(self) -> None:
        """
        Hex addresses collapse into one placeholder
        """

        assert normalize_message('object 0x7f3a1b2c') == normalize_message('object 0xdeadbeef')

    def test_quoted_values(self) -> None:
        """
        Quoted values collapse into one placeholder
        """

        assert normalize_message("no key 'alpha'") == normalize_message("no key 'beta'")

    def test_length_is_capped(self) -> None:
        """
        The normalized message is capped in length
        """

        assert len(normalize_message('日' * 5000)) == 256


__all__ = (
    'test_logger_separates_otherwise_identical_errors',
    'test_same_basename_in_different_packages_differs',
    'test_exception_text_does_not_affect_fingerprint',
    'test_stdlib_and_logalert_frames_are_stripped',
    'test_line_numbers_do_not_affect_fingerprint',
    'test_message_is_used_only_without_traceback',
    'test_exception_group_order_does_not_matter',
    'test_absolute_and_relative_paths_agree',
    'test_different_exception_types_differ',
    'test_version_is_part_of_the_hash',
    'test_different_frames_differ',
    'test_user_override_wins',
    'TestNormalizeMessage',
)
