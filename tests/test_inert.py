"""
Logalert Tests Inert

Nothing happens until `init()` is called - no network module is imported, no
socket is created.

:copyright: (c) 2025 Aiko Sora
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parent.parent / 'src')

NETWORK_MODULES = (
    'socket',
    'ssl',
    'selectors',
    'subprocess',
    'asyncio',
    'httpx',
    'httpcore',
    'redis',
    'urllib3',
    'nio',
)


def _run_child(code: str) -> dict[str, object]:
    """
    Run a snippet in a clean interpreter and read its last output line

    :param code: The code to run, which has to print a JSON object

    :return dict[str, object]:
    """

    env = {**os.environ, 'PYTHONPATH': SRC}
    result = subprocess.run(
        [sys.executable, '-c', code], capture_output=True, text=True, env=env, timeout=60
    )

    assert result.returncode == 0, result.stderr

    return json.loads(result.stdout.strip().splitlines()[-1])


def test_import_does_not_pull_network_modules() -> None:
    """
    `import logalert` pulls in no network module at all
    """

    data = _run_child(
        'import json, sys;'
        'before = set(sys.modules);'
        'import logalert;'
        "print(json.dumps({'new': sorted(set(sys.modules) - before)}))"
    )

    leaked = sorted(set(data['new']) & set(NETWORK_MODULES))  # type: ignore[arg-type]

    assert not leaked, f'import logalert pulled in network modules: {leaked}'


def test_public_names_are_importable_in_child() -> None:
    """
    The public API is reachable from a clean interpreter
    """

    data = _run_child(
        'import json, logalert;'
        "print(json.dumps({'v': logalert.__version__, 'n': len(logalert.__all__)}))"
    )

    # Checking that the whole public surface is there
    assert data['n'] > 20  # type: ignore[operator]


def test_init_does_not_create_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ``init()`` and capture create no sockets
    """

    import socket

    from conftest import RecordingSink

    from logalert.client import Client
    from logalert.options import Options

    attempts: list[object] = []

    def guard(*args: object, **kwargs: object) -> None:
        attempts.append(args)

        raise AssertionError('logalert created a socket even though it was never asked to')

    monkeypatch.setattr(socket, 'socket', guard)
    sink = RecordingSink()
    client = Client(Options(sinks=[sink], dedup_window=0))

    try:
        for _ in range(100):
            client.capture(ValueError('inertness check'))

        client.flush(5.0)

    finally:
        client.close()

    # Checking that the whole path ran without touching a socket
    assert not attempts
    assert len(sink.sent) == 100


def test_capture_without_init_is_silent() -> None:
    """
    Before ``init()`` the public functions do not fail and do nothing
    """

    import logalert

    assert logalert.is_initialized() is False
    assert logalert.get_client() is None

    logalert.tag('k', 'v')
    logalert.context('c', {'a': 1})
    logalert.breadcrumb('nothing is happening')

    assert logalert.flush(0.1) is True


__all__ = (
    'test_import_does_not_pull_network_modules',
    'test_public_names_are_importable_in_child',
    'test_capture_without_init_is_silent',
    'test_init_does_not_create_sockets',
)
