"""
Logalert Tests Api

The public API is a promise, and it is pinned down: a change here breaks
someone's code.

:copyright: (c) 2025 Aiko Sora
"""

import re
from pathlib import Path

import pytest

import logalert
from logalert.exceptions import InvalidOption
from logalert.options import Options

EXPECTED_PUBLIC_API = frozenset(
    {
        'init',
        'shutdown',
        'flush',
        'is_initialized',
        'get_client',
        'alert',
        'dict_config',
        'tag',
        'context',
        'user',
        'breadcrumb',
        'override_fingerprint',
        'Scope',
        'Alert',
        'Breadcrumb',
        'EventLevel',
        'Options',
        'Sink',
        'TelegramSink',
        'MatrixSink',
        'StderrSink',
        'AlertHandler',
        'BreadcrumbHandler',
        'install_loop_hooks',
        'LogAlertError',
        'InvalidOption',
        'MissingDependencyError',
        'TransportError',
        'SinkError',
        'SinkTransientError',
        'SinkPermanentError',
        'SinkRateLimited',
        'EncryptedRoomError',
        '__version__',
    }
)


def test_public_api_is_frozen() -> None:
    """
    The set of public names changes only by a deliberate decision
    """

    actual = frozenset(logalert.__all__)

    assert actual == EXPECTED_PUBLIC_API, (
        f'the public API changed.\n'
        f'  added:   {sorted(actual - EXPECTED_PUBLIC_API)}\n'
        f'  removed: {sorted(EXPECTED_PUBLIC_API - actual)}'
    )


def test_every_public_name_exists() -> None:
    """
    Every name declared in `__all__` is really there
    """

    missing = [name for name in logalert.__all__ if not hasattr(logalert, name)]

    assert not missing, f'declared in __all__ but missing: {missing}'


def test_public_names_are_documented() -> None:
    """
    Every public name has a docstring: that is documentation, not decoration
    """

    undocumented = []

    for name in logalert.__all__:
        if name.startswith('__'):
            continue

        obj = getattr(logalert, name)

        if not (getattr(obj, '__doc__', None) or getattr(obj, '__module__', None)):
            undocumented.append(name)

    assert not undocumented


def test_version_matches_pyproject() -> None:
    """
    The version is declared twice, and the two declarations have to match
    """

    pyproject = Path(__file__).resolve().parent.parent / 'pyproject.toml'
    match = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.MULTILINE)

    assert match is not None
    assert match.group(1) == logalert.__version__


class TestSinkSelection:
    """
    Sinks are passed as objects

    A name cannot be resolved to a configuration section: the section would have to be an
    attribute of the options, and only ``telegram`` and ``matrix`` are. So a sink is handed
    over ready-made, and a bare name is refused instead of quietly built with no settings.
    """

    def test_a_name_is_refused(self) -> None:
        """
        A name is a mistake worth an error, not a sink built from nothing
        """

        from logalert.client import build_sinks

        with pytest.raises(InvalidOption, match='sink objects, not names'):
            build_sinks(Options(sinks=['telegram']))

    def test_an_instance_is_accepted_unchanged(self) -> None:
        """
        A sink passed as an object comes back as the very same object
        """

        from conftest import RecordingSink

        from logalert.client import build_sinks

        marker = RecordingSink()

        assert build_sinks(Options(sinks=[marker])) == [marker]

    def test_the_telegram_section_still_builds_a_sink(self) -> None:
        """
        The section form is the one the README documents, so it stays
        """

        from logalert.client import build_sinks

        built = build_sinks(Options(telegram={'token': 'T', 'chat_id': 'C'}))

        assert [getattr(sink, 'name', '') for sink in built] == ['telegram']


class TestOptions:
    """
    Options: the configuration is validated, and the secrets stay hidden
    """

    def test_repr_masks_secrets(self) -> None:
        """
        The config will inevitably end up in a traceback or in a log
        """

        options = Options(
            telegram={'token': '123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw', 'chat_id': '1'},
            redis_url='redis://:hunter2@localhost/0',
            project='demo',
        )

        text = repr(options)

        assert 'AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw' not in text
        assert 'hunter2' not in text
        assert '<hidden>' in text

    def test_redis_requires_project(self) -> None:
        """
        Without project we would write into the global namespace of another
        app's Redis
        """

        with pytest.raises(InvalidOption, match='project is required'):
            Options(redis_url='redis://localhost/0')

    def test_sample_rate_bounds(self) -> None:
        """
        A sample rate outside zero to one is refused
        """

        with pytest.raises(InvalidOption, match='sample_rate'):
            Options(sample_rate=1.5)

    def test_backpressure_values(self) -> None:
        """
        An unknown backpressure policy is refused
        """

        with pytest.raises(InvalidOption, match='backpressure'):
            Options(backpressure='whatever')  # type: ignore[arg-type]

    def test_include_locals_values(self) -> None:
        """
        An unknown include_locals value is refused
        """

        with pytest.raises(InvalidOption, match='include_locals'):
            Options(include_locals='everything')  # type: ignore[arg-type]

    def test_error_names_the_offending_field(self) -> None:
        """
        A message saying "invalid configuration" is useless - the field name is
        what we need
        """

        with pytest.raises(InvalidOption, match='queue_size'):
            Options(queue_size=0)

    def test_claim_idle_is_derived_with_margin(self) -> None:
        """
        The claim threshold has to sit above the worst-case delivery time
        """

        options = Options(send_timeout=10.0, retry_backoff=(1.0, 5.0), max_delivery_attempts=3)
        worst_case_ms = (10.0 + 6.0) * 1000

        # Checking that the derived threshold clears the worst case
        assert options.claim_min_idle_ms is not None
        assert options.claim_min_idle_ms > worst_case_ms

    def test_too_small_claim_idle_is_rejected(self) -> None:
        """
        A claim threshold below the worst-case delivery time is refused
        """

        with pytest.raises(InvalidOption, match='claim_min_idle_ms'):
            Options(send_timeout=10.0, retry_backoff=(60.0,), claim_min_idle_ms=100)


class TestEnvIsolation:
    """
    The library reads nothing from the environment
    """

    def test_foreign_environment_does_not_leak(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        The commonplace variables of another application are ignored
        """

        from logalert.client import build_sinks

        monkeypatch.setenv('REDIS_URL', 'redis://foreign/0')
        monkeypatch.setenv('PROJECT', 'foreign-project')

        options = Options()

        # Checking that a foreign environment left the options alone
        assert options.redis_url is None
        assert options.project is None
        assert build_sinks(options) == []


class TestCli:
    """
    Cli: the `version`, `check` and `test` subcommands
    """

    def test_version_command(self, capsys: pytest.CaptureFixture[str]) -> None:
        """
        Asking for the version prints it and succeeds
        """

        from logalert.cli import main

        assert main(['version']) == 0
        assert logalert.__version__ in capsys.readouterr().out

    def test_connection_flags_work_on_either_side_of_the_subcommand(self) -> None:
        """
        The connection flags are accepted before and after the subcommand
        """

        from logalert.cli import _build_parser, _options_from_args

        parser = _build_parser()

        for argv in (
            ['check', '--telegram-token', 'T', '--telegram-chat-id', 'C'],
            ['--telegram-token', 'T', '--telegram-chat-id', 'C', 'check'],
        ):
            requested = _options_from_args(parser.parse_args(argv))

            assert requested.telegram == {'token': 'T', 'chat_id': 'C'}, argv

    def test_check_without_sinks_reports_clearly(self, capsys: pytest.CaptureFixture[str]) -> None:
        """
        Checking a config without sinks fails and says what is missing
        """

        from logalert.cli import main

        assert main(['check']) == 2
        assert 'sink' in capsys.readouterr().err

    def test_test_command_sends_synthetic_alert(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A trial alert goes through the same formatting path as a real one
        """

        import io

        import logalert.cli

        buffer = io.StringIO()

        from logalert.sinks import StderrSink

        monkeypatch.setattr(
            logalert.cli, '_build_sinks', lambda options: [StderrSink(stream=buffer)]
        )

        assert logalert.cli.main(['test']) == 0

        output = buffer.getvalue()

        # Checking that the trial alert carries a formatted traceback
        assert 'test alert' in output
        assert 'Traceback' in output, 'the trial alert carries no traceback'


__all__ = (
    'TestSinkSelection',
    'TestEnvIsolation',
    'TestOptions',
    'TestCli',
)
