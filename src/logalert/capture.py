"""
Logalert Capture

Turning live exceptions into :class:`Alert` and installing hooks

:copyright: (c) 2025 Aiko Sora
"""

import atexit
import contextlib
import linecache
import logging
import os
import sys
import threading
from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol

from .options import Options
from .types import Alert, ExceptionInfo, Frame, Stacktrace

if TYPE_CHECKING:
    import asyncio


_log = logging.getLogger('logalert.debug')

MAX_CAUSE_DEPTH = 5
MAX_GROUP_LEAVES = 10
_TRUNCATED = '...'

_STDLIB_ROOTS: tuple[str, ...] | None = None


class Capturer(Protocol):
    """
    The minimum the hooks need from a client
    """

    def capture(
        self,
        exc: BaseException | None = None,
        *,
        message: str | None = None,
        source: str = 'manual',
        mechanism: str = 'manual',
        level: str | None = None,
    ) -> str | None:
        """
        Capture an event

        :param exc: The exception to capture
        :param message: The event text, required when there is no exception
        :param source: Where the event came from - one of the ``EventSource``
            values
        :param mechanism: What produced the event
        :param level: The level name

        :return str | None:
        """

        ...


def _safe_str(value: object, limit: int) -> str:
    """
    A string representation that survives a hostile ``__str__``

    :param value: The value to render
    :param limit: The maximum length of the result

    :return str:
    """

    try:
        text = str(value)

    except Exception:
        try:
            text = repr(value)

        except Exception:
            return '<value that cannot be serialized>'

    if len(text) > limit:
        return text[: limit - len(_TRUNCATED)] + _TRUNCATED

    return text


def _stdlib_roots() -> tuple[str, ...]:
    """
    The directories holding the standard library and the interpreter itself

    :return tuple[str, ...]:
    """

    import sysconfig

    roots = {sysconfig.get_paths().get('stdlib', ''), sys.prefix, sys.base_prefix}

    return tuple(os.path.realpath(r) for r in roots if r)


def _stdlib_roots_cached() -> tuple[str, ...]:
    """
    The same roots, resolved once per process

    :return tuple[str, ...]:
    """

    global _STDLIB_ROOTS

    if _STDLIB_ROOTS is None:
        _STDLIB_ROOTS = _stdlib_roots()

    return _STDLIB_ROOTS


def _is_in_app(filename: str) -> bool:
    """
    A frame from application code rather than stdlib or site-packages

    :param filename: The file the frame points at

    :return bool:
    """

    if not filename or filename.startswith('<'):
        return False

    path = os.path.realpath(filename)

    if 'site-packages' in path or 'dist-packages' in path:
        return False

    return not any(path.startswith(root + os.sep) for root in _stdlib_roots_cached())


def _source_line(filename: str, lineno: int) -> str | None:
    """
    The source line a frame points at, stripped, or ``None``

    :param filename: The file the line is read from
    :param lineno: The line number to read

    :return str | None:
    """

    if filename.startswith('<') or lineno <= 0:
        return None

    line = linecache.getline(filename, lineno)

    return line.strip() or None if line else None


def _collect_locals(frame: Any, options: Options) -> dict[str, str]:
    """
    Collect the local variables of a frame

    :param frame: The frame whose locals are read
    :param options: Configuration deciding what may be collected

    :return dict[str, str]:
    """

    result: dict[str, str] = {}
    safe_only = options.include_locals == 'safe'

    for name, value in list(frame.f_locals.items()):
        if name.startswith('__'):
            continue

        if safe_only and not isinstance(value, (int, float, bool, str, bytes, type(None))):
            continue

        result[name] = _safe_str(value, options.max_value_length)

    return result


def _frame_from(frame: Any, lineno: int, options: Options) -> Frame:
    """
    Build a frame object from a live stack frame

    :param frame: Live frame from the traceback
    :param lineno: Line number the frame is currently at
    :param options: Resolved options

    :return Frame:
    """

    code = frame.f_code
    filename = code.co_filename
    module = frame.f_globals.get('__name__')

    return Frame(
        filename=filename,
        lineno=lineno,
        function=code.co_name,
        module=module if isinstance(module, str) else None,
        in_app=_is_in_app(filename),
        source_line=_source_line(filename, lineno) if options.include_source_context else None,
        locals=_collect_locals(frame, options) if options.include_locals else {},
    )


def _frames_from_traceback(tb: TracebackType | None, options: Options) -> tuple[Frame, ...]:
    """
    Build frame objects from a live traceback chain

    :param tb: The traceback to walk - ``None`` gives an empty result
    :param options: Configuration deciding how much is captured

    :return tuple[Frame, ...]:
    """

    frames: list[Frame] = []

    while tb is not None:
        frames.append(_frame_from(tb.tb_frame, tb.tb_lineno, options))
        tb = tb.tb_next

    return tuple(frames[-options.max_frames :])


def exception_to_info(
    exc: BaseException,
    options: Options,
    *,
    depth: int = 0,
) -> ExceptionInfo:
    """
    Build an :class:`ExceptionInfo` from a live exception

    :param exc: The exception to format
    :param options: Configuration deciding how much is captured
    :param depth: The current cause and context depth

    :return ExceptionInfo:
    """

    exc_type = type(exc)
    module = getattr(exc_type, '__module__', '') or ''

    info = ExceptionInfo(
        type_name=exc_type.__qualname__,
        module=module,
        message=_safe_str(exc, options.max_value_length),
        stacktrace=Stacktrace(frames=_frames_from_traceback(exc.__traceback__, options)),
    )

    if isinstance(exc, BaseExceptionGroup):
        info = _with_group(info, exc, options, depth)

    if depth < MAX_CAUSE_DEPTH:
        cause = exc.__cause__
        context = exc.__context__
        handled: set[int] = set()

        if cause is not None:
            info = _replace_cause(info, exception_to_info(cause, options, depth=depth + 1))
            handled.add(id(cause))

        if context is not None and id(context) not in handled:
            info = _replace_context(info, exception_to_info(context, options, depth=depth + 1))

    return info


def _with_group(
    info: ExceptionInfo, exc: BaseExceptionGroup[BaseException], options: Options, depth: int
) -> ExceptionInfo:
    """
    Flatten an ``ExceptionGroup`` into a flat list of leaves

    :param info: The event built for the group itself
    :param exc: The group the leaves are taken from
    :param options: Configuration deciding how much is captured
    :param depth: The current cause and context depth

    :return ExceptionInfo:
    """

    from dataclasses import replace

    leaves: list[ExceptionInfo] = []

    for sub in exc.exceptions[:MAX_GROUP_LEAVES]:
        if isinstance(sub, BaseException):
            leaves.append(exception_to_info(sub, options, depth=depth + 1))

    return replace(info, is_group=True, leaves=tuple(leaves))


def _replace_cause(info: ExceptionInfo, cause: ExceptionInfo) -> ExceptionInfo:
    """
    Attach a cause to an event

    :param info: The event the cause is attached to
    :param cause: The event built for ``exc.__cause__``

    :return ExceptionInfo:
    """

    from dataclasses import replace

    return replace(info, cause=cause)


def _replace_context(info: ExceptionInfo, context: ExceptionInfo) -> ExceptionInfo:
    """
    Attach an implicit context to an event

    :param info: The event the context is attached to
    :param context: The event built for ``exc.__context__``

    :return ExceptionInfo:
    """

    from dataclasses import replace

    return replace(info, context=context)


def should_ignore(exc: BaseException, options: Options) -> bool:
    """
    Decide whether this exception should be passed over in silence

    :param exc: The exception to check
    :param options: The configuration holding the ignore list

    :return bool:
    """

    ignored = set(options.ignore_errors)

    if options.capture_keyboard_interrupt:
        ignored -= {'KeyboardInterrupt', 'builtins.KeyboardInterrupt'}

    else:
        ignored.add('KeyboardInterrupt')

    if not ignored:
        return False

    current: BaseException | None = exc
    seen = 0

    while current is not None and seen < MAX_CAUSE_DEPTH:
        exc_type = type(current)

        if exc_type.__name__ in ignored or exc_type.__qualname__ in ignored:
            return True

        qualified = f'{exc_type.__module__}.{exc_type.__qualname__}'

        if qualified in ignored:
            return True

        current = current.__cause__ or current.__context__
        seen += 1

    return False


class HookHandle:
    """
    A handle for removing the hooks that were installed
    """

    def __init__(self, undo: list[Callable[[], None]]) -> None:
        """
        Keep the callbacks that put the hooks back the way they were

        :param undo: Callbacks to run, in reverse order, when the hooks are
            removed

        :return None:
        """

        self._undo = undo
        self._done = False

    def uninstall(self) -> None:
        """
        Remove the hooks that were installed

        :return None:
        """

        if self._done:
            return

        self._done = True

        for action in reversed(self._undo):
            with contextlib.suppress(Exception):
                action()


def _capture_safely(
    capturer: Capturer,
    exc: BaseException,
    options: Options,
    *,
    source: str,
    mechanism: str,
    level: str,
) -> None:
    """
    Capture an exception from a hook, swallowing anything that goes wrong

    :param capturer: The object whose ``capture`` is called
    :param exc: The exception to capture
    :param options: The configuration the capture is made with
    :param source: Where the event came from - one of the ``EventSource`` values
    :param mechanism: What produced the event
    :param level: The level name

    :return None:
    """

    try:
        if should_ignore(exc, options):
            return

        capturer.capture(exc, source=source, mechanism=mechanism, level=level)

    except Exception:
        _log.debug('failed to capture exception from %s', mechanism, exc_info=True)


def install_hooks(capturer: Capturer, options: Options) -> HookHandle:
    """
    Install every hook and return a handle for removing them

    :param capturer: The object whose ``capture`` the hooks call
    :param options: Configuration deciding which hooks are installed

    :return HookHandle:
    """

    undo: list[Callable[[], None]] = []

    _install_excepthook(capturer, options, undo)
    _install_threading_hook(capturer, options, undo)

    if options.capture_unraisable:
        _install_unraisable_hook(capturer, options, undo)

    _install_running_loop_hook(capturer, options)
    _install_atexit(capturer, undo)
    _install_fork_handler(capturer)

    return HookHandle(undo)


def _install_excepthook(
    capturer: Capturer, options: Options, undo: list[Callable[[], None]]
) -> None:
    """
    Install a ``sys.excepthook`` that captures and then delegates

    :param capturer: The object whose ``capture`` the hook calls
    :param options: The configuration the capture is made with
    :param undo: The list the removal callback is appended to

    :return None:
    """

    previous = sys.excepthook

    def hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_tb: TracebackType | None,
    ) -> None:
        _capture_safely(
            capturer,
            exc_value,
            options,
            source='excepthook',
            mechanism='excepthook',
            level='fatal',
        )

        previous(exc_type, exc_value, exc_tb)

    sys.excepthook = hook
    undo.append(lambda: setattr(sys, 'excepthook', previous))


def _install_threading_hook(
    capturer: Capturer, options: Options, undo: list[Callable[[], None]]
) -> None:
    """
    Install a ``threading.excepthook`` that captures and then delegates

    :param capturer: The object whose ``capture`` the hook calls
    :param options: The configuration the capture is made with
    :param undo: The list the removal callback is appended to

    :return None:
    """

    previous = threading.excepthook

    def hook(args: threading.ExceptHookArgs) -> None:
        thread_name = args.thread.name if args.thread is not None else ''

        if thread_name.startswith('logalert-'):
            previous(args)

            return

        if args.exc_value is not None:
            _capture_safely(
                capturer,
                args.exc_value,
                options,
                source='threading',
                mechanism='threading.excepthook',
                level='fatal',
            )

        previous(args)

    threading.excepthook = hook
    undo.append(lambda: setattr(threading, 'excepthook', previous))


def _install_unraisable_hook(
    capturer: Capturer, options: Options, undo: list[Callable[[], None]]
) -> None:
    """
    Install a ``sys.unraisablehook`` that captures and then delegates

    :param capturer: The object whose ``capture`` the hook calls
    :param options: The configuration the capture is made with
    :param undo: The list the removal callback is appended to

    :return None:
    """

    previous = sys.unraisablehook

    def hook(args: sys.UnraisableHookArgs) -> None:
        if args.exc_value is not None:
            _capture_safely(
                capturer,
                args.exc_value,
                options,
                source='unraisable',
                mechanism='unraisablehook',
                level='warning',
            )

        previous(args)

    sys.unraisablehook = hook
    undo.append(lambda: setattr(sys, 'unraisablehook', previous))


def install_loop_hooks(
    loop: 'asyncio.AbstractEventLoop', capturer: Capturer, options: Options
) -> None:
    """
    Attach a handler to a specific event loop

    :param loop: The loop the handler is attached to
    :param capturer: The object whose ``capture`` the handler calls
    :param options: The configuration the capture is made with

    :return None:
    """

    previous_handler = loop.get_exception_handler()

    def handler(loop_: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get('exception')

        if isinstance(exc, BaseException):
            _capture_safely(
                capturer,
                exc,
                options,
                source='asyncio',
                mechanism='asyncio',
                level='error',
            )

        if previous_handler is not None:
            previous_handler(loop_, context)

        else:
            loop_.default_exception_handler(context)

    loop.set_exception_handler(handler)


def _install_running_loop_hook(capturer: Capturer, options: Options) -> None:
    """
    Attach a handler to the loop that is already running, if there is one

    :param capturer: The object whose ``capture`` the handler calls
    :param options: The configuration the capture is made with

    :return None:
    """

    import asyncio

    try:
        loop = asyncio.get_running_loop()

    except RuntimeError:
        return

    install_loop_hooks(loop, capturer, options)


def _install_atexit(capturer: Capturer, undo: list[Callable[[], None]]) -> None:
    """
    Flush the queue on the way out

    :param capturer: The object whose ``flush`` is called on the way out
    :param undo: The list the removal callback is appended to

    :return None:
    """

    def flush_on_exit() -> None:
        """
        Flush the queue while the interpreter is shutting down

        :return None:
        """

        try:
            flush = getattr(capturer, 'flush', None)

            if callable(flush):
                flush()

        except Exception:
            pass

    atexit.register(flush_on_exit)
    undo.append(lambda: atexit.unregister(flush_on_exit))


def _install_fork_handler(capturer: Capturer) -> None:
    """
    Restart the worker in the child process after a fork

    :param capturer: The object whose ``restart_after_fork`` is called

    :return None:
    """

    if not hasattr(os, 'register_at_fork'):
        return

    def after_in_child() -> None:
        from ._guard import reset_thread_state

        reset_thread_state()
        restart = getattr(capturer, 'restart_after_fork', None)

        if callable(restart):
            with contextlib.suppress(Exception):
                restart()

    os.register_at_fork(after_in_child=after_in_child)


def alert_from_exception(
    exc: BaseException,
    options: Options,
    *,
    source: str,
    mechanism: str,
    level: str,
    logger_name: str = '',
    message: str = '',
) -> Alert:
    """
    Build an :class:`Alert` from an exception. Used by both the hooks and the
    handler

    :param exc: The exception the event is built from
    :param options: Configuration deciding how much is captured
    :param source: Where the event came from - one of the ``EventSource`` values
    :param mechanism: What produced the event
    :param level: The level name
    :param logger_name: The logger the event is attributed to
    :param message: Event text; the exception text is used when empty

    :return Alert:
    """

    from datetime import UTC, datetime

    from .types import new_event_id

    return Alert(
        event_id=new_event_id(),
        timestamp=datetime.now(UTC),
        level=level,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        logger=logger_name,
        message=message or _safe_str(exc, options.max_value_length),
        exception=exception_to_info(exc, options),
        mechanism=mechanism,
        handled=source == 'logging',
    )


__all__ = (
    'install_loop_hooks',
    'exception_to_info',
    'install_hooks',
    'should_ignore',
    'HookHandle',
)
