"""
Logalert Tests Queue

Covers the bounded queue: the "emit never blocks" guarantee.

:copyright: (c) 2025 Aiko Sora
"""

import time

from conftest import make_alert

from logalert._queue import BoundedQueue


def test_put_is_fast_even_when_full() -> None:
    """
    A full queue must not slow `put` down
    """

    queue: BoundedQueue = BoundedQueue(maxsize=10, policy='drop_oldest')

    for _ in range(10):
        queue.put(make_alert())

    started = time.monotonic()

    for _ in range(10_000):
        queue.put(make_alert())

    elapsed = time.monotonic() - started

    # Checking that the puts returned quickly
    assert elapsed < 0.5, f'10000 puts into a full queue took {elapsed:.3f} s'

    # Checking that every dropped alert was counted
    assert queue.stats().dropped['queue_full'] == 10_000


def test_drop_oldest_keeps_newest() -> None:
    """
    During a storm a fresh error is more useful than a stale one
    """

    queue: BoundedQueue = BoundedQueue(maxsize=3, policy='drop_oldest')

    for index in range(5):
        queue.put(make_alert(message=f'number {index}'))

    kept = [a.message for a in queue.drain(1.0)]

    # Checking that the newest alerts were the ones kept
    assert kept == ['number 2', 'number 3', 'number 4']
    assert queue.stats().dropped['queue_full'] == 2


def test_drop_newest_keeps_oldest() -> None:
    """
    The opposite policy keeps the alerts that arrived first
    """

    queue: BoundedQueue = BoundedQueue(maxsize=3, policy='drop_newest')

    for index in range(5):
        queue.put(make_alert(message=f'number {index}'))

    kept = [a.message for a in queue.drain(1.0)]

    # Checking that the oldest alerts were the ones kept
    assert kept == ['number 0', 'number 1', 'number 2']
    assert queue.stats().dropped['queue_full'] == 2


def test_dropped_counters_are_taken_once() -> None:
    """
    Drop counters are handed over once and attached to the next alert
    """

    queue: BoundedQueue = BoundedQueue(maxsize=1, policy='drop_oldest')
    queue.put(make_alert())

    for _ in range(7):
        queue.put(make_alert())

    # Checking that the second take yields nothing
    assert queue.take_dropped() == {'queue_full': 7}
    assert queue.take_dropped() == {}


def test_put_after_close_does_not_raise() -> None:
    """
    A put into a closed queue is counted, not raised
    """

    queue: BoundedQueue = BoundedQueue(maxsize=4, policy='drop_oldest')
    queue.close()
    queue.put(make_alert())

    assert queue.stats().dropped['queue_closed'] == 1


def test_block_policy_is_bounded() -> None:
    """
    The block policy waits,
    but no longer than declared
    """

    queue: BoundedQueue = BoundedQueue(maxsize=1, policy='block', block_timeout=0.05)
    queue.put(make_alert())

    started = time.monotonic()

    for _ in range(5):
        queue.put(make_alert())

    elapsed = time.monotonic() - started

    # Checking that the waiting stayed within the declared timeout
    assert elapsed < 0.6, f'block waited too long: {elapsed:.3f} s'


def test_reset_after_fork_clears_lock() -> None:
    """
    The queue must still work after a fork
    """

    queue: BoundedQueue = BoundedQueue(maxsize=4, policy='drop_oldest')

    queue.put(make_alert())
    queue.reset_after_fork()
    queue.put(make_alert())

    assert queue.qsize() == 1
    assert queue.stats().dropped == {}


__all__ = (
    'test_dropped_counters_are_taken_once',
    'test_put_after_close_does_not_raise',
    'test_reset_after_fork_clears_lock',
    'test_put_is_fast_even_when_full',
    'test_drop_newest_keeps_oldest',
    'test_drop_oldest_keeps_newest',
    'test_block_policy_is_bounded',
)
