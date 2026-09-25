"""
Logalert Tests Privacy

Privacy: what leaves the process, and what never leaves it.

:copyright: (c) 2025 Aiko Sora
"""

from conftest import make_alert

from logalert._privacy import REDACTED, redact_text, scrub
from logalert.options import Options
from logalert.types import ExceptionInfo, Frame, Stacktrace

TOKEN = '123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw'
SYNAPSE_TOKEN = 'syt_dGhpc19pc19hX3Rlc3RfdG9rZW4'


class TestSecretRedaction:
    """
    Secrets get scrubbed always,
    whatever the settings say
    """

    def test_telegram_bot_token(self) -> None:
        """
        A bot token does not survive redaction
        """

        assert TOKEN not in redact_text(f'POST /bot{TOKEN}/sendMessage')

    def test_matrix_access_token(self) -> None:
        """
        A Synapse access token does not survive redaction
        """

        assert SYNAPSE_TOKEN not in redact_text(f'Authorization: Bearer {SYNAPSE_TOKEN}')

    def test_bearer_header(self) -> None:
        """
        Whatever follows `Bearer` is hidden, in any alphabet
        """

        assert 'συνθηματικό' not in redact_text('Authorization: Bearer συνθηματικό')

    def test_password_assignment(self) -> None:
        """
        A password is hidden in both the plain and the JSON form
        """

        assert 'hunter2' not in redact_text('password=hunter2')
        assert 'hunter2' not in redact_text('"password": "hunter2"')

    def test_private_key(self) -> None:
        """
        A PEM private key header is hidden
        """

        assert 'PRIVATE KEY' not in redact_text('-----BEGIN RSA PRIVATE KEY-----')

    def test_redaction_survives_pii_enabled(self) -> None:
        """
        Even with `send_default_pii=True` the tokens stay scrubbed
        """

        alert = make_alert(message=f'never made it to /bot{TOKEN}/sendMessage')
        cleaned = scrub(alert, Options(send_default_pii=True))

        assert TOKEN not in cleaned.message


class TestScrubbing:
    """
    Scrubbing: what is replaced, what is kept, and the shape that survives
    """

    def test_denylisted_keys_are_replaced(self) -> None:
        """
        Keys on the denylist are replaced, and their neighbours are left alone
        """

        alert = make_alert(contexts={'auth': {'password': 'hunter2', 'user': 'ivan'}})
        cleaned = scrub(alert, Options())

        assert cleaned.contexts['auth']['password'] == REDACTED
        assert cleaned.contexts['auth']['user'] == 'ivan'

    def test_composite_key_names(self) -> None:
        """
        `db_password`, `stripe_api_key` are secrets too
        """

        alert = make_alert(contexts={'cfg': {'db_password': 'x', 'stripe_api_key': 'y'}})
        cleaned = scrub(alert, Options())

        assert cleaned.contexts['cfg']['db_password'] == REDACTED
        assert cleaned.contexts['cfg']['stripe_api_key'] == REDACTED

    def test_key_matching_ignores_case_and_separators(self) -> None:
        """
        Matching ignores case and the separator between words
        """

        alert = make_alert(contexts={'cfg': {'API-KEY': 'y', 'accessToken': 'z'}})
        cleaned = scrub(alert, Options())

        assert cleaned.contexts['cfg']['API-KEY'] == REDACTED
        assert cleaned.contexts['cfg']['accessToken'] == REDACTED

    def test_locals_are_denied_by_name(self) -> None:
        """
        `password = "hunter2"` is not caught by scanning values - only by name
        """

        exc = ExceptionInfo(
            type_name='ValueError',
            module='builtins',
            message='',
            stacktrace=Stacktrace(
                frames=(
                    Frame(
                        filename='orders.py',
                        lineno=1,
                        function='charge',
                        locals={'password': 'hunter2', 'order_id': '42'},
                    ),
                )
            ),
        )

        cleaned = scrub(make_alert(exception=exc), Options())

        assert cleaned.exception is not None

        frame_locals = cleaned.exception.stacktrace.frames[0].locals

        assert frame_locals['password'] == REDACTED
        assert frame_locals['order_id'] == '42'

    def test_user_keeps_only_id_by_default(self) -> None:
        """
        `id` stays: it tells users apart without giving anything else away
        """

        alert = make_alert(user={'id': '42', 'email': 'ivan@example.com', 'ip': '10.0.0.1'})
        cleaned = scrub(alert, Options())

        assert cleaned.user == {'id': '42'}

    def test_user_is_kept_when_pii_enabled(self) -> None:
        """
        With PII enabled the whole user object is kept
        """

        alert = make_alert(user={'id': '42', 'email': 'ivan@example.com'})
        cleaned = scrub(alert, Options(send_default_pii=True))

        assert cleaned.user == {'id': '42', 'email': 'ivan@example.com'}

    def test_nested_structures_survive(self) -> None:
        """
        The shape survives: scrubbing must not turn a dict into a string
        """

        alert = make_alert(contexts={'order': {'items': [{'sku': 'A1', 'token': 'x'}], 'n': 3}})
        cleaned = scrub(alert, Options())
        order = cleaned.contexts['order']

        # Checking that the nesting is intact and only the token is hidden
        assert order['n'] == 3
        assert isinstance(order['items'], list)
        assert order['items'][0]['sku'] == 'A1'
        assert order['items'][0]['token'] == REDACTED

    def test_scrub_returns_new_alert(self) -> None:
        """
        Alert is immutable: scrubbing returns a copy, the original is left
        untouched
        """

        alert = make_alert(user={'id': '1', 'email': 'a@b.c'})
        scrub(alert, Options())

        assert alert.user == {'id': '1', 'email': 'a@b.c'}


__all__ = (
    'TestSecretRedaction',
    'TestScrubbing',
)
