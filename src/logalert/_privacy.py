"""
Logalert Privacy

Scrubbing an event before it is sent

:copyright: (c) 2025 Aiko Sora
"""

import re
from dataclasses import replace
from typing import Any

from .options import Options
from .types import Alert, Breadcrumb, ExceptionInfo, Frame, Stacktrace

REDACTED = '[Filtered]'

DEFAULT_DENYLIST = frozenset(
    {
        'password',
        'passwd',
        'pwd',
        'secret',
        'token',
        'access_token',
        'refresh_token',
        'api_key',
        'apikey',
        'client_secret',
        'private_key',
        'authorization',
        'auth',
        'cookie',
        'session',
        'sessionid',
        'credential',
        'credentials',
        'signature',
        'challenge',
    }
)

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'\d{7,12}:[A-Za-z0-9_-]{30,}'),
    re.compile(r'\bsyt_[A-Za-z0-9_-]{10,}\b'),
    re.compile(r'\bMDAx[A-Za-z0-9_-]{10,}\b'),
    re.compile(r'(?i)\b(?:Bearer|Basic)\s+\S{6,}'),
    re.compile(
        r"""(?i)\b(?:password|passwd|secret|token|api[_-]?key)\b["']?\s*[=:]\s*["']?[^\s"',}]+"""
    ),
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
)

_NORMALIZE_KEY_RE = re.compile(r'[^a-z0-9]')


def _normalize_key(key: str) -> str:
    """
    The form a name is compared in: lower case, nothing but letters and digits

    :param key: The variable or field name to normalize

    :return str:
    """

    return _NORMALIZE_KEY_RE.sub('', key.lower())


def _is_denied(key: str) -> bool:
    """
    Whether a name is on the denylist, by exact match or by suffix

    :param key: The variable or field name to check

    :return bool:
    """

    normalized = _normalize_key(key)

    if normalized in DEFAULT_DENYLIST:
        return True

    return any(
        normalized.endswith(suffix)
        for suffix in ('password', 'secret', 'token', 'apikey', 'privatekey')
    )


def redact_text(value: str) -> str:
    """
    Replace the secrets it finds

    :param value: The text to scrub

    :return str:
    """

    result = value

    for pattern in SECRET_PATTERNS:
        result = pattern.sub(REDACTED, result)

    return result


def _redact_value(value: Any, *, deny_keys: bool, depth: int = 0) -> Any:
    """
    Recursively scrub a value while preserving its structure

    :param value: The value to scrub
    :param deny_keys: Whether keys are also checked against the denylist
    :param depth: The current recursion depth

    :return Any:
    """

    if depth > 6:
        return '[...]'

    if isinstance(value, str):
        return redact_text(value)

    if isinstance(value, dict):
        result: dict[str, Any] = {}

        for key, item in value.items():
            key_str = str(key)

            if deny_keys and _is_denied(key_str) and not isinstance(item, (dict, list, tuple)):
                result[key_str] = REDACTED

            else:
                result[key_str] = _redact_value(item, deny_keys=deny_keys, depth=depth + 1)

        return result

    if isinstance(value, (list, tuple)):
        return [_redact_value(item, deny_keys=deny_keys, depth=depth + 1) for item in value]

    return value


def _scrub_frame(frame: Frame, *, deny_keys: bool) -> Frame:
    """
    Scrub the local variables of a frame

    :param frame: The frame to scrub
    :param deny_keys: Whether variable names are also checked against the
        denylist

    :return Frame:
    """

    if not frame.locals:
        return frame

    return replace(frame, locals=_redact_value(frame.locals, deny_keys=deny_keys))


def _scrub_stacktrace(stacktrace: Stacktrace, *, deny_keys: bool) -> Stacktrace:
    """
    Scrub every frame of a stacktrace

    :param stacktrace: The stacktrace to scrub
    :param deny_keys: Whether variable names are also checked against the
        denylist

    :return Stacktrace:
    """

    return Stacktrace(frames=tuple(_scrub_frame(f, deny_keys=deny_keys) for f in stacktrace.frames))


def _scrub_exception(exc: ExceptionInfo | None, *, deny_keys: bool) -> ExceptionInfo | None:
    """
    Scrub an exception, its stacktrace and its whole cause chain

    :param exc: The exception to scrub, when there is one
    :param deny_keys: Whether variable names are also checked against the
        denylist

    :return ExceptionInfo | None:
    """

    if exc is None:
        return None

    return replace(
        exc,
        message=redact_text(exc.message),
        stacktrace=_scrub_stacktrace(exc.stacktrace, deny_keys=deny_keys),
        cause=_scrub_exception(exc.cause, deny_keys=deny_keys),
        context=_scrub_exception(exc.context, deny_keys=deny_keys),
        leaves=tuple(
            scrubbed
            for scrubbed in (_scrub_exception(leaf, deny_keys=deny_keys) for leaf in exc.leaves)
            if scrubbed is not None
        ),
    )


def _scrub_breadcrumb(crumb: Breadcrumb) -> Breadcrumb:
    """
    Scrub a breadcrumb message together with its data

    :param crumb: The breadcrumb to scrub

    :return Breadcrumb:
    """

    return replace(
        crumb,
        message=redact_text(crumb.message),
        data=_redact_value(crumb.data, deny_keys=True),
    )


def scrub(alert: Alert, options: Options) -> Alert:
    """
    Scrub an event before sending it

    :param alert: The event to scrub
    :param options: Configuration deciding whether personal data goes through

    :return Alert:
    """

    deny_keys = not options.send_default_pii
    user = alert.user

    if deny_keys and user:
        allowed = {key: value for key, value in user.items() if key == 'id'}
        user = allowed or None

    return replace(
        alert,
        message=redact_text(alert.message),
        exception=_scrub_exception(alert.exception, deny_keys=deny_keys),
        tags=_redact_value(alert.tags, deny_keys=deny_keys),
        contexts=_redact_value(alert.contexts, deny_keys=deny_keys),
        user=user,
        breadcrumbs=tuple(_scrub_breadcrumb(b) for b in alert.breadcrumbs),
    )


__all__ = (
    'DEFAULT_DENYLIST',
    'SECRET_PATTERNS',
    'redact_text',
    'REDACTED',
    'scrub',
)
