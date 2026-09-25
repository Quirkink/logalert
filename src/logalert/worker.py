"""
Logalert Worker

The Redis consumer: reads the stream and delivers the alerts

:copyright: (c) 2025 Aiko Sora
"""

import contextlib
import logging
import signal
import threading

from ._delivery import Deliverer
from .options import Options

_log = logging.getLogger('logalert.debug')


def run_worker(
    *,
    redis_url: str,
    project: str,
    options: Options | None = None,
    consumer_name: str | None = None,
    run_forever: bool = True,
) -> int:
    """
    Start the consumer. Returns the exit code for the CLI

    :param redis_url: Redis connection URL
    :param project: Project name the stream keys are namespaced with
    :param options: Full options object; built from the URL and the project when
        omitted
    :param consumer_name: Name of this consumer inside the group; generated when
        omitted
    :param run_forever: Run the main loop, or drain what is pending and return

    :return int:
    """

    from .client import build_sinks
    from .transport.redis import StreamConsumer, build_client

    opts = options or Options(redis_url=redis_url, project=project)

    if not opts.project:
        opts.project = project

    client = build_client(redis_url)
    _check_redis_version(client)
    sinks = build_sinks(opts)

    if not sinks:
        raise SystemExit(
            'no sinks configured - the worker has nowhere to deliver. '
            'Set telegram=..., matrix=... or sinks=[...]'
        )

    for sink in sinks:
        binder = getattr(sink, 'bind', None)

        if callable(binder):
            binder(opts)

    deliverer = Deliverer(sinks, opts)
    consumer = StreamConsumer(
        client, opts.project or project, deliverer, opts, consumer_name=consumer_name
    )

    stop = threading.Event()
    _install_signal_handlers(stop)

    _log.info('worker started: group %s, stream logalert:%s:alerts', consumer.group, opts.project)

    try:
        if run_forever:
            consumer.run(stop)

            return 0

        consumer.ensure_group()
        consumer.claim_stale()
        consumer.process_new(stop)

        return 0

    except KeyboardInterrupt:
        return 0

    finally:
        stop.set()
        deliverer.close()

        with contextlib.suppress(Exception):
            client.close()


def stop_worker(stop: threading.Event) -> None:
    """
    Ask the consumer loop to stop

    :param stop: The event the loop checks between messages

    :return None:
    """

    stop.set()


def _install_signal_handlers(stop: threading.Event) -> None:
    """
    Shutdown on SIGTERM/SIGINT

    :param stop: The event the signal handler sets

    :return None:
    """

    def handler(signum: int, _frame: object) -> None:
        _log.info('received signal %s, stopping', signum)

        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, handler)


def _check_redis_version(client: object) -> None:
    """
    Make sure Redis can do XAUTOCLAIM

    :param client: The Redis client to ask for the server version

    :return None:
    """

    try:
        info = client.info('server')  # type: ignore[attr-defined]
    except Exception as exc:
        raise SystemExit(f'could not connect to Redis: {exc}') from exc

    version = str(info.get('redis_version', '0'))

    try:
        major, minor = (int(part) for part in version.split('.')[:2])
    except ValueError:
        return

    if (major, minor) < (6, 2):
        raise SystemExit(
            f'Redis 6.2 or newer is required (found {version}): the worker uses XAUTOCLAIM '
            f'to claim messages left hanging on a consumer that died'
        )


__all__ = (
    'stop_worker',
    'run_worker',
)
