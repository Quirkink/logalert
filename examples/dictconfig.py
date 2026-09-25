"""
Logalert Example DictConfig

Wiring the handler through ``logging.config.dictConfig``

:copyright: (c) 2025 Aiko Sora
"""

import logging
import logging.config
import os

import logalert

log = logging.getLogger('billing.worker')

# capture_logs=False is the point of this example. init() is told not to install
# its own handler, so the one dictConfig builds is the only one on the root
# logger - two would send every event twice.
logalert.init(
    sinks=[
        logalert.TelegramSink(
            token=os.environ['LOGALERT_TELEGRAM_TOKEN'],
            chat_id=os.environ['LOGALERT_TELEGRAM_CHAT_ID'],
        )
    ],
    environment='production',
    release='billing@1.2.3',
    level='ERROR',
    capture_logs=False,
)

# The fragment is an ordinary dictConfig: version, a handler, a root logger.
# Edit it as you would any other, or merge it into the configuration you already
# have. The handler resolves the client on its own, so nothing has to be passed
# in.
logging.config.dictConfig(logalert.dict_config())


def charge(order_id: int) -> None:
    """
    Fail at something, for the sake of the example

    :param order_id: The order being charged

    :return None:
    """

    raise ConnectionError(f'payment gateway is not answering for order {order_id}')


def main() -> None:
    """
    Report one caught error and one that nobody catches

    :return None:
    """

    try:
        charge(4217)

    except ConnectionError:
        log.exception('failed to charge the order')

    logalert.flush(timeout=5.0)

    raise RuntimeError('the worker ran out of retries and gave up')


if __name__ == '__main__':
    main()
