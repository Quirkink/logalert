"""
Logalert Example Redis Worker

A queue that outlives the process that produced the alert

:copyright: (c) 2025 Aiko Sora
"""

import logging
import os

import logalert

log = logging.getLogger('billing.worker')

logalert.init(
    sinks=[
        logalert.TelegramSink(
            token=os.environ['LOGALERT_TELEGRAM_TOKEN'],
            chat_id=os.environ['LOGALERT_TELEGRAM_CHAT_ID'],
        )
    ],
    redis_url=os.environ.get('LOGALERT_REDIS_URL', 'redis://localhost:6379/0'),
    project='billing',
    environment='production',
    release='billing@1.2.3',
    level='ERROR',
)

# With redis_url the alert lands in a Redis stream instead of going out right
# away, so this process may exit the moment it is done - a crashed worker no
# longer means a lost alert. Something has to drain the stream, and that is a
# separate process:
#
#     logalert worker --redis-url redis://localhost:6379/0 --project billing
#
# It must name the same project: the project namespaces the keys, so two
# different ones are two different queues and the alerts would sit there.


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
