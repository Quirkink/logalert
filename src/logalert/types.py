"""
Logalert Types

Data model: :class:`Alert` and everything it is made of

:copyright: (c) 2025 Aiko Sora
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

EventLevel = Literal['debug', 'info', 'warning', 'error', 'fatal']

EventSource = Literal[
    'logging',
    'excepthook',
    'threading',
    'asyncio',
    'unraisable',
    'manual',
]

LEVEL_ORDER: dict[str, int] = {
    'debug': 10,
    'info': 20,
    'warning': 30,
    'error': 40,
    'fatal': 50,
}


def new_event_id() -> str:
    """
    Event identifier - 32 hex characters, so it is not mistaken for a hyphenated
    UUID

    :return str:
    """

    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class Frame:
    """
    A single stack frame
    """

    filename: str
    lineno: int
    function: str
    module: str | None = None
    in_app: bool = True
    source_line: str | None = None
    locals: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize the frame into a plain dictionary

        :return dict[str, Any]:
        """

        data: dict[str, Any] = {
            'filename': self.filename,
            'lineno': self.lineno,
            'function': self.function,
            'in_app': self.in_app,
        }

        if self.module is not None:
            data['module'] = self.module

        if self.source_line is not None:
            data['source_line'] = self.source_line

        if self.locals:
            data['locals'] = self.locals

        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'Frame':
        """
        Rebuild a frame from its serialized form

        :param data: Serialized frame

        :return Frame:
        """

        return cls(
            filename=data['filename'],
            lineno=int(data['lineno']),
            function=data['function'],
            module=data.get('module'),
            in_app=bool(data.get('in_app', True)),
            source_line=data.get('source_line'),
            locals=dict(data.get('locals') or {}),
        )


@dataclass(frozen=True, slots=True)
class Stacktrace:
    """
    Stack of frames from outermost to innermost - in execution order
    """

    frames: tuple[Frame, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize every frame of the stack

        :return dict[str, Any]:
        """

        return {'frames': [f.to_dict() for f in self.frames]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'Stacktrace':
        """
        Rebuild a stack from its serialized form

        :param data: Serialized stacktrace

        :return Stacktrace:
        """

        return cls(frames=tuple(Frame.from_dict(f) for f in data.get('frames', ())))


@dataclass(frozen=True, slots=True)
class ExceptionInfo:
    """
    An exception with its chain of causes
    """

    type_name: str
    module: str
    message: str
    stacktrace: Stacktrace = field(default_factory=Stacktrace)
    cause: 'ExceptionInfo | None' = None
    context: 'ExceptionInfo | None' = None
    is_group: bool = False
    leaves: 'tuple[ExceptionInfo, ...]' = ()
    escaped: bool = False

    @property
    def qualified_name(self) -> str:
        """
        Exception name prefixed by its module

        :return str:
        """

        return f'{self.module}.{self.type_name}' if self.module else self.type_name

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize the exception together with its chain

        :return dict[str, Any]:
        """

        data: dict[str, Any] = {
            'type': self.type_name,
            'module': self.module,
            'message': self.message,
            'stacktrace': self.stacktrace.to_dict(),
        }

        if self.cause is not None:
            data['cause'] = self.cause.to_dict()

        if self.context is not None:
            data['context'] = self.context.to_dict()

        if self.is_group:
            data['is_group'] = True

            data['leaves'] = [leaf.to_dict() for leaf in self.leaves]

        if self.escaped:
            data['escaped'] = True

        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'ExceptionInfo':
        """
        Rebuild an exception, walking its chain recursively

        :param data: Serialized exception

        :return ExceptionInfo:
        """

        cause = data.get('cause')

        context = data.get('context')

        return cls(
            type_name=data['type'],
            module=data.get('module', ''),
            message=data.get('message', ''),
            stacktrace=Stacktrace.from_dict(data.get('stacktrace') or {}),
            cause=cls.from_dict(cause) if cause else None,
            context=cls.from_dict(context) if context else None,
            is_group=bool(data.get('is_group', False)),
            leaves=tuple(cls.from_dict(leaf) for leaf in data.get('leaves', ())),
            escaped=bool(data.get('escaped', False)),
        )


@dataclass(frozen=True, slots=True)
class Breadcrumb:
    """
    An entry in the ring buffer of "what was going on before the error"
    """

    timestamp: datetime
    level: EventLevel
    category: str
    message: str
    type: str = 'default'
    data: dict[str, Any] = field(default_factory=dict)
    origin: Literal['auto', 'manual'] = 'auto'

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize the breadcrumb

        :return dict[str, Any]:
        """

        data: dict[str, Any] = {
            'timestamp': self.timestamp.isoformat(),
            'level': self.level,
            'category': self.category,
            'message': self.message,
            'type': self.type,
            'origin': self.origin,
        }

        if self.data:
            data['data'] = self.data

        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'Breadcrumb':
        """
        Rebuild a breadcrumb from its serialized form

        :param data: Serialized breadcrumb

        :return Breadcrumb:
        """

        return cls(
            timestamp=_parse_dt(data['timestamp']),
            level=data['level'],
            category=data['category'],
            message=data['message'],
            type=data.get('type', 'default'),
            data=dict(data.get('data') or {}),
            origin=data.get('origin', 'auto'),
        )


@dataclass(frozen=True, slots=True)
class Alert:
    """
    The event that ultimately goes out to the messenger
    """

    event_id: str
    timestamp: datetime
    level: EventLevel
    source: EventSource
    logger: str
    message: str
    exception: ExceptionInfo | None = None
    environment: str = 'production'
    release: str | None = None
    server_name: str | None = None
    platform: str = 'python'
    tags: dict[str, str] = field(default_factory=dict)
    contexts: dict[str, dict[str, Any]] = field(default_factory=dict)
    user: dict[str, str] | None = None
    breadcrumbs: tuple[Breadcrumb, ...] = ()
    fingerprint: tuple[str, ...] | None = None
    mechanism: str = 'logging'
    handled: bool = True
    count: int = 1
    dropped: dict[str, int] = field(default_factory=dict)

    @property
    def title(self) -> str:
        """
        Short human-readable event name for the message title

        :return str:
        """

        if self.exception is not None:
            return self.exception.type_name

        return self.level

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize the alert for transport

        :return dict[str, Any]:
        """

        data: dict[str, Any] = {
            'event_id': self.event_id,
            'timestamp': self.timestamp.isoformat(),
            'level': self.level,
            'source': self.source,
            'logger': self.logger,
            'message': self.message,
            'platform': self.platform,
            'environment': self.environment,
            'mechanism': self.mechanism,
            'handled': self.handled,
        }

        if self.exception is not None:
            data['exception'] = self.exception.to_dict()

        if self.release is not None:
            data['release'] = self.release

        if self.server_name is not None:
            data['server_name'] = self.server_name

        if self.tags:
            data['tags'] = self.tags

        if self.contexts:
            data['contexts'] = self.contexts

        if self.user is not None:
            data['user'] = self.user

        if self.breadcrumbs:
            data['breadcrumbs'] = [b.to_dict() for b in self.breadcrumbs]

        if self.fingerprint is not None:
            data['fingerprint'] = list(self.fingerprint)

        if self.count != 1:
            data['count'] = self.count

        if self.dropped:
            data['dropped'] = self.dropped

        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'Alert':
        """
        Rebuild an alert from its serialized form

        :param data: Serialized alert

        :return Alert:
        """

        exc = data.get('exception')

        fp = data.get('fingerprint')

        return cls(
            event_id=data['event_id'],
            timestamp=_parse_dt(data['timestamp']),
            level=data['level'],
            source=data['source'],
            logger=data['logger'],
            message=data['message'],
            exception=ExceptionInfo.from_dict(exc) if exc else None,
            environment=data.get('environment', 'production'),
            release=data.get('release'),
            server_name=data.get('server_name'),
            platform=data.get('platform', 'python'),
            tags=dict(data.get('tags') or {}),
            contexts=dict(data.get('contexts') or {}),
            user=data.get('user'),
            breadcrumbs=tuple(Breadcrumb.from_dict(b) for b in data.get('breadcrumbs', ())),
            fingerprint=tuple(fp) if fp else None,
            mechanism=data.get('mechanism', 'logging'),
            handled=bool(data.get('handled', True)),
            count=int(data.get('count', 1)),
            dropped=dict(data.get('dropped') or {}),
        )


def _parse_dt(value: str | datetime) -> datetime:
    """
    Parse a timestamp that came out of Redis, tolerant of a missing timezone

    :param value: ISO string, or a datetime that is already parsed

    :return datetime:
    """

    if isinstance(value, datetime):
        return value

    parsed = datetime.fromisoformat(value)

    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = (
    'ExceptionInfo',
    'new_event_id',
    'EventSource',
    'Breadcrumb',
    'EventLevel',
    'Stacktrace',
    'Alert',
    'Frame',
)
