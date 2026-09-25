"""
Logalert Cli

The ``logalert`` command: check, test send, worker, id lookup

:copyright: (c) 2025 Aiko Sora
"""

import argparse
import contextlib
import sys
from collections.abc import Sequence
from typing import Any

from ._version import __version__
from .exceptions import LogAlertError
from .options import Options
from .types import Alert


def main(argv: Sequence[str] | None = None) -> int:
    """
    Run the selected subcommand and return its exit code

    :param argv: The arguments to parse; ``sys.argv[1:]`` when omitted

    :return int:
    """

    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        return int(args.handler(args))

    except LogAlertError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2

    except KeyboardInterrupt:
        return 130


def _build_parser() -> argparse.ArgumentParser:
    """
    Build the parser with the connection flags and the subcommands

    :return argparse.ArgumentParser:
    """

    # The connection flags live on a parent parser, so they are accepted both
    # before and after the subcommand. Without it `logalert check
    # --telegram-token ...` is rejected as an unrecognised argument, which is
    # the natural way to write it.
    #
    # ``SUPPRESS`` as the default is what makes both positions work: a subparser
    # resets its own defaults, so a plain default would wipe the value the
    # top-level parser had already read. With SUPPRESS the attribute is only
    # ever set when the flag is given.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--telegram-token', default=argparse.SUPPRESS, help='Telegram bot token')
    common.add_argument('--telegram-chat-id', default=argparse.SUPPRESS, help='chat id')
    common.add_argument(
        '--telegram-thread-id', type=int, default=argparse.SUPPRESS, help='forum topic id'
    )
    common.add_argument('--matrix-homeserver', default=argparse.SUPPRESS, help='homeserver URL')
    common.add_argument(
        '--matrix-room-id', default=argparse.SUPPRESS, help='room id, starts with !'
    )
    common.add_argument(
        '--matrix-access-token', default=argparse.SUPPRESS, help='Matrix access token'
    )
    common.add_argument(
        '--redis-url', default=argparse.SUPPRESS, help='Redis URL for the durable queue'
    )
    common.add_argument(
        '--project', default=argparse.SUPPRESS, help='project name, isolates the keys in Redis'
    )
    common.add_argument(
        '--debug', action='store_true', default=argparse.SUPPRESS, help='verbose library output'
    )

    parser = argparse.ArgumentParser(
        prog='logalert',
        description='Check the logalert setup and maintain alert delivery',
        parents=[common],
    )

    parser.add_argument('--version', action='version', version=f'logalert {__version__}')

    sub = parser.add_subparsers(dest='command', required=True)

    def _sub(name: str, help: str) -> argparse.ArgumentParser:
        """
        Add a subcommand that also accepts the connection flags

        :param name: Subcommand name
        :param help: One-line description for the help output

        :return argparse.ArgumentParser:
        """

        return sub.add_parser(name, help=help, parents=[common])

    check = _sub('check', 'check the configuration without sending anything')
    check.set_defaults(handler=_cmd_check)

    test = _sub('test', 'send a test alert')
    test.set_defaults(handler=_cmd_test)

    worker = _sub('worker', 'start the Redis consumer')
    worker.add_argument('--once', action='store_true', help='do a single pass and exit')
    worker.add_argument('--name', help='consumer name')
    worker.set_defaults(handler=_cmd_worker)

    topics = _sub('topics', 'look up chat ids and forum topic ids')
    topics.set_defaults(handler=_cmd_topics)

    version = _sub('version', help='show the version')
    version.set_defaults(handler=lambda _args: _print_version())

    return parser


def _options_from_args(args: argparse.Namespace, *, with_debug: bool = True) -> Options:
    """
    Build the :class:`Options` from the parsed arguments

    :param args: The parsed command line arguments
    :param with_debug: Whether ``--debug`` is honoured, or forced off

    :return Options:
    """

    telegram: dict[str, Any] = {}

    if getattr(args, 'telegram_token', None):
        telegram['token'] = getattr(args, 'telegram_token', None)

    if getattr(args, 'telegram_chat_id', None):
        telegram['chat_id'] = getattr(args, 'telegram_chat_id', None)

    if getattr(args, 'telegram_thread_id', None):
        telegram['message_thread_id'] = getattr(args, 'telegram_thread_id', None)

    matrix: dict[str, Any] = {}

    if getattr(args, 'matrix_homeserver', None):
        matrix['homeserver'] = getattr(args, 'matrix_homeserver', None)

    if getattr(args, 'matrix_room_id', None):
        matrix['room_id'] = getattr(args, 'matrix_room_id', None)

    if getattr(args, 'matrix_access_token', None):
        matrix['access_token'] = getattr(args, 'matrix_access_token', None)

    return Options(
        telegram=telegram or None,
        matrix=matrix or None,
        redis_url=getattr(args, 'redis_url', None),
        project=getattr(args, 'project', None),
        debug=with_debug and getattr(args, 'debug', False),
    )


def _build_sinks(options: Options) -> list[Any]:
    """
    Build the configured sinks and hand each of them the options

    :param options: The resolved options

    :return list[Any]:
    """

    from .client import build_sinks

    sinks = build_sinks(options)

    for sink in sinks:
        binder = getattr(sink, 'bind', None)

        if callable(binder):
            binder(options)

    return sinks


def _cmd_check(args: argparse.Namespace) -> int:
    """
    Check every sink and print a report

    :param args: The parsed command line arguments

    :return int:
    """

    options = _options_from_args(args)

    try:
        sinks = _build_sinks(options)

    except LogAlertError as exc:
        print(f'✗ {exc}', file=sys.stderr)
        return 2

    if not sinks:
        print(
            'no sinks configured. Pass --telegram-token/--telegram-chat-id or --matrix-*',
            file=sys.stderr,
        )

        return 2

    failed = 0

    for sink in sinks:
        name = getattr(sink, 'name', type(sink).__name__)

        try:
            sink.healthcheck()

        except Exception as exc:
            failed += 1
            print(f'✗ {name}: {exc}')

        else:
            print(f'✓ {name}: options accepted')

        finally:
            with contextlib.suppress(Exception):
                sink.close()

    if options.redis_url:
        failed += _check_redis(options)

    return 1 if failed else 0


def _check_redis(options: Options) -> int:
    """
    Check the Redis connection and version

    :param options: The resolved options, holding the Redis URL

    :return int:
    """

    from .transport.redis import build_client
    from .worker import _check_redis_version

    assert options.redis_url is not None

    try:
        client = build_client(options.redis_url)
        _check_redis_version(client)

    except SystemExit as exc:
        print(f'✗ redis: {exc}')
        return 1

    except Exception as exc:
        print(f'✗ redis: {exc}')
        return 1

    else:
        print('✓ redis: connection and version are fine')
        return 0


def _cmd_test(args: argparse.Namespace) -> int:
    """
    Send a synthetic alert - exercise the whole path

    :param args: The parsed command line arguments

    :return int:
    """

    options = _options_from_args(args)
    sinks = _build_sinks(options)

    if not sinks:
        print('no sinks configured', file=sys.stderr)
        return 2

    alert = _synthetic_alert()
    failures = 0

    for sink in sinks:
        name = getattr(sink, 'name', type(sink).__name__)

        try:
            sink.send(alert, key=alert.event_id, timeout=options.send_timeout)

        except Exception as exc:
            failures += 1
            print(f'✗ {name}: {exc}', file=sys.stderr)

        else:
            print(f'✓ {name}: test alert sent')

        finally:
            with contextlib.suppress(Exception):
                sink.close()

    return 1 if failures else 0


def _synthetic_alert() -> Alert:
    """
    A plausible alert, so the test send also exercises the formatting

    :return Alert:
    """

    from datetime import UTC, datetime

    from .capture import exception_to_info
    from .types import new_event_id

    try:
        raise ValueError('this logalert test alert is not an application error')

    except ValueError as exc:
        info = exception_to_info(exc, Options())

    return Alert(
        event_id=new_event_id(),
        timestamp=datetime.now(UTC),
        level='error',
        source='manual',
        logger='logalert.cli',
        message='configuration check',
        exception=info,
        environment='production',
        server_name=None,
    )


def _cmd_worker(args: argparse.Namespace) -> int:
    """
    Run the Redis consumer

    :param args: The parsed command line arguments

    :return int:
    """

    from .worker import run_worker

    options = _options_from_args(args)

    if not options.redis_url:
        print('the worker needs --redis-url', file=sys.stderr)
        return 2

    if not options.project:
        print('the worker needs --project', file=sys.stderr)
        return 2

    return run_worker(
        redis_url=options.redis_url,
        project=options.project,
        options=options,
        consumer_name=args.name,
        run_forever=not args.once,
    )


def _cmd_topics(args: argparse.Namespace) -> int:
    """
    Show chat ids and topic ids from the latest updates

    :param args: The parsed command line arguments

    :return int:
    """

    if not getattr(args, 'telegram_token', None):
        print('--telegram-token is required', file=sys.stderr)
        return 2

    from .sinks.base import require

    httpx = require('httpx', 'telegram')

    try:
        response = httpx.get(
            f'https://api.telegram.org/bot{getattr(args, "telegram_token", None)}/getUpdates',
            timeout=10.0,
        )
        payload = response.json()

    except Exception as exc:
        print(f'could not fetch updates: {exc}', file=sys.stderr)
        return 1

    if not payload.get('ok'):
        print(f'Telegram returned an error: {payload.get("description")}', file=sys.stderr)
        return 1

    seen: set[tuple[Any, Any]] = set()

    for update in payload.get('result', []):
        for key in ('message', 'channel_post', 'edited_message'):
            message = update.get(key)

            if not message:
                continue

            chat = message.get('chat', {})
            pair = (chat.get('id'), message.get('message_thread_id'))

            if pair in seen:
                continue

            seen.add(pair)
            title = chat.get('title') or chat.get('username') or chat.get('first_name')
            thread = message.get('message_thread_id')
            line = f'chat_id={chat.get("id")}  ({chat.get("type")}) {title}'

            if thread:
                line += f'  message_thread_id={thread}'

            print(line)

    if not seen:
        print(
            'no updates. Message the bot in a chat or a topic and try again',
            file=sys.stderr,
        )
        return 1

    return 0


def _print_version() -> int:
    """
    Print the version of the installed library

    :return int:
    """

    print(f'logalert {__version__}')

    return 0


__all__ = ('main',)


if __name__ == '__main__':
    sys.exit(main())
