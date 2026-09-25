"""
Logalert Delivery

Delivering an alert to every sink: retry, circuit breaker, error classification

:copyright: (c) 2025 Aiko Sora
"""

import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from enum import Enum

from ._guard import capture_guard
from .exceptions import SinkPermanentError, SinkRateLimited, SinkTransientError
from .options import Options
from .sinks.base import Sink
from .types import Alert

_log = logging.getLogger('logalert.debug')


class DeliveryOutcome(Enum):
    """
    Outcome of one delivery attempt
    """

    SENT = 'sent'
    DEFERRED = 'deferred'
    DEAD = 'dead'


class SinkReport:
    """
    What happened to one particular sink
    """

    __slots__ = ('error', 'name', 'ok')

    def __init__(self, name: str, ok: bool, error: BaseException | None = None) -> None:
        """
        Record the outcome of one sink's attempt

        :param name: The sink's name
        :param ok: Whether the sink accepted the alert
        :param error: The exception that made it fail, if any

        :return None:
        """

        self.name = name
        self.ok = ok
        self.error = error

    @property
    def permanent(self) -> bool:
        """
        Whether the sink failed with a permanent error

        :return bool:
        """

        return not self.ok and isinstance(self.error, SinkPermanentError)

    def __repr__(self) -> str:
        """
        Text form naming the sink and its state

        :return str:
        """

        state = 'ok' if self.ok else f'fail({type(self.error).__name__})'

        return f'SinkReport({self.name}: {state})'


class DeliveryResult:
    """
    Delivery outcome together with a per-sink report
    """

    __slots__ = ('outcome', 'reports')

    def __init__(self, outcome: DeliveryOutcome, reports: Sequence[SinkReport]) -> None:
        """
        Keep the outcome and freeze the per-sink reports

        :param outcome: The overall delivery outcome
        :param reports: The report of every sink that was attempted

        :return None:
        """

        self.outcome = outcome
        self.reports = tuple(reports)

    @property
    def settled(self) -> bool:
        """
        Every sink either accepted or failed permanently - the message can be
        closed

        :return bool:
        """

        return bool(self.reports) and all(r.ok or r.permanent for r in self.reports)

    def __repr__(self) -> str:
        """
        Text form naming the outcome and the reports

        :return str:
        """

        return f'DeliveryResult({self.outcome.value}, {list(self.reports)})'


class CircuitBreaker:
    """
    Circuit breaker that caps the **rate** of calls to a failing receiver
    """

    def __init__(
        self,
        *,
        failures: int = 10,
        cooldown: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Initialize the breaker in its closed state

        :param failures: Consecutive failures that open the breaker
        :param cooldown: Seconds the breaker stays open before a probe
        :param clock: Monotonic clock used to measure the cooldown

        :return None:
        """

        self._failures = failures
        self._cooldown = cooldown
        self._clock = clock
        self._consecutive = 0
        self._opened_at = 0.0
        self._state = 'closed'
        self._probe_sent = False

    @property
    def state(self) -> str:
        """
        Current breaker state: 'closed', 'open' or 'half_open'

        :return str:
        """

        return self._state

    def allow(self) -> bool:
        """
        Whether we let the next attempt through

        :return bool:
        """

        if self._state == 'closed':
            return True

        elapsed = self._clock() - self._opened_at

        if elapsed < self._cooldown:
            return False

        if self._state == 'open':
            self._state = 'half_open'
            self._probe_sent = False

        if not self._probe_sent:
            self._probe_sent = True

            return True

        return False

    def record(self, ok: bool) -> None:
        """
        Feed the result of one attempt into the breaker

        :param ok: Whether the attempt succeeded

        :return None:
        """

        if ok:
            self._consecutive = 0
            self._state = 'closed'
            self._probe_sent = False

            return

        self._consecutive += 1

        if self._state == 'half_open' or self._consecutive >= self._failures:
            self._state = 'open'
            self._opened_at = self._clock()


class Deliverer:
    """
    Sends a single alert to every sink, honoring each sink's requirements
    """

    def __init__(
        self,
        sinks: Sequence[Sink],
        options: Options,
        *,
        breaker: CircuitBreaker | None = None,
        on_permanent: Callable[[Alert, SinkReport], None] | None = None,
    ) -> None:
        """
        Initialize the deliverer

        :param sinks: The sinks to deliver to
        :param options: Options governing retries and timeouts
        :param breaker: Breaker to share; a fresh one is created when omitted
        :param on_permanent: Called with the alert and the report on a permanent
            failure

        :return None:
        """

        self._sinks = list(sinks)
        self._options = options
        self._breaker = breaker or CircuitBreaker()
        self._on_permanent = on_permanent

    @property
    def breaker(self) -> CircuitBreaker:
        """
        The circuit breaker this deliverer uses

        :return CircuitBreaker:
        """

        return self._breaker

    @property
    def sinks(self) -> Sequence[Sink]:
        """
        The sinks as a snapshot tuple

        :return Sequence[Sink]:
        """

        return tuple(self._sinks)

    def deliver(
        self,
        alert: Alert,
        *,
        key: str,
        only: Sequence[str] | None = None,
    ) -> DeliveryResult:
        """
        Deliver the alert. Never raises: the outcome is reported as a value

        :param alert: The event to deliver
        :param key: Delivery key, reused across retries
        :param only: Restrict delivery to these sink names

        :return DeliveryResult:
        """

        if not self._breaker.allow():
            _log.debug('breaker is open, alert %s deferred', alert.event_id)

            return DeliveryResult(DeliveryOutcome.DEFERRED, [])

        targets = self._sinks

        if only is not None:
            wanted = set(only)
            targets = [s for s in self._sinks if getattr(s, 'name', '') in wanted]

        reports: list[SinkReport] = []

        for sink in targets:
            reports.append(self._deliver_to_sink(sink, alert, key=key))

        sent = [r for r in reports if r.ok]
        deferred = [r for r in reports if not r.ok and isinstance(r.error, SinkTransientError)]

        self._breaker.record(ok=bool(sent) or not reports)

        if deferred:
            return DeliveryResult(DeliveryOutcome.DEFERRED, reports)

        if not sent and reports:
            return DeliveryResult(DeliveryOutcome.DEAD, reports)

        return DeliveryResult(DeliveryOutcome.SENT, reports)

    def sink_names(self) -> tuple[str, ...]:
        """
        Names of the sinks, in delivery order

        :return tuple[str, ...]:
        """

        return tuple(getattr(s, 'name', type(s).__name__) for s in self._sinks)

    def _deliver_to_sink(self, sink: Sink, alert: Alert, *, key: str) -> SinkReport:
        """
        Deliver the alert to one sink, retrying while the failure looks
        temporary

        :param sink: The sink to deliver through
        :param alert: The event to deliver
        :param key: Delivery key, reused across retries

        :return SinkReport:
        """

        name = getattr(sink, 'name', type(sink).__name__)
        attempts = max(1, self._options.max_delivery_attempts)
        last_error: BaseException | None = None

        for attempt in range(attempts):
            try:
                with capture_guard() as allowed:
                    if not allowed:
                        return SinkReport(
                            name, ok=False, error=SinkTransientError('re-entrant call')
                        )

                    sink.send(alert, key=key, timeout=self._options.send_timeout)

                return SinkReport(name, ok=True)

            except SinkRateLimited as exc:
                last_error = exc

                if attempt + 1 >= attempts:
                    break

                self._sleep(min(exc.retry_after, 30.0))

            except SinkTransientError as exc:
                last_error = exc

                if attempt + 1 >= attempts:
                    break

                self._sleep(self._backoff(attempt))

            except SinkPermanentError as exc:
                _log.warning('sink %s failed permanently: %s', name, exc)

                if self._on_permanent is not None:
                    with contextlib.suppress(Exception):
                        self._on_permanent(alert, SinkReport(name, ok=False, error=exc))

                return SinkReport(name, ok=False, error=exc)

            except Exception as exc:
                last_error = SinkTransientError(f'unexpected sink error: {exc!r}')

                if attempt + 1 >= attempts:
                    break

                self._sleep(self._backoff(attempt))

        _log.debug('sink %s rejected alert %s after %d attempts', name, alert.event_id, attempts)

        return SinkReport(name, ok=False, error=last_error)

    def _backoff(self, attempt: int) -> float:
        """
        Delay before the next attempt, taken from the configured backoff
        schedule

        :param attempt: Index of the attempt that just failed, counted from zero

        :return float:
        """

        schedule = self._options.retry_backoff

        if not schedule:
            return 1.0

        return schedule[min(attempt, len(schedule) - 1)]

    def _sleep(self, seconds: float) -> None:
        """
        Pause before the next attempt

        :param seconds: How long to pause, in seconds

        :return None:
        """

        if seconds > 0:
            time.sleep(seconds)

    def close(self) -> None:
        """
        Close every sink, ignoring the errors of individual ones

        :return None:
        """

        for sink in self._sinks:
            with contextlib.suppress(Exception):
                sink.close()


__all__ = (
    'DeliveryOutcome',
    'CircuitBreaker',
    'SinkReport',
    'Deliverer',
)
