"""
Logalert Example Matrix

The same service, delivering into a Matrix room instead of a chat

:copyright: (c) 2025 Aiko Sora
"""

import logging
import os

import logalert

log = logging.getLogger('billing.worker')

# MatrixSink speaks the Client-Server REST API, so this needs no asyncio and no
# key store, unlike the sink for encrypted rooms. Plain ``logalert check`` will
# say whether the room is encrypted before you find out the hard way.
logalert.init(
    sinks=[
        logalert.MatrixSink(
            homeserver=os.environ['LOGALERT_MATRIX_HOMESERVER'],
            room_id=os.environ['LOGALERT_MATRIX_ROOM_ID'],
            access_token=os.environ['LOGALERT_MATRIX_ACCESS_TOKEN'],
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


def main() -> None:
    """
    Report one caught error and one that nobody catches

    :return None:
    """

    logalert.tag('region', 'eu-west')

    try:
        charge(4217)

    except ConnectionError:
        log.exception('failed to charge the order')

    logalert.flush(timeout=5.0)

    raise RuntimeError('the worker ran out of retries and gave up')


if __name__ == '__main__':
    main()
