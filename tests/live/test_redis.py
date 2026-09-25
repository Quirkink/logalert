"""
Logalert Tests Redis

The Redis transport against a real Redis: a message is never lost between the
read and the ack.

:copyright: (c) 2025 Aiko Sora
"""

import json
import os
import pathlib
import sys
import threading
import time

import pytest

from logalert._delivery import Deliverer
from logalert.exceptions import SinkPermanentError
from logalert.options import Options
from logalert.transport.redis import (
    RedisStreamTransport,
    StreamConsumer,
    build_client,
    dead_key,
    group_name,
    sink_state_key,
    stream_key,
)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from conftest import RecordingSink, make_alert

pytestmark = pytest.mark.live

PROJECT = 'logalert-test'
FAST = {'send_timeout': 0.001, 'retry_backoff': (0.001,), 'max_delivery_attempts': 1}


@pytest.fixture
def client():  # type: ignore[no-untyped-def]
    """
    A Redis client against the test database, emptied before and after

    :return None:
    """

    url = os.environ.get('LOGALERT_TEST_REDIS_URL', 'redis://localhost:6379/0')

    redis = build_client(url)
    redis.flushdb()

    yield redis

    redis.flushdb()
    redis.close()


def _consumer(redis, sink, *, name='worker-1', **kwargs):  # type: ignore[no-untyped-def]
    """
    Build a stream consumer over a single sink

    :param redis: The Redis client
    :param sink: The sink the deliverer hands the alerts to
    :param name: The name of the consumer
    :param kwargs: The options that replace the fast defaults

    :return None:
    """

    options = Options(**{**FAST, **kwargs})
    deliverer = Deliverer([sink], options)

    return StreamConsumer(redis, PROJECT, deliverer, options, consumer_name=name)


class TestProducing:
    """
    Producing: a submitted alert reaches the stream
    """

    def test_message_lands_in_stream(self, client) -> None:  # type: ignore[no-untyped-def]
        """
        A submitted alert lands in the stream
        """

        options = Options(**FAST)
        transport = RedisStreamTransport(client, PROJECT, options)

        try:
            transport.submit(make_alert(message='check'))

            assert transport.flush(5.0) is True

        finally:
            transport.close()

        assert client.xlen(stream_key(PROJECT)) == 1

    def test_stream_is_capped(self, client) -> None:  # type: ignore[no-untyped-def]
        """
        Without MAXLEN the stream would grow without bound
        """

        options = Options(**FAST, stream_maxlen=100)

        transport = RedisStreamTransport(client, PROJECT, options)

        try:
            for index in range(2000):
                transport.submit(make_alert(message=f'number {index}'))

            transport.flush(10.0)

        finally:
            transport.close()

        # Checking that the trimming happened at all
        size = client.xlen(stream_key(PROJECT))

        assert size < 1000, f'the stream is not being trimmed: {size} entries'


class TestConsuming:
    """
    Consuming: the read, the ack, and the claiming back of a stalled message
    """

    def test_delivery_acknowledges(self, client) -> None:  # type: ignore[no-untyped-def]
        """
        A delivered message is acknowledged
        """

        options = Options(**FAST)

        transport = RedisStreamTransport(client, PROJECT, options)

        transport.submit(make_alert())
        transport.flush(5.0)
        transport.close()

        sink = RecordingSink()

        consumer = _consumer(client, sink)

        consumer.ensure_group()
        consumer.process_new(threading.Event())

        # Checking that the message was delivered and then acknowledged
        assert len(sink.sent) == 1

        pending = client.xpending(stream_key(PROJECT), consumer.group)

        assert pending['pending'] == 0, 'the message was left unacknowledged'

    def test_unacked_message_is_redelivered(self, client) -> None:  # type: ignore[no-untyped-def]
        """
        A crash between the read and the XACK does not lose the alert
        """

        options = Options(**FAST)
        transport = RedisStreamTransport(client, PROJECT, options)

        transport.submit(make_alert())
        transport.flush(5.0)
        transport.close()

        key = stream_key(PROJECT)
        group = group_name(['recording'])

        client.xgroup_create(key, group, id='0', mkstream=True)
        client.xreadgroup(group, 'crashed-worker', {key: '>'}, count=1)

        time.sleep(0.05)

        sink = RecordingSink()
        rescuer = _consumer(client, sink, name='rescuer')

        rescuer.claim_stale()

        assert len(sink.sent) == 1, 'the pending message was not claimed'

    def test_redelivery_does_not_duplicate_to_successful_sink(self, client) -> None:  # type: ignore[no-untyped-def]
        """
        A sink that accepted the message the first time must not receive it
        again
        """

        options = Options(**FAST)

        transport = RedisStreamTransport(client, PROJECT, options)
        transport.submit(make_alert())
        transport.flush(5.0)
        transport.close()

        healthy = RecordingSink()
        healthy.name = 'healthy'

        flaky = RecordingSink(fail_with=SinkPermanentError('recipient refused'))
        flaky.name = 'flaky'

        deliverer = Deliverer([healthy, flaky], options)

        consumer = StreamConsumer(client, PROJECT, deliverer, options, consumer_name='w1')
        consumer.ensure_group()
        consumer.process_new(threading.Event())

        # Checking that only the failing sink was retried
        assert len(healthy.sent) == 1
        assert client.xpending(stream_key(PROJECT), consumer.group)['pending'] == 0
        assert client.xlen(dead_key(PROJECT)) == 1

        message_id = client.xrange(stream_key(PROJECT))[0][0].decode()
        state = client.hkeys(sink_state_key(PROJECT, message_id))

        assert b'healthy' in state or 'healthy' in state

    def test_permanently_failed_message_is_not_retried_forever(self, client) -> None:  # type: ignore[no-untyped-def]
        """
        A permanently failed message is acknowledged rather than left pending
        """

        options = Options(**FAST)

        transport = RedisStreamTransport(client, PROJECT, options)
        transport.submit(make_alert())
        transport.flush(5.0)
        transport.close()

        sink = RecordingSink(fail_with=SinkPermanentError('chat deleted'))

        consumer = _consumer(client, sink)
        consumer.ensure_group()
        consumer.process_new(threading.Event())

        assert client.xpending(stream_key(PROJECT), consumer.group)['pending'] == 0


class TestDegradation:
    """
    Degradation: Redis being down must not mean an alert lost
    """

    def test_broken_redis_falls_back_to_direct_delivery(self) -> None:  # type: ignore[no-untyped-def]
        """
        If Redis is unreachable, the alert goes out anyway
        """

        sink = RecordingSink()

        options = Options(**FAST)

        deliverer = Deliverer([sink], options)

        class BrokenRedis:
            """
            Stands in for a Redis client whose connection has gone
            """

            def xadd(self, *args: object, **kwargs: object) -> None:
                """
                Fail the way a client with no connection does

                :param args: Accepted and ignored, so the signature matches the
                    real client
                :param kwargs: Accepted and ignored, for the same reason

                :return None:
                """

                raise ConnectionError('Redis is unreachable')

        transport = RedisStreamTransport(BrokenRedis(), PROJECT, options, fallback=deliverer)

        try:
            transport.submit(make_alert())

            assert transport.flush(5.0) is True

        finally:
            transport.close()

        assert len(sink.sent) == 1, 'the alert was lost along with Redis'


class TestKeyLayout:
    """
    Key layout: the namespace, the group name and the payload
    """

    def test_keys_are_namespaced_by_project(self, client) -> None:  # type: ignore[no-untyped-def]
        """
        A Redis shared with other applications is the norm, so keys must be
        isolated
        """

        options = Options(**FAST)

        transport = RedisStreamTransport(client, PROJECT, options)
        transport.submit(make_alert())
        transport.flush(5.0)
        transport.close()

        # Checking that every key carries our project prefix
        keys = [k.decode() for k in client.keys('logalert:*')]

        assert keys
        assert all(key.startswith(f'logalert:{PROJECT}:') for key in keys)

    def test_group_name_encodes_sink_set(self) -> None:
        """
        Within one group a message reaches exactly one consumer
        """

        assert group_name(['telegram', 'matrix']) == 'matrix+telegram'
        assert group_name(['matrix', 'telegram']) == 'matrix+telegram'
        assert group_name(['telegram']) != group_name(['telegram', 'matrix'])

    def test_payload_round_trips(self, client) -> None:  # type: ignore[no-untyped-def]
        """
        The alert goes through JSON losslessly - otherwise event fields are lost
        """

        alert = make_alert(message='check', tags={'region': 'eu'})
        options = Options(**FAST)

        transport = RedisStreamTransport(client, PROJECT, options)
        transport.submit(alert)
        transport.flush(5.0)
        transport.close()

        # Checking that the fields survived the round trip
        entries = client.xrange(stream_key(PROJECT))
        restored = json.loads(entries[0][1][b'payload'])

        assert restored['event_id'] == alert.event_id
        assert restored['tags'] == {'region': 'eu'}


class TestDedupe:
    """
    The shared suppression window: the counter has to survive the window it
    belongs to
    """

    def test_repeats_are_suppressed_and_the_summary_survives(self, client) -> None:  # type: ignore[no-untyped-def]
        """
        The first event sends, repeats are held back, and the summary carries
        the count
        """

        from logalert._dedupe import RedisDedupe

        window = 1.0
        fingerprint = 'live-fingerprint'

        dedupe = RedisDedupe(client, project=PROJECT, window=window)

        # Checking that the first sighting goes out at once
        first = dedupe.check(fingerprint)

        assert first.send is True
        assert first.occurrences == 1

        for expected in (2, 3, 4):
            held = dedupe.check(fingerprint)

            assert held.send is False
            assert held.occurrences == expected

        time.sleep(window + 0.3)

        # Checking that the count outlived the window and reached the summary
        summary = dedupe.check(fingerprint)

        assert summary.send is True
        assert summary.occurrences == 4, 'the summary lost the count'

    def test_the_window_reopens_cleanly(self, client) -> None:  # type: ignore[no-untyped-def]
        """
        After a summary the counter starts over rather than continuing to climb
        """

        from logalert._dedupe import RedisDedupe

        window = 1.0
        fingerprint = 'live-fingerprint-second'

        dedupe = RedisDedupe(client, project=PROJECT, window=window)
        dedupe.check(fingerprint)

        time.sleep(window + 0.3)

        # Checking that a fresh window begins from one, not from the previous
        # count
        assert dedupe.check(fingerprint).occurrences == 1

        assert dedupe.check(fingerprint).occurrences == 2


__all__ = (
    'TestDegradation',
    'TestConsuming',
    'TestKeyLayout',
    'TestProducing',
    'TestDedupe',
    'client',
)
