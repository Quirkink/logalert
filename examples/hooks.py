"""
Logalert Example Hooks

Dropping events and enriching them on the way out

:copyright: (c) 2025 Aiko Sora
"""

import logging
import os
from dataclasses import replace

import logalert
from logalert import Alert

log = logging.getLogger('billing.worker')

NOISE = 'known_noise'


def worth_sending(alert: Alert) -> bool:
    """
    Whether the event goes out at all

    :param alert: The event being considered

    :return bool:
    """

    return alert.tags.get('kind') != NOISE


def with_channel(alert: Alert) -> Alert:
    """
    Attach the deployment channel to every event

    :param alert: The event being enriched

    :return Alert:
    """

    channel = os.environ.get('DEPLOY_CHANNEL', 'dev')

    return replace(alert, tags={**alert.tags, 'channel': channel})


# filter answers a yes-or-no question and enrich transforms the event. They are
# two hooks rather than one on purpose: a single function that both decides and
# rewrites is where the mistakes come from.
logalert.init(
    sinks=[
        logalert.TelegramSink(
            token=os.environ['LOGALERT_TELEGRAM_TOKEN'],
            chat_id=os.environ['LOGALERT_TELEGRAM_CHAT_ID'],
        )
    ],
    environment='production',
    release='billing@1.2.3',
    filter=worth_sending,
    enrich=with_channel,
    breadcrumbs=20,
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
    Send one enriched event, then drop one that the filter refuses

    :return None:
    """

    # Breadcrumbs are collected as the work happens and travel with the next
    # alert. That is what turns "it broke" into "it broke right after these two
    # steps", which is usually worth more than the traceback itself.
    logalert.breadcrumb('cart loaded from the database', category='db')
    logalert.breadcrumb('gateway call started', category='http')

    logalert.tag('region', 'eu-west')
    logalert.context('order', {'id': 4217, 'total': 89.5})
    logalert.user({'id': 'user-9', 'email': 'buyer@example.org'})

    try:
        charge(4217)

    except ConnectionError:
        log.exception('failed to charge the order')

    logalert.flush(timeout=5.0)

    # Everything logged from here on carries this tag, so the filter drops it
    # before it ever reaches a sink
    logalert.tag('kind', NOISE)

    try:
        charge(9999)

    except ConnectionError:
        log.exception('this one never leaves the process')

    logalert.flush(timeout=5.0)


if __name__ == '__main__':
    main()
