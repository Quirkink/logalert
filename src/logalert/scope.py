"""
Logalert Scope

Scope: tags, contexts, and the ring buffer of events

:copyright: (c) 2025 Aiko Sora
"""

import threading
from collections import deque
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

from .types import Breadcrumb, EventLevel


class BreadcrumbBuffer:
    """
    A ring buffer of recent events
    """

    def __init__(self, *, maxlen: int = 100) -> None:
        """
        Make a buffer that keeps at most ``maxlen`` events

        :param maxlen: How many events to keep; below one disables the buffer

        :return None:
        """

        self._items: deque[Breadcrumb] = deque(maxlen=max(maxlen, 0))
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """
        Whether the buffer is collecting anything

        :return bool:
        """

        return self._items.maxlen is not None and self._items.maxlen > 0

    def add(self, crumb: Breadcrumb) -> None:
        """
        Append an event to the buffer

        :param crumb: Event to store

        :return None:
        """

        if not self.enabled:
            return

        with self._lock:
            self._items.append(crumb)

    def snapshot(self) -> tuple[Breadcrumb, ...]:
        """
        A copy of the contents

        :return tuple[Breadcrumb, ...]:
        """

        with self._lock:
            return tuple(self._items)

    def clear(self) -> None:
        """
        Empty the buffer

        :return None:
        """

        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        """
        How many events the buffer currently holds

        :return int:
        """

        with self._lock:
            return len(self._items)


class Scope:
    """
    Global tags, contexts, and the event buffer
    """

    def __init__(self, *, max_breadcrumbs: int = 100) -> None:
        """
        Make a scope with empty tags, contexts, and buffer

        :param max_breadcrumbs: How many events the buffer keeps

        :return None:
        """

        self._lock = threading.RLock()
        self._tags: dict[str, str] = {}
        self._contexts: dict[str, dict[str, Any]] = {}
        self._user: dict[str, str] | None = None
        self._fingerprint: tuple[str, ...] | None = None
        self.breadcrumbs = BreadcrumbBuffer(maxlen=max_breadcrumbs)

    def set_tag(self, key: str, value: str) -> None:
        """
        Add a tag to every subsequent event

        :param key: Tag name
        :param value: Tag value

        :return None:
        """

        with self._lock:
            self._tags[str(key)] = str(value)

    def set_tags(self, tags: Mapping[str, str]) -> None:
        """
        Add several tags to every subsequent event

        :param tags: Tag names mapped to their values

        :return None:
        """

        with self._lock:
            for key, value in tags.items():
                self._tags[str(key)] = str(value)

    def set_context(self, name: str, value: Mapping[str, Any]) -> None:
        """
        Attach a context block to every subsequent event

        :param name: Context name
        :param value: Context payload, copied before it is stored

        :return None:
        """

        with self._lock:
            self._contexts[str(name)] = dict(value)

    def set_user(self, user: Mapping[str, str] | None) -> None:
        """
        Set, or drop, the user attached to every subsequent event

        :param user: User attributes, or None to drop the current user

        :return None:
        """

        with self._lock:
            self._user = dict(user) if user else None

    def set_fingerprint(self, fingerprint: tuple[str, ...] | None) -> None:
        """
        Override the fingerprint of every subsequent event

        :param fingerprint: Fingerprint parts, or None to clear the override

        :return None:
        """

        with self._lock:
            self._fingerprint = fingerprint

    def tags(self) -> dict[str, str]:
        """
        A copy of the current tags

        :return dict[str, str]:
        """

        with self._lock:
            return dict(self._tags)

    def contexts(self) -> dict[str, dict[str, Any]]:
        """
        A copy of the current contexts

        :return dict[str, dict[str, Any]]:
        """

        with self._lock:
            return {k: dict(v) for k, v in self._contexts.items()}

    def user(self) -> dict[str, str] | None:
        """
        A copy of the current user, if one is set

        :return dict[str, str] | None:
        """

        with self._lock:
            return dict(self._user) if self._user else None

    def fingerprint(self) -> tuple[str, ...] | None:
        """
        The fingerprint override, if one is set

        :return tuple[str, ...] | None:
        """

        with self._lock:
            return self._fingerprint

    def add_breadcrumb(
        self,
        message: str,
        *,
        category: str = 'manual',
        level: EventLevel = 'info',
        type: str = 'default',
        data: Mapping[str, Any] | None = None,
        origin: str = 'manual',
        timestamp: datetime | None = None,
        max_length: int = 1024,
    ) -> None:
        """
        Record an event in the buffer

        :param message: Event description
        :param category: Category the event belongs to
        :param level: Severity of the event
        :param type: Event type, as the renderer names it
        :param data: Extra payload, copied before it is stored
        :param origin: Where the event came from; anything but ``manual`` is
            stored as ``auto``
        :param timestamp: When it happened, now by default
        :param max_length: Longest message to keep, the ellipsis included

        :return None:
        """

        text = message if len(message) <= max_length else message[: max_length - 3] + '...'

        self.breadcrumbs.add(
            Breadcrumb(
                timestamp=timestamp or datetime.now(UTC),
                level=level,
                category=category,
                message=text,
                type=type,
                data=dict(data) if data else {},
                origin='manual' if origin == 'manual' else 'auto',
            )
        )

    def clear(self) -> None:
        """
        Drop the tags, the contexts, the user, and the buffer

        :return None:
        """

        with self._lock:
            self._tags.clear()
            self._contexts.clear()

            self._user = None
            self._fingerprint = None

        self.breadcrumbs.clear()

    def __enter__(self) -> 'Scope':
        """
        Enter the context manager

        :return Scope:
        """

        return self

    def __exit__(self, *_: object) -> None:
        """
        Leave the context manager, which changes nothing

        :param _: Exception type, value and traceback

        :return None:
        """

        return None

    def __iter__(self) -> Iterator[str]:
        """
        Iterate over the tag names, in sorted order

        :return Iterator[str]:
        """

        return iter(sorted(self._tags))


__all__ = (
    'BreadcrumbBuffer',
    'Scope',
)
