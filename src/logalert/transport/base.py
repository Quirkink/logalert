"""
Logalert Transport Base

Transport: how an event gets from the application to the sinks

:copyright: (c) 2025 Aiko Sora
"""

import threading
import time
from dataclasses import replace
from typing import Protocol, runtime_checkable

from .._delivery import Deliverer, DeliveryOutcome
from .._queue import BoundedQueue
from ..options import Options
from ..types import Alert


@runtime_checkable
class Transport(Protocol):
    """
    Where the client hands off a finished event
    """

    def submit(self, alert: Alert) -> None:
        """
        Accept the event. Must return quickly and must not raise

        :param alert: The finished event

        :return None:
        """

        ...

    def flush(self, timeout: float) -> bool:
        """
        Flush everything buffered. False means it did not fit in the time
        allowed

        :param timeout: How long the flush may take, in seconds

        :return bool:
        """

        ...

    def close(self) -> None:
        """
        Stop and free resources

        :return None:
        """

        ...


class DirectTransport:
    """
    An in-memory queue plus a worker thread that sends the alerts
    """

    def __init__(self, deliverer: Deliverer, options: Options) -> None:
        """
        Build the queue and start the worker thread

        :param deliverer: Delivers each event to the sinks
        :param options: Resolved options: queue size, backpressure, batch size

        :return None:
        """

        self._deliverer = deliverer
        self._options = options
        self._queue = BoundedQueue(
            maxsize=options.queue_size,
            policy=options.backpressure,
            block_timeout=options.block_timeout,
        )

        self._stop = threading.Event()
        self._thread = self._spawn_worker()

    def _spawn_worker(self) -> threading.Thread:
        """
        Create the daemon thread running the delivery loop and start it

        :return threading.Thread:
        """

        thread = threading.Thread(target=self._run, name='logalert-worker', daemon=True)
        thread.start()

        return thread

    def submit(self, alert: Alert) -> None:
        """
        Put the event in the queue; the worker thread picks it up from there

        :param alert: The finished event

        :return None:
        """

        self._queue.put(alert)

    def _run(self) -> None:
        """
        Deliver the queue in batches until the stop flag is set

        :return None:
        """

        while not self._stop.is_set():
            batch = self._queue.get_batch(max_items=self._options.max_items_per_batch, timeout=0.2)

            for alert in batch:
                if self._stop.is_set():
                    return

                self._deliver(alert)

    def _deliver(self, alert: Alert) -> None:
        """
        Hand one event to the deliverer, with the losses the queue has to report

        :param alert: The event to deliver

        :return None:
        """

        dropped = self._queue.take_dropped()

        if dropped:
            alert = replace(alert, dropped=dropped)

        self._deliverer.deliver(alert, key=alert.event_id)

    def flush(self, timeout: float | None = None) -> bool:
        """
        Deliver everything in the queue, right here in the calling thread

        :param timeout: Seconds to spend draining; ``shutdown_timeout`` when
            omitted

        :return bool:
        """

        budget = self._options.shutdown_timeout if timeout is None else timeout
        deadline = time.monotonic() + budget
        pending = self._queue.drain(budget)
        ok = True

        for alert in pending:
            if time.monotonic() >= deadline:
                ok = False

                break

            result = self._deliverer.deliver(alert, key=alert.event_id)

            if result.outcome is DeliveryOutcome.DEFERRED:
                ok = False

        return ok

    def close(self) -> None:
        """
        Stop the worker, close the queue and close the deliverer

        :return None:
        """

        self._stop.set()
        self._queue.close()
        self._thread.join(timeout=1.0)
        self._deliverer.close()

    def restart_after_fork(self) -> None:
        """
        Bring the worker back up in the child process

        :return None:
        """

        self._queue.reset_after_fork()
        self._stop = threading.Event()
        self._thread = self._spawn_worker()

    @property
    def queue(self) -> BoundedQueue:
        """
        The queue the events are buffered in

        :return BoundedQueue:
        """

        return self._queue


__all__ = (
    'DirectTransport',
    'Transport',
)
