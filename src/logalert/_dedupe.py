"""
Logalert Dedupe

Suppression window for repeats

:copyright: (c) 2025 Aiko Sora
"""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from redis import Redis


_DEDUPE_LUA = """
local is_new = redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[1])
local prev = tonumber(redis.call('GET', KEYS[2]) or '0')
if is_new then
  -- The counter is given a longer life than the window marker on purpose. The summary
  -- is assembled on the first event *after* the window, and it is assembled from this
  -- key. With the marker's own TTL the two expired at the same instant, so the count was
  -- always already gone by the time anyone read it and the summary never fired at all.
  redis.call('SET', KEYS[2], '1', 'EX', ARGV[2])
  return {0, prev}
end
local n = redis.call('INCR', KEYS[2])
return {1, n}
"""


class DedupeResult(NamedTuple):
    """
    Verdict of one dedup check
    """

    send: bool
    occurrences: int


class _NoDedupe:
    """
    Stub used when ``dedup_window=0``
    """

    def check(self, fingerprint: str) -> DedupeResult:
        """
        Always report the event as new

        :param fingerprint: Fingerprint of the event

        :return DedupeResult:
        """

        return DedupeResult(send=True, occurrences=1)

    def close(self) -> None:
        """
        Nothing to release

        :return None:
        """

        return None


class InProcessDedupe:
    """
    Suppression window in process memory
    """

    def __init__(
        self,
        *,
        window: float,
        maxsize: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Initialize the dedupe window

        :param window: Suppression window in seconds
        :param maxsize: Maximum number of fingerprints kept
        :param clock: Monotonic clock used to stamp the windows

        :return None:
        """

        self._window = window
        self._maxsize = maxsize
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, list[float]] = OrderedDict()

    def check(self, fingerprint: str) -> DedupeResult:
        """
        Count the fingerprint and decide whether the event goes out

        :param fingerprint: Fingerprint of the event

        :return DedupeResult:
        """

        if self._window <= 0:
            return DedupeResult(send=True, occurrences=1)

        now = self._clock()

        with self._lock:
            entry = self._entries.get(fingerprint)

            if entry is None:
                self._remember(fingerprint, now)

                return DedupeResult(send=True, occurrences=1)

            window_start, count = entry

            if now - window_start < self._window:
                entry[1] = count + 1
                self._entries.move_to_end(fingerprint)

                return DedupeResult(send=False, occurrences=int(count) + 1)

            previous = int(count)
            self._remember(fingerprint, now)

            return DedupeResult(send=True, occurrences=previous)

    def _remember(self, fingerprint: str, now: float) -> None:
        """
        Open a window for the fingerprint and drop the oldest entries

        :param fingerprint: Fingerprint of the event
        :param now: Current time, as given by the clock

        :return None:
        """

        self._entries[fingerprint] = [now, 1]
        self._entries.move_to_end(fingerprint)

        while len(self._entries) > self._maxsize:
            self._entries.popitem(last=False)

    def close(self) -> None:
        """
        Forget every remembered fingerprint

        :return None:
        """

        with self._lock:
            self._entries.clear()


class RedisDedupe:
    """
    Suppression window shared by all workers
    """

    def __init__(self, client: 'Redis', *, project: str, window: float) -> None:
        """
        Register the dedup script with the Redis client

        :param client: Connected Redis client
        :param project: Project name the keys are namespaced with
        :param window: Suppression window in seconds

        :return None:
        """

        self._client = client
        self._project = project
        self._window = window
        self._script = client.register_script(_DEDUPE_LUA)

    def key_seen(self, fingerprint: str) -> str:
        """
        Redis key holding the seen marker of a fingerprint

        :param fingerprint: Fingerprint of the event

        :return str:
        """

        return f'logalert:{self._project}:seen:{fingerprint}'

    def key_count(self, fingerprint: str) -> str:
        """
        Redis key holding the recurrence counter of a fingerprint

        :param fingerprint: Fingerprint of the event

        :return str:
        """

        return f'logalert:{self._project}:count:{fingerprint}'

    def check(self, fingerprint: str) -> DedupeResult:
        """
        Run the dedup script and translate its answer

        :param fingerprint: Fingerprint of the event

        :return DedupeResult:
        """

        if self._window <= 0:
            return DedupeResult(send=True, occurrences=1)

        seconds = max(1, int(self._window))
        suppressed, count = self._script(
            keys=[self.key_seen(fingerprint), self.key_count(fingerprint)],
            # Twice the window for the counter: the window marker has to be gone
            # before the next event opens a new one, and the counter has to
            # still be there to supply the summary for the window that just
            # closed.
            args=[seconds, seconds * 2],
        )

        count = int(count)

        if int(suppressed):
            return DedupeResult(send=False, occurrences=count)

        return DedupeResult(send=True, occurrences=max(count, 1))

    def close(self) -> None:
        """
        Nothing to release - the Redis keys expire on their own

        :return None:
        """

        return None


def build_dedupe(
    *,
    window: float,
    maxsize: int,
    redis_client: 'Redis | None' = None,
    project: str | None = None,
) -> InProcessDedupe | RedisDedupe | _NoDedupe:
    """
    Pick the implementation that matches the configuration

    :param window: Suppression window in seconds; 0 turns dedup off entirely
    :param maxsize: How many fingerprints the in-process window keeps
    :param redis_client: Redis client, when a shared window is available
    :param project: Project name the Redis keys are namespaced with

    :return InProcessDedupe | RedisDedupe | _NoDedupe:
    """

    if window <= 0:
        return _NoDedupe()

    if redis_client is not None and project:
        return RedisDedupe(redis_client, project=project, window=window)

    return InProcessDedupe(window=window, maxsize=maxsize)


__all__ = (
    'InProcessDedupe',
    'DedupeResult',
    'build_dedupe',
    'RedisDedupe',
)
