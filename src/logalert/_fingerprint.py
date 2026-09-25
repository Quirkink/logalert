"""
Logalert Fingerprint

Derives the grouping key that decides which errors are "the same bug"

:copyright: (c) 2025 Aiko Sora
"""

import hashlib
import os
import re
from pathlib import PurePath
from typing import TYPE_CHECKING

from .types import Alert, ExceptionInfo, Frame, Stacktrace

if TYPE_CHECKING:
    from .options import Options


FINGERPRINT_VERSION = 'logalert-fp-v1'

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__)).replace('\\', '/') + '/'

MAX_CAUSE_DEPTH = 5
MAX_GROUP_LEAVES = 10

_UUID_RE = re.compile(
    r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'
)
_HEX_RE = re.compile(r'\b0x[0-9a-fA-F]+\b')
_PATH_RE = re.compile(r'(?:/[\w.\-+]+){2,}')
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_INT_RE = re.compile(r'\b\d+\b')


def normalize_message(message: str, *, limit: int = 256) -> str:
    """
    Strip out everything in the text that changes from run to run

    :param message: The message text to normalize
    :param limit: Maximum length of the result

    :return str:
    """

    result = _UUID_RE.sub('U', message)
    result = _HEX_RE.sub('X', result)
    result = _PATH_RE.sub('P', result)
    result = _QUOTED_RE.sub('S', result)
    result = _INT_RE.sub('N', result)

    return result[:limit]


def fingerprint(alert: Alert, options: 'Options | None' = None) -> str:
    """
    Compute the event fingerprint

    :param alert: The event to fingerprint
    :param options: Options controlling path depth, the logger and line numbers

    :return str:
    """

    if alert.fingerprint:
        return _hash(FINGERPRINT_VERSION, *alert.fingerprint)

    depth = options.fingerprint_path_depth if options else 2

    include_logger = options.fingerprint_include_logger if options else True
    include_lineno = options.fingerprint_include_lineno if options else False

    parts: list[str] = [FINGERPRINT_VERSION]

    if alert.exception is not None:
        parts.extend(_exception_parts(alert.exception, depth, include_lineno))

    if include_logger:
        parts.append(f'logger:{alert.logger}')

    if alert.exception is None:
        parts.append(f'msg:{normalize_message(alert.message)}')

    return _hash(*parts)


def _exception_parts(exc: ExceptionInfo, depth: int, include_lineno: bool) -> list[str]:
    """
    Break the exception down together with its cause chain

    :param exc: The exception to break down
    :param depth: How many trailing parts of a path a frame contributes
    :param include_lineno: Whether line numbers take part in the fingerprint

    :return list[str]:
    """

    parts: list[str] = []
    current: ExceptionInfo | None = exc
    seen = 0

    while current is not None and seen < MAX_CAUSE_DEPTH:
        if current.is_group and current.leaves:
            parts.append(_group_part(current, depth, include_lineno))

        else:
            parts.append(_single_part(current, depth, include_lineno))

        current = current.cause or current.context
        seen += 1

    return parts


def _single_part(exc: ExceptionInfo, depth: int, include_lineno: bool) -> str:
    """
    One link of the chain: the type name, the frame count and the frames

    :param exc: The exception to break down
    :param depth: How many trailing parts of a path a frame contributes
    :param include_lineno: Whether line numbers take part in the fingerprint

    :return str:
    """

    frames = _frame_parts(exc.stacktrace, depth, include_lineno)

    return f'{exc.qualified_name}@{len(frames)}frames|' + '|'.join(frames)


def _group_part(exc: ExceptionInfo, depth: int, include_lineno: bool) -> str:
    """
    ExceptionGroup: the order of the leaves must not affect the fingerprint, so
    we sort

    :param exc: The exception group to break down
    :param depth: How many trailing parts of a path a frame contributes
    :param include_lineno: Whether line numbers take part in the fingerprint

    :return str:
    """

    leaves = sorted(
        _single_part(leaf, depth, include_lineno) for leaf in exc.leaves[:MAX_GROUP_LEAVES]
    )

    return f'{exc.qualified_name}@group[{len(leaves)}]|' + '||'.join(leaves)


def _frame_parts(stacktrace: Stacktrace, depth: int, include_lineno: bool) -> list[str]:
    """
    The parts the frames of a stacktrace contribute, noise left out

    :param stacktrace: The stacktrace to walk
    :param depth: How many trailing parts of a path a frame contributes
    :param include_lineno: Whether line numbers take part in the fingerprint

    :return list[str]:
    """

    return [
        _frame_part(frame, depth, include_lineno)
        for frame in stacktrace.frames
        if not _is_noise(frame)
    ]


def _frame_part(frame: Frame, depth: int, include_lineno: bool) -> str:
    """
    Frame - a line of the fingerprint

    :param frame: The frame to turn into a part
    :param depth: How many trailing parts of the path to keep
    :param include_lineno: Whether the line number takes part in the fingerprint

    :return str:
    """

    parts = PurePath(frame.filename).parts
    tail = '/'.join(parts[-depth:]) if len(parts) >= depth else frame.filename

    if include_lineno:
        return f'{tail}:{frame.function}:{frame.lineno}'

    return f'{tail}:{frame.function}'


def _is_noise(frame: Frame) -> bool:
    """
    Frames from logging itself and from the library itself - a constant in every
    fingerprint.

    :param frame: The frame to inspect

    :return bool:
    """

    filename = frame.filename.replace('\\', '/')

    if filename.endswith('logging/__init__.py'):
        return True

    module = frame.module or ''

    if module == 'logalert' or module.startswith('logalert.'):
        return True

    return filename.startswith(_PACKAGE_DIR)


def _hash(*parts: str) -> str:
    """
    The SHA-256 of the parts joined with a NUL, cut down to 32 hex characters

    :param parts: The strings that make up the fingerprint

    :return str:
    """

    digest = hashlib.sha256('\0'.join(parts).encode('utf-8', 'replace'))

    return digest.hexdigest()[:32]


__all__ = (
    'FINGERPRINT_VERSION',
    'normalize_message',
    'fingerprint',
)
