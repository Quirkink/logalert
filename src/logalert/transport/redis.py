"""
Logalert Transport Redis

A durable queue on Redis Streams

:copyright: (c) 2025 Aiko Sora
"""

import json
import logging
import threading
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from .._delivery import Deliverer
from .._queue import BoundedQueue
from ..options import Options
from ..sinks.base import require
from ..types import Alert

_log = logging.getLogger('logalert.debug')

SINK_STATE_TTL = 86_400
READ_COUNT = 10
BLOCK_MS = 2_000


def stream_key(project: str) -> str:
    """
    Key of the stream the events are written to

    :param project: Project name the keys are namespaced with

    :return str:
    """

    return f'logalert:{project}:alerts'


def dead_key(project: str) -> str:
    """
    Key of the stream holding messages a sink rejected permanently

    :param project: Project name the keys are namespaced with

    :return str:
    """

    return f'logalert:{project}:dead'


def sink_state_key(project: str, message_id: str) -> str:
    """
    Key of the hash naming the sinks that already accepted a message

    :param project: Project name the keys are namespaced with
    :param message_id: The stream message the hash belongs to

    :return str:
    """

    return f'logalert:{project}:sinkstate:{message_id}'


def group_name(sink_names: Sequence[str]) -> str:
    """
    The group name derived from the sink set

    :param sink_names: Names of the sinks a worker delivers to

    :return str:
    """

    names = sorted({name for name in sink_names if name})

    return '+'.join(names) if names else 'default'


class RedisStreamTransport:
    """
    Puts events on a Redis Stream without blocking the application
    """

    def __init__(
        self,
        client: Any,
        project: str,
        options: Options,
        *,
        fallback: Deliverer | None = None,
    ) -> None:
        """
        Configure the transport and start the producer thread

        :param client: Connected Redis client
        :param project: Project name the stream keys are namespaced with
        :param options: Resolved options: queue size, backpressure, stream
            length
        :param fallback: Deliverer used when ``XADD`` fails, when there is one

        :return None:
        """

        self._client = client
        self._project = project
        self._options = options
        self._key = stream_key(project)
        self._fallback = fallback
        self._queue = BoundedQueue(
            maxsize=options.queue_size,
            policy=options.backpressure,
            block_timeout=options.block_timeout,
        )

        self._stop = threading.Event()
        self._thread = self._spawn()

    def _spawn(self) -> threading.Thread:
        """
        Create the daemon thread running the producer loop and start it

        :return threading.Thread:
        """

        thread = threading.Thread(target=self._run, name='logalert-producer', daemon=True)

        thread.start()

        return thread

    def submit(self, alert: Alert) -> None:
        """
        Put the event in the queue; the producer thread does the ``XADD``

        :param alert: The finished event

        :return None:
        """

        self._queue.put(alert)

    def _run(self) -> None:
        """
        Take batches off the in-memory queue and push them into the stream

        :return None:
        """

        while not self._stop.is_set():
            batch = self._queue.get_batch(max_items=self._options.max_items_per_batch, timeout=0.2)

            for alert in batch:
                if self._stop.is_set():
                    return

                self._push(alert)

    def _push(self, alert: Alert) -> None:
        """
        Append one alert to the stream

        :param alert: The event to push

        :return None:
        """

        dropped = self._queue.take_dropped()

        if dropped:
            alert = replace(alert, dropped=dropped)

        try:
            self._client.xadd(
                self._key,
                {'payload': json.dumps(alert.to_dict(), ensure_ascii=False)},
                maxlen=self._options.stream_maxlen,
                approximate=True,
            )

        except Exception as exc:
            _log.warning('XADD failed (%s), sending directly', exc)

            if self._fallback is not None:
                self._fallback.deliver(alert, key=alert.event_id)

    def flush(self, timeout: float | None = None) -> bool:
        """
        Push everything still queued to the stream, right here in the calling
        thread

        :param timeout: Seconds to spend draining; ``shutdown_timeout`` when
            omitted

        :return bool:
        """

        import time

        budget = self._options.shutdown_timeout if timeout is None else timeout
        deadline = time.monotonic() + budget
        ok = True

        for alert in self._queue.drain(budget):
            if time.monotonic() >= deadline:
                ok = False

                break

            self._push(alert)

        return ok

    def close(self) -> None:
        """
        Stop the producer thread and close the queue

        :return None:
        """

        self._stop.set()
        self._queue.close()
        self._thread.join(timeout=1.0)

    def restart_after_fork(self) -> None:
        """
        Bring the producer back up in the child process

        :return None:
        """

        self._queue.reset_after_fork()
        self._stop = threading.Event()
        self._thread = self._spawn()


class StreamConsumer:
    """
    Reads the stream as part of a group and delivers the alerts
    """

    def __init__(
        self,
        client: Any,
        project: str,
        deliverer: Deliverer,
        options: Options,
        *,
        consumer_name: str | None = None,
    ) -> None:
        """
        Configure the consumer

        :param client: Connected Redis client
        :param project: Project name the stream keys are namespaced with
        :param deliverer: Delivers each event to the sinks
        :param options: The resolved options: claim threshold, stream length
        :param consumer_name: Name of this consumer inside the group; generated
            when omitted

        :return None:
        """

        self._client = client
        self._project = project
        self._deliverer = deliverer
        self._options = options
        self._key = stream_key(project)
        self._group = group_name(deliverer.sink_names())
        self._consumer = consumer_name or f'logalert-{id(self):x}'

    @property
    def group(self) -> str:
        """
        Name of the consumer group, derived from the sink set

        :return str:
        """

        return self._group

    def ensure_group(self) -> None:
        """
        Create the group, starting from the beginning of the stream

        :return None:
        """

        try:
            self._client.xgroup_create(self._key, self._group, id='0', mkstream=True)

        except Exception as exc:
            if 'BUSYGROUP' not in str(exc):
                raise

    def run(self, stop: threading.Event) -> None:
        """
        The main loop. Returns once ``stop`` is set

        :param stop: The event that asks the loop to stop

        :return None:
        """

        self.ensure_group()

        while not stop.is_set():
            try:
                self.claim_stale()
                self.process_new(stop)

            except Exception as exc:
                _log.warning('stream read failed: %s', exc)
                stop.wait(1.0)

    def process_new(self, stop: threading.Event) -> None:
        """
        Read the messages that have not been delivered to this group yet

        :param stop: The event that asks the loop to stop

        :return None:
        """

        response = self._client.xreadgroup(
            self._group,
            self._consumer,
            {self._key: '>'},
            count=READ_COUNT,
            block=BLOCK_MS,
        )

        for _stream, messages in response or ():
            for message_id, fields in messages:
                if stop.is_set():
                    return

                self._handle(_decode_id(message_id), fields)

    def claim_stale(self) -> None:
        """
        Claim messages left hanging on a consumer that died

        :return None:
        """

        min_idle = self._options.claim_min_idle_ms or 0

        try:
            claimed = self._client.xautoclaim(
                self._key,
                self._group,
                self._consumer,
                min_idle_time=min_idle,
                start_id='0-0',
                count=READ_COUNT,
            )

        except Exception as exc:
            _log.debug('XAUTOCLAIM is unavailable: %s', exc)

            return

        messages = claimed[1] if len(claimed) > 1 else ()

        for message_id, fields in messages:
            if fields:
                self._handle(_decode_id(message_id), fields)

    def _handle(self, message_id: str, fields: dict[Any, Any]) -> None:
        """
        Deliver one stream message and acknowledge it once the outcome is final

        :param message_id: The stream message being handled
        :param fields: The message fields, holding the payload

        :return None:
        """

        raw = fields.get('payload') or fields.get(b'payload')

        if raw is None:
            _log.warning('message %s has no payload, skipping', message_id)
            self._ack(message_id)

            return

        try:
            alert = Alert.from_dict(json.loads(raw))

        except Exception as exc:
            _log.warning('could not parse message %s: %s', message_id, exc)
            self._to_dead_letter(message_id, raw, '<unparsed>')
            self._ack(message_id)

            return

        already = self._delivered_sinks(message_id)
        pending = [name for name in self._deliverer.sink_names() if name not in already]

        if not pending:
            self._ack(message_id)

            return

        result = self._deliverer.deliver(alert, key=message_id, only=pending)

        for report in result.reports:
            if report.ok or report.permanent:
                self._mark_sink(message_id, report.name)

            if report.permanent:
                self._to_dead_letter(message_id, raw, report.name)

        if result.settled or not result.reports:
            self._ack(message_id)

    def _ack(self, message_id: str) -> None:
        """
        Acknowledge the message, so it leaves the group's pending list

        :param message_id: The stream message to acknowledge

        :return None:
        """

        try:
            self._client.xack(self._key, self._group, message_id)

        except Exception as exc:
            _log.warning('XACK %s did not go through: %s', message_id, exc)

    def _delivered_sinks(self, message_id: str) -> set[str]:
        """
        Which sinks have already accepted this message

        :param message_id: The stream message being handled

        :return set[str]:
        """

        try:
            stored = self._client.hkeys(sink_state_key(self._project, message_id))

        except Exception:
            return set()

        return {key.decode() if isinstance(key, bytes) else str(key) for key in stored}

    def _mark_sink(self, message_id: str, sink_name: str) -> None:
        """
        Record that one sink has accepted the message

        :param message_id: The stream message being handled
        :param sink_name: The sink that accepted it

        :return None:
        """

        key = sink_state_key(self._project, message_id)

        try:
            self._client.hset(key, sink_name, '1')

            self._client.expire(key, SINK_STATE_TTL)
        except Exception:
            pass

    def _to_dead_letter(self, message_id: str, raw: Any, sink_name: str) -> None:
        """
        Set aside a message that a sink rejected permanently

        :param message_id: The stream message being set aside
        :param raw: The original payload, stored as it was received
        :param sink_name: The sink that rejected it

        :return None:
        """

        try:
            self._client.xadd(
                dead_key(self._project),
                {'payload': raw, 'original_id': message_id, 'sink': sink_name},
                maxlen=self._options.stream_maxlen,
                approximate=True,
            )

        except Exception as exc:
            _log.warning('could not move %s to the dead-letter: %s', message_id, exc)


def build_client(url: str) -> Any:
    """
    Create a Redis client, with a sensible error if the extra is not installed

    :param url: Redis connection URL

    :return Any:
    """

    redis = require('redis', 'redis')

    return redis.Redis.from_url(url, decode_responses=False)


def _decode_id(value: Any) -> str:
    """
    The message id as text

    :param value: The message id as Redis returned it

    :return str:
    """

    return value.decode() if isinstance(value, bytes) else str(value)


__all__ = (
    'RedisStreamTransport',
    'StreamConsumer',
    'sink_state_key',
    'group_name',
    'stream_key',
    'dead_key',
)
