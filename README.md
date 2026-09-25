# Logalert

**logalert** is a library that intercepts errors, stack traces, and unhandled exceptions in a Python application and sends them to a configured messenger.

![PyPI - Version](https://img.shields.io/pypi/v/logalert)
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/logalert)

___

Key features of this module:

* **Sinks** - Telegram, Matrix, an encrypted Matrix room, stderr, or your own
* **Automatic capture** - logging, `excepthook`, threads, asyncio, unraisable hooks
* **Deduplication** - a suppression window that reports a summary instead of flooding
* **Durable delivery** - an optional Redis Streams queue that survives a restart
* **No required dependencies** - extras only, so it never conflicts with the host project

## Example

```python
import logalert

logalert.init(telegram={'token': '...', 'chat_id': '...'})
```

## Installation

```bash
pip install logalert[telegram]        # Telegram
pip install logalert[matrix]          # Matrix, unencrypted room
pip install logalert[matrix-e2ee]     # Matrix with E2E encryption
pip install logalert[redis]           # durable queue
pip install logalert[all]
```

## Quick start

### Telegram

A bot cannot message first: until you press `/start`, `sendMessage` returns
`chat not found`. You can find `chat_id` via
`https://api.telegram.org/bot<TOKEN>/getUpdates` after writing to the bot.

```python
import logalert

logalert.init(
    telegram={
        'token': '123456:ABC-DEF...',
        'chat_id': '-1001234567890',
        # "message_thread_id": 42,   # when alerts go to a forum topic
    },
    environment='prod',
    release='order-service@1.4.2',
)
```

### Matrix

```python
logalert.init(
    matrix={
        'homeserver': 'https://matrix.example.org',
        'room_id': '!abcdef:example.org',
        'access_token': 'syt_...',
    }
)
```

Both sinks can be configured at once - the message goes to each:

```python
logalert.init(telegram={...}, matrix={...})
```

### Matrix in an encrypted room

The plain sink can only send to unencrypted rooms and will **refuse loudly** to work with
an encrypted one rather than send plaintext. E2EE needs an extra package and a fuller set
of parameters:

```bash
pip install logalert[matrix-e2ee]
```

```python
logalert.init(
    matrix={
        'homeserver': 'https://matrix.example.org',
        'room_id': '!abcdef:example.org',
        'access_token': 'syt_...',
        'e2ee': True,
        'user_id': '@bot:example.org',  # required
        'device_id': 'LOGALERT',  # required, see below
        'store_path': '/var/lib/logalert/matrix-store',  # required
    }
)
```

### Checking your setup

```bash
logalert check     # validates tokens and room access without sending anything
logalert test      # sends a test alert
```

For Matrix, `logalert check` also reports whether the room is encrypted and tells you which
extra is needed.

## What gets captured

| Source | What it catches |
|---|---|
| `logging` | every record at `level` (default `ERROR`) and above, together with `exc_info` |
| `sys.excepthook` | the exception that killed the main thread |
| `threading.excepthook` | an exception inside a `Thread` |
| asyncio handler | an exception in a task or event loop callback |
| `sys.unraisablehook` | exceptions in `__del__` (off by default, enable with `capture_unraisable=True`) |

Not sent by default: `KeyboardInterrupt`, `SystemExit`, `asyncio.CancelledError`.

## Sending manually

```python
try:
    charge(order)
except Exception as exc:
    logalert.alert(exc)
    raise

logalert.alert('payment gateway did not respond within 30 seconds', level='warning')
```

## Context

```python
logalert.tag('region', 'eu-west')
logalert.context('order', {'id': 4217, 'total': 1990})
logalert.breadcrumb('cache miss', category='cache')

logalert.alert(exc)
```

`breadcrumb` writes into a ring buffer that is attached to the next alert - this shows what
was happening before the error, not just the traceback itself.

## Hooks

Two hooks with one job each, instead of one overloaded one:

```python
def only_payments(alert: logalert.Alert) -> bool:
    return 'payment' in alert.logger


def add_region(alert: logalert.Alert) -> logalert.Alert:
    from dataclasses import replace

    return replace(alert, tags={**alert.tags, 'dc': 'eu-1'})


logalert.init(
    telegram={...},
    filter=only_payments,  # return False to drop the event
    enrich=add_region,  # return a new event
)
```

## Durable delivery via Redis

By default alerts go out from a background thread inside the process: fast and with no
infrastructure, but if the process dies the unsent queue is lost.

When losing alerts is not acceptable, turn on Redis:

```python
logalert.init(
    telegram={...},
    redis_url='redis://localhost:6379/0',
    project='order-service',  # required: keys are isolated from other applications
)
```

```bash
logalert worker --redis-url redis://localhost:6379/0 --project order-service
```

Requires **Redis 6.2+**

## Deduplication

Custom grouping is set explicitly:

```python
from dataclasses import replace

logalert.alert(replace(captured, fingerprint=('my-group',)))
```

With Redis the window is shared across all workers. Without it, each process has its own.

## Privacy

By default only the technical part leaves the process: exception type, traceback, logger,
host, version. Local variables in the traceback are **not** sent (`include_locals=False`) -
they regularly contain passwords, tokens and personal data, so they stay out of the alert
unless you ask for them.

What is available when you need more:

```python
logalert.init(
    telegram={...},
    include_locals='safe',  # primitives only, denylisted names skipped
    send_default_pii=True,  # allow user fields
    include_source_context=True,
)
```

## Writing your own sink

The `Sink` protocol is public

```python
import json
import pathlib

from logalert import Alert


class FileSink:
    name = 'file'
    idempotent = False

    def __init__(self, path: str = 'alerts.jsonl') -> None:
        self._path = pathlib.Path(path)

    def send(self, alert: Alert, *, key: str, timeout: float) -> None:
        with self._path.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(alert.to_dict(), ensure_ascii=False) + '\n')

    def healthcheck(self) -> None:
        return None

    def close(self) -> None:
        return None
```

```python
import logalert

logalert.init(sinks=[FileSink('/var/log/alerts.jsonl')])
```

A sink is handed over as an object, and it carries its own configuration.

The `send()` contract: called only from the worker thread; must be synchronous and finish
within `timeout`; reports failure **by raising**, not by returning `False`:

- `SinkTransientError` - temporary failure, retry;
- `SinkRateLimited(retry_after=N)` - the receiver asks you to wait N seconds;
- `SinkPermanentError` - retrying is pointless, goes to the dead-letter stream.

A sink must not log above `DEBUG` - that would open an amplification loop.

## CLI

```bash
logalert check      # validate the configuration
logalert test       # send a test alert
logalert worker     # run the consumer for Redis mode
logalert topics     # discover chat_id and message_thread_id for forum topics
logalert version
```

## Troubleshooting

If alerts are not arriving, turn on the library's debug output:

```python
logalert.init(telegram={...}, debug=True)
```

It shows what was filtered out, what was suppressed by deduplication, and with what error
delivery failed.

## License

[MIT](https://github.com/Quirkink/logalert/blob/main/LICENSE)
