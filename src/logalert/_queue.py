"""
Logalert Queue

Bounded queue between the application and the background worker

:copyright: (c) 2025 Aiko Sora
"""

import threading
import time
from collections import Counter, deque

from .types import Alert


class QueueStats:
    """
    Queue counters
    """

    __slots__ = ('delivered', 'dropped', 'enqueued')

    def __init__(self, enqueued: int, delivered: int, dropped: dict[str, int]) -> None:
        """
        Store the counters

        :param enqueued: How many events were enqueued
        :param delivered: How many events were handed to the worker
        :param dropped: How many events were lost, per reason

        :return None:
        """

        self.enqueued = enqueued
        self.delivered = delivered
        self.dropped = dropped

    @property
    def dropped_total(self) -> int:
        """
        Every dropped event, whatever the reason

        :return int:
        """

        return sum(self.dropped.values())

    def __repr__(self) -> str:
        """
        Text form of the counters

        :return str:
        """

        return (
            f'QueueStats(enqueued={self.enqueued}, delivered={self.delivered}, '
            f'dropped={self.dropped})'
        )


class BoundedQueue:
    """
    Event queue with a bound and an overflow policy
    """

    def __init__(
        self,
        *,
        maxsize: int,
        policy: str = 'drop_oldest',
        block_timeout: float = 0.05,
    ) -> None:
        """
        Initialize the queue

        :param maxsize: How many events the buffer holds
        :param policy: What to do when it is full: 'drop_oldest', 'drop_newest'
            or 'block'
        :param block_timeout: How long the 'block' policy waits for room, in
            seconds

        :return None:
        """

        self._buf: deque[Alert] = deque(maxlen=maxsize)
        self._cond = threading.Condition()
        self._policy = policy
        self._block_timeout = block_timeout
        self._closed = False
        self._dropped: Counter[str] = Counter()
        self._enqueued = 0
        self._delivered = 0

    def put(self, alert: Alert) -> None:
        """
        Enqueue an event

        :param alert: The event to enqueue

        :return None:
        """

        try:
            with self._cond:
                if self._closed:
                    self._dropped['queue_closed'] += 1

                    return

                if self._is_full() and self._policy == 'block':
                    self._cond.wait(self._block_timeout)

                if self._is_full():
                    self._dropped['queue_full'] += 1

                    if self._policy == 'drop_newest':
                        return

                self._buf.append(alert)
                self._enqueued += 1
                self._cond.notify()

        except Exception:
            pass

    def count(self, reason: str) -> None:
        """
        Record a loss that happened outside the queue (hook filtering, dedup,
        sampling)

        :param reason: The reason the loss is attributed to

        :return None:
        """

        with self._cond:
            self._dropped[reason] += 1

    def get_batch(self, *, max_items: int, timeout: float) -> list[Alert]:
        """
        Fetch a batch of events, waiting for the first one no longer than
        ``timeout``

        :param max_items: The maximum number of events to take
        :param timeout: How long to wait for the first event, in seconds

        :return list[Alert]:
        """

        deadline = time.monotonic() + timeout

        with self._cond:
            while not self._buf and not self._closed:
                remaining = deadline - time.monotonic()

                if remaining <= 0:
                    return []

                self._cond.wait(remaining)

            batch = [self._buf.popleft() for _ in range(min(max_items, len(self._buf)))]
            self._delivered += len(batch)

            if batch:
                self._cond.notify_all()

            return batch

    def drain(self, timeout: float) -> list[Alert]:
        """
        Fetch everything available, waiting up to ``timeout``

        :param timeout: The maximum time to wait, in seconds

        :return list[Alert]:
        """

        deadline = time.monotonic() + timeout
        collected: list[Alert] = []

        while True:
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                break

            batch = self.get_batch(max_items=10_000, timeout=min(remaining, 0.1))

            if not batch:
                if self.qsize() == 0:
                    break

                continue

            collected.extend(batch)

        return collected

    def qsize(self) -> int:
        """
        Number of events waiting in the buffer

        :return int:
        """

        with self._cond:
            return len(self._buf)

    def close(self) -> None:
        """
        Close the queue and wake the worker so it can finish

        :return None:
        """

        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        """
        Whether the queue has been closed

        :return bool:
        """

        return self._closed

    def stats(self) -> QueueStats:
        """
        Snapshot of the counters

        :return QueueStats:
        """

        with self._cond:
            return QueueStats(self._enqueued, self._delivered, dict(self._dropped))

    def take_dropped(self) -> dict[str, int]:
        """
        Hand back the accumulated drop counters and reset them

        :return dict[str, int]:
        """

        with self._cond:
            snapshot = dict(self._dropped)
            self._dropped.clear()

            return snapshot

    def reset_after_fork(self) -> None:
        """
        Recreate the synchronization primitives in the child process

        :return None:
        """

        self._buf = deque(maxlen=self._buf.maxlen)
        self._cond = threading.Condition()
        self._closed = False

    def _is_full(self) -> bool:
        """
        Whether the buffer has grown to ``maxlen``

        :return bool:
        """

        maxlen = self._buf.maxlen

        return maxlen is not None and len(self._buf) >= maxlen


__all__ = (
    'BoundedQueue',
    'QueueStats',
)
