"""
Logalert Example Telegram

A small service wired to the Telegram sink

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
    environment='production',
    release='billing@1.2.3',
    level='ERROR',
)


def charge(order_id: int) -> None:
    """
    Fail at something, for the sake of the example

    :param order_id: The order being charged

    :return None:
    """

    raise ConnectionError(f'payment gateway is not answering for order {order_id}')


def handled_error() -> None:
    """
    The everyday case: the error is caught, and we report it ourselves

    :return None:
    """

    logalert.tag('region', 'eu-west')

    try:
        charge(4217)

    except ConnectionError:
        log.exception('failed to charge the order')


def unhandled_error() -> None:
    """
    The case nobody caught: the exception escapes and the process is about to
    die

    :return None:
    """

    raise RuntimeError('the worker ran out of retries and gave up')


def main() -> None:
    """
    Run the handled case, then let an unhandled one escape

    :return None:
    """

    handled_error()

    logalert.flush(timeout=5.0)

    unhandled_error()


if __name__ == '__main__':
    main()
