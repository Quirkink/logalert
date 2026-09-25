"""
Logalert Tests Dedupe

Suppression window: repeats are suppressed, but the count is never lost.

:copyright: (c) 2025 Aiko Sora
"""

from logalert._dedupe import InProcessDedupe, build_dedupe


class FakeClock:
    """
    Controllable time - otherwise tests around windows become slow and flaky
    """

    def __init__(self) -> None:
        """
        Start the clock at a fixed, arbitrary moment

        :return None:
        """

        self.now = 1000.0

    def __call__(self) -> float:
        """
        Read the clock, so it can stand where a time function is expected

        :return float:
        """

        return self.now

    def advance(self, seconds: float) -> None:
        """
        Move the fake clock forward

        :param seconds: How far to advance

        :return None:
        """

        self.now += seconds


def _dedupe(window: float = 300.0, size: int = 100) -> tuple[InProcessDedupe, FakeClock]:
    """
    Build a deduplicator with a controllable clock

    :param window: The suppression window, in seconds
    :param size: The number of fingerprints kept in the cache

    :return tuple[InProcessDedupe, FakeClock]:
    """

    clock = FakeClock()

    return InProcessDedupe(window=window, maxsize=size, clock=clock), clock


def test_first_event_is_sent_immediately() -> None:
    """
    The first event of a fingerprint goes out at once
    """

    dedupe, _ = _dedupe()

    result = dedupe.check('fingerprint')

    # Checking that nothing was held back
    assert result.send is True
    assert result.occurrences == 1


def test_repeats_within_window_are_suppressed() -> None:
    """
    Repeats inside the window are held back, but still counted
    """

    dedupe, clock = _dedupe(window=300.0)
    dedupe.check('fingerprint')

    for expected in (2, 3, 4):
        clock.advance(10)
        result = dedupe.check('fingerprint')

        # Checking that only the counter moves
        assert result.send is False
        assert result.occurrences == expected


def test_summary_arrives_on_first_event_after_window() -> None:
    """
    The key difference from a silent drop: the counter does not disappear
    """

    dedupe, clock = _dedupe(window=300.0)
    dedupe.check('fingerprint')

    for _ in range(46):
        clock.advance(1)
        dedupe.check('fingerprint')

    clock.advance(400)
    result = dedupe.check('fingerprint')

    # Checking that the summary carries the full count
    assert result.send is True
    assert result.occurrences == 47


def test_distinct_fingerprints_are_never_suppressed() -> None:
    """
    Different fingerprints never suppress one another
    """

    dedupe, _ = _dedupe()

    assert dedupe.check('one').send is True
    assert dedupe.check('another').send is True
    assert dedupe.check('third').send is True


def test_zero_window_disables_suppression() -> None:
    """
    A zero window turns suppression off altogether
    """

    dedupe = build_dedupe(window=0, maxsize=10)

    assert dedupe.check('fingerprint').send is True
    assert dedupe.check('fingerprint').send is True


def test_cache_does_not_grow_unbounded() -> None:
    """
    The cache does not grow without bound
    """

    dedupe, _ = _dedupe(size=10)

    for index in range(1000):
        dedupe.check(f'fingerprint-{index}')

    # Checking that the cache stayed within its size
    assert len(dedupe._entries) <= 10


def test_lru_keeps_recent_fingerprints() -> None:
    """
    A frequently repeating fingerprint must not be evicted by rare ones
    """

    dedupe, clock = _dedupe(window=300.0, size=3)
    dedupe.check('hot')

    for index in range(3):
        clock.advance(1)
        dedupe.check('hot')
        dedupe.check(f'cold-{index}')

    # Checking that the hot fingerprint is still suppressed
    assert dedupe.check('hot').send is False


__all__ = (
    'test_summary_arrives_on_first_event_after_window',
    'test_distinct_fingerprints_are_never_suppressed',
    'test_repeats_within_window_are_suppressed',
    'test_zero_window_disables_suppression',
    'test_first_event_is_sent_immediately',
    'test_cache_does_not_grow_unbounded',
    'test_lru_keeps_recent_fingerprints',
    'FakeClock',
)
