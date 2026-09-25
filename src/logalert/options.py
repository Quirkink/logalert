"""
Logalert Options

Library configuration

:copyright: (c) 2025 Aiko Sora
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import MISSING, dataclass, field, fields
from typing import Any, Literal

from .exceptions import InvalidOption
from .types import LEVEL_ORDER, Alert, EventLevel

BackpressurePolicy = Literal['drop_oldest', 'drop_newest', 'block']
BACKPRESSURE_POLICIES: tuple[str, ...] = ('drop_oldest', 'drop_newest', 'block')

DEFAULT_IGNORE_ERRORS: tuple[str, ...] = (
    'KeyboardInterrupt',
    'SystemExit',
    'CancelledError',
)

DEFAULT_IGNORE_LOGGERS: tuple[str, ...] = (
    'uvicorn.access',
    'sqlalchemy.engine',
    'botocore',
    'urllib3.connectionpool',
)

FilterHook = Callable[[Alert], bool]
EnrichHook = Callable[[Alert], Alert]

_SECRET_FIELDS = frozenset({'telegram', 'matrix', 'redis_url'})


@dataclass
class Options:
    """
    Client settings, created via :func:`logalert.init`
    """

    sinks: Sequence[Any] = ()
    telegram: dict[str, Any] | None = None
    matrix: dict[str, Any] | None = None

    redis_url: str | None = None
    project: str | None = None
    stream_maxlen: int = 10_000
    max_delivery_attempts: int = 5
    claim_min_idle_ms: int | None = None
    retry_backoff: tuple[float, ...] = (1.0, 5.0, 15.0, 60.0, 120.0)

    level: str = 'error'
    ignore_errors: Sequence[str] = DEFAULT_IGNORE_ERRORS
    ignore_loggers: Sequence[str] = DEFAULT_IGNORE_LOGGERS
    capture_unraisable: bool = False
    capture_keyboard_interrupt: bool = False
    default_integrations: bool = True

    environment: str = 'production'
    release: str | None = None
    server_name: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    filter: FilterHook | None = None
    enrich: EnrichHook | None = None

    dedup_window: float = 300.0
    dedup_cache_size: int = 4096
    drop_report_threshold: int = 100

    breadcrumbs: int = 100
    breadcrumb_level: str | None = 'info'
    max_breadcrumb_length: int = 1024
    include_breadcrumbs: bool = True

    send_default_pii: bool = False
    include_locals: bool | Literal['safe'] = False
    include_source_context: bool = True

    queue_size: int = 1000
    backpressure: BackpressurePolicy = 'drop_oldest'
    block_timeout: float = 0.05
    max_items_per_batch: int = 50
    send_timeout: float = 10.0
    shutdown_timeout: float = 2.0
    sample_rate: float = 1.0

    debug: bool = False
    attach_stacktrace: bool = False

    max_frames: int = 50
    max_value_length: int = 1024
    fingerprint_path_depth: int = 2
    fingerprint_include_logger: bool = True
    fingerprint_include_lineno: bool = False

    def __post_init__(self) -> None:
        """
        Check the options and fill in what can be derived

        :return None:
        """

        self._validate_level('level', self.level)

        if self.breadcrumb_level is not None:
            self._validate_level('breadcrumb_level', self.breadcrumb_level)

        if not 0.0 <= self.sample_rate <= 1.0:
            raise InvalidOption(
                f'sample_rate must be in the range 0.0..1.0, got {self.sample_rate!r}'
            )

        if self.queue_size <= 0:
            raise InvalidOption(f'queue_size must be > 0, got {self.queue_size!r}')

        if self.dedup_window < 0:
            raise InvalidOption(f'dedup_window cannot be negative, got {self.dedup_window!r}')

        if self.dedup_cache_size <= 0:
            raise InvalidOption(f'dedup_cache_size must be > 0, got {self.dedup_cache_size!r}')

        if self.backpressure not in BACKPRESSURE_POLICIES:
            raise InvalidOption(
                f'backpressure must be one of {BACKPRESSURE_POLICIES}, got {self.backpressure!r}'
            )

        if self.include_locals not in (False, True, 'safe'):
            raise InvalidOption(
                f'include_locals must be False, True or "safe", got {self.include_locals!r}'
            )

        if self.stream_maxlen <= 0:
            raise InvalidOption(f'stream_maxlen must be > 0, got {self.stream_maxlen!r}')

        if self.max_delivery_attempts <= 0:
            raise InvalidOption(
                f'max_delivery_attempts must be > 0, got {self.max_delivery_attempts!r}'
            )

        if not self.retry_backoff:
            raise InvalidOption('retry_backoff cannot be empty')

        if self.redis_url and not self.project:
            raise InvalidOption(
                'project is required when redis_url is set: keys in Redis have to be '
                'isolated from other applications (logalert:{project}:...)'
            )

        if self.claim_min_idle_ms is None:
            self.claim_min_idle_ms = self._derive_claim_min_idle_ms()

        elif self.claim_min_idle_ms <= self._worst_case_delivery_ms():
            raise InvalidOption(
                f'claim_min_idle_ms={self.claim_min_idle_ms} is too small: another consumer '
                f'will claim the message while we are still retrying delivery, producing a '
                f'duplicate. It needs to be more than {self._worst_case_delivery_ms()} ms '
                f'(max_delivery_attempts x (send_timeout + backoff)).'
            )

    def _validate_level(self, field_name: str, value: str) -> None:
        """
        Reject a level name that is not part of our vocabulary

        :param field_name: Field being checked, quoted back in the error
        :param value: Candidate level name

        :return None:
        """

        if value.lower() not in LEVEL_ORDER:
            raise InvalidOption(f'{field_name} must be one of {sorted(LEVEL_ORDER)}, got {value!r}')

    def _worst_case_delivery_ms(self) -> int:
        """
        How many milliseconds we may still be holding the message in the worst
        case

        :return int:
        """

        return int((self.send_timeout + sum(self.retry_backoff)) * 1000)

    def _derive_claim_min_idle_ms(self) -> int:
        """
        Claim threshold - with headroom over the worst-case delivery time

        :return int:
        """

        return int(self._worst_case_delivery_ms() * 1.5)

    def resolved_level(self) -> int:
        """
        Capture level expressed in stdlib ``logging`` terms

        :return int:
        """

        return logging.getLevelNamesMapping()[self.level.upper()]

    def resolved_level_for_breadcrumb(self) -> int:
        """
        Level from which records pile up in the event buffer

        :return int:
        """

        if self.breadcrumb_level is None:
            return logging.NOTSET

        return logging.getLevelNamesMapping()[self.breadcrumb_level.upper()]

    def level_name(self, numeric: int) -> EventLevel:
        """
        Numeric stdlib level - our level vocabulary

        :param numeric: Numeric stdlib level

        :return EventLevel:
        """

        if numeric >= logging.CRITICAL:
            return 'fatal'

        if numeric >= logging.ERROR:
            return 'error'

        if numeric >= logging.WARNING:
            return 'warning'

        if numeric >= logging.INFO:
            return 'info'

        return 'debug'

    def __repr__(self) -> str:
        """
        Prints only what differs from the default, and never secrets

        :return str:
        """

        parts: list[str] = []

        for f in fields(self):
            if f.default is MISSING:
                continue

            value = getattr(self, f.name)

            if value == f.default:
                continue

            if f.name in _SECRET_FIELDS and value:
                parts.append(f'{f.name}=<hidden>')

            else:
                parts.append(f'{f.name}={value!r}')

        return f'Options({", ".join(parts)})'


__all__ = (
    'DEFAULT_IGNORE_LOGGERS',
    'BACKPRESSURE_POLICIES',
    'DEFAULT_IGNORE_ERRORS',
    'EnrichHook',
    'FilterHook',
    'Options',
)
