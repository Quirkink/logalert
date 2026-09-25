"""
Logalert Render

Turning an :class:`Alert` into message text

:copyright: (c) 2025 Aiko Sora
"""

import re
from collections.abc import Callable, Iterable

from .options import Options
from .types import Alert, Breadcrumb

TELEGRAM_LIMIT = 4096
MATRIX_LIMIT = 16_000
WEBHOOK_TEXT_LIMIT = 1900

WRAP_OPEN = '<pre><code class="language-python">'
WRAP_CLOSE = '</code></pre>'

_LEVEL_CODES = {
    'debug': 'DBG',
    'info': 'INF',
    'warning': 'WRN',
    'error': 'ERR',
    'fatal': 'FTL',
}

_SEVERITY = {
    'debug': '⚪',
    'info': '🔵',
    'warning': '🟠',
    'error': '🔴',
    'fatal': '💀',
}

Measure = Callable[[str], int]

_TAG_RE = re.compile(r'</?(?:b|i|u|s|code|pre|blockquote)(?:\s[^>]*)?>')


def utf16_len(text: str) -> int:
    """
    String length in UTF-16 units - which is how Telegram counts it

    :param text: The text to measure

    :return int:
    """

    return len(text.encode('utf-16-le')) // 2


def escape_html(text: str) -> str:
    """
    Escape the three characters that are significant in HTML

    :param text: The text to escape

    :return str:
    """

    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def split_blocks(
    text: str,
    *,
    limit: int,
    measure: Measure = utf16_len,
) -> list[str]:
    """
    Cut the text into chunks that fit within ``limit``

    :param text: The text to cut into chunks
    :param limit: Largest chunk size, in ``measure`` units
    :param measure: Function measuring a chunk; UTF-16 code units by default

    :return list[str]:
    """

    if limit <= 0:
        raise ValueError(f'limit must be > 0, got {limit!r}')

    if not text:
        return ['']

    chunks: list[str] = []
    position = 0
    total = len(text)

    while position < total:
        end = _longest_fitting_prefix(text, position, limit, measure)

        if end <= position:
            end = position + 1

        if end < total:
            newline = text.rfind('\n', position, end)

            if newline > position:
                end = newline + 1

        chunks.append(text[position:end])
        position = end

    return chunks


def _longest_fitting_prefix(text: str, start: int, limit: int, measure: Measure) -> int:
    """
    End index of the longest prefix of ``text[start:]`` that fits within the
    limit

    :param text: The text being searched
    :param start: Index the prefix starts at
    :param limit: Largest prefix size, in ``measure`` units
    :param measure: Function measuring a slice of the text

    :return int:
    """

    low, high = start, len(text)

    best = start

    while low <= high:
        mid = (low + high) // 2

        if measure(text[start:mid]) <= limit:
            best = mid
            low = mid + 1

        else:
            high = mid - 1

    return best


def _header(alert: Alert, options: Options) -> str:
    """
    Build the opening lines of a message

    :param alert: The event being rendered
    :param options: Resolved options

    :return str:
    """

    severity = _SEVERITY.get(alert.level, '🔴')

    if alert.exception is not None:
        title = f'<b>{escape_html(alert.exception.type_name)}</b>'
        suffix = '' if alert.handled else ' - <i>unhandled</i>'

    else:
        title = f'<b>{escape_html(alert.level)}</b>'
        suffix = ''

    lines = [
        f'{severity} {title}{suffix} · <code>{escape_html(alert.logger)}</code>',
    ]

    facts = [alert.environment]

    if alert.release:
        facts.append(alert.release)

    if alert.server_name:
        facts.append(alert.server_name)

    facts.append(alert.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC'))
    lines.append(f'<i>{escape_html(" · ".join(facts))}</i>')

    if alert.tags:
        tags = ' '.join(f'{k}={v}' for k, v in sorted(alert.tags.items()))
        lines.append(f'<i>{escape_html(tags)}</i>')

    if message_line := _message_line(alert):
        lines.append(message_line)

    return '\n'.join(lines)


def _message_line(alert: Alert) -> str:
    """
    The message text - only when it does not duplicate the exception text

    :param alert: The event being rendered

    :return str:
    """

    message = alert.message.strip()

    if not message:
        return ''

    if alert.exception is not None and message == alert.exception.message.strip():
        return ''

    return f'<blockquote>{escape_html(message)}</blockquote>'


def _traceback_text(alert: Alert, options: Options) -> str:
    """
    Assemble the traceback together with its cause chain into a single text
    block

    :param alert: The event being rendered
    :param options: Options controlling frame count and source context

    :return str:
    """

    if alert.exception is None:
        return ''

    return '\n'.join(
        f'{label}{_single_traceback(exc, options)}'
        for label, exc in _walk_exceptions(alert.exception)
    )


def _walk_exceptions(exc: object) -> Iterable[tuple[str, object]]:
    """
    Walk the exception and its cause chain, marking how each link was reached

    :param exc: The exception to walk

    :return Iterable[tuple[str, object]]:
    """

    yield '', exc

    seen = 0
    current = exc

    while seen < 5:
        cause = getattr(current, 'cause', None)
        context = getattr(current, 'context', None)

        if cause is not None:
            yield '\nCause: ', cause
            current = cause

        elif context is not None:
            yield '\nContext: ', context
            current = context

        else:
            return

        seen += 1


def _single_traceback(exc: object, options: Options) -> str:
    """
    Render a single exception as a traceback block

    :param exc: The exception to render
    :param options: Options controlling frame count, source context and locals

    :return str:
    """

    lines: list[str] = ['Traceback (most recent call last):']
    stacktrace = getattr(exc, 'stacktrace', None)
    frames = getattr(stacktrace, 'frames', ()) if stacktrace else ()
    frames = list(frames)[-options.max_frames :]

    for frame in frames:
        lines.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.function}')

        if options.include_source_context and frame.source_line:
            lines.append(f'    {frame.source_line}')

        if frame.locals:
            items = ', '.join(f'{k}={v}' for k, v in sorted(frame.locals.items()))
            lines.append(f'    locals: {items}')

    message = getattr(exc, 'message', '')
    name = getattr(exc, 'qualified_name', 'Exception')
    lines.append(f'{name}: {message}' if message else str(name))

    return '\n'.join(lines)


def _footer(alert: Alert, options: Options) -> str:
    """
    The lines that follow the traceback: repetition, dropped counts and
    breadcrumbs

    :param alert: The event being rendered
    :param options: Options carrying the dedup window

    :return str:
    """

    lines: list[str] = []

    if alert.count > 1:
        lines.append(f'🔁 <b>Repeated</b> {alert.count} times (window {options.dedup_window:g}s)')

    if alert.dropped:
        total = sum(alert.dropped.values())
        reasons = ', '.join(f'{k}={v}' for k, v in sorted(alert.dropped.items()))
        lines.append(f'⚠️ <b>Dropped</b> {total} ({escape_html(reasons)})')

    crumbs = alert.breadcrumbs[-20:]

    if crumb_lines := _render_breadcrumbs(crumbs):
        lines.append('<b>Latest breadcrumbs:</b>')
        lines.append(f'<pre>{escape_html(chr(10).join(crumb_lines))}</pre>')

    return '\n'.join(lines)


def _render_breadcrumbs(crumbs: Iterable[Breadcrumb]) -> list[str]:
    """
    One line per breadcrumb, with the message cut to 120 characters

    :param crumbs: The breadcrumbs to render

    :return list[str]:
    """

    result: list[str] = []

    for crumb in crumbs:
        stamp = crumb.timestamp.strftime('%H:%M:%S')
        text = crumb.message

        if len(text) > 120:
            text = text[:117] + '...'

        result.append(f'{stamp} {_LEVEL_CODES[crumb.level]} {crumb.category}: {text}')

    return result


def _compose(
    header: str,
    body: str,
    footer: str,
    *,
    limit: int,
    wrap: bool,
    separator: str = '\n',
) -> list[str]:
    """
    Assemble messages from a header, a body and a footer, staying within the
    limit

    :param header: The message header
    :param body: The traceback text, cut into chunks
    :param footer: The message footer
    :param limit: Largest message size, in UTF-16 code units
    :param wrap: Whether the chunks are wrapped in ``<pre><code>``
    :param separator: String joining the header, the body and the footer

    :return list[str]:
    """

    marker_cost = 16
    wrapper_cost = (utf16_len(WRAP_OPEN) + utf16_len(WRAP_CLOSE)) if wrap else 0
    head_cost = utf16_len(header) + (utf16_len(separator) if header else 0)
    first_budget = limit - head_cost - wrapper_cost - marker_cost
    rest_budget = limit - wrapper_cost - marker_cost

    if not body:
        text = separator.join(part for part in (header, footer) if part)

        return split_blocks(text, limit=limit, measure=utf16_len) if text else ['']

    if first_budget <= 0:
        pieces = split_blocks(body, limit=max(rest_budget, 1), measure=utf16_len)
        messages = [header] if header else []
        messages.extend(_wrap_all(pieces, wrap))

        if footer:
            messages.append(footer)

        return messages

    first_pieces = split_blocks(body, limit=first_budget, measure=utf16_len)

    if len(first_pieces) == 1:
        body_text = _wrap(first_pieces[0], wrap)
        parts = [part for part in (header, body_text, footer) if part]
        text = separator.join(parts)

        if utf16_len(text) <= limit:
            return [text]

        if footer and utf16_len(separator.join([header, body_text, footer])) > limit:
            return [separator.join(p for p in (header, body_text) if p), footer]

    head_message = separator.join(p for p in (header, _wrap(first_pieces[0], wrap)) if p)
    tail_pieces: list[str] = []
    remaining = ''.join(first_pieces[1:])

    if remaining:
        tail_pieces = split_blocks(remaining, limit=rest_budget, measure=utf16_len)

    messages = [head_message]
    total = len(tail_pieces) + 1

    for index, piece in enumerate(tail_pieces, start=2):
        marker = f'[{index}/{total}] (continued)\n'
        messages.append(marker + _wrap(piece, wrap))

    if footer:
        if utf16_len(messages[-1] + separator + footer) <= limit:
            messages[-1] = messages[-1] + separator + footer

        else:
            messages.append(footer)

    return messages


def _wrap(text: str, wrap: bool) -> str:
    """
    Put the code markup around the text, when wrapping is on

    :param text: The text to wrap
    :param wrap: Whether the text is wrapped at all

    :return str:
    """

    if not wrap:
        return text

    return f'{WRAP_OPEN}{escape_html(text)}{WRAP_CLOSE}'


def _wrap_all(pieces: list[str], wrap: bool) -> list[str]:
    """
    Wrap every chunk in the list

    :param pieces: The chunks to wrap
    :param wrap: Whether the chunks are wrapped at all

    :return list[str]:
    """

    return [_wrap(piece, wrap) for piece in pieces]


def render_telegram(alert: Alert, options: Options) -> list[str]:
    """
    Messages for ``sendMessage`` with ``parse_mode=HTML``

    :param alert: The event to render
    :param options: Options controlling frames, breadcrumbs and the footer

    :return list[str]:
    """

    if options.include_breadcrumbs is False:
        alert = _without_breadcrumbs(alert)

    return _compose(
        _header(alert, options),
        _traceback_text(alert, options),
        _footer(alert, options),
        limit=TELEGRAM_LIMIT,
        wrap=True,
    )


def render_matrix(alert: Alert, options: Options) -> list[str]:
    """
    HTML for ``formatted_body`` in Matrix

    :param alert: The event to render
    :param options: Options controlling frames, breadcrumbs and the footer

    :return list[str]:
    """

    return _compose(
        _header(alert, options),
        _traceback_text(alert, options),
        _footer(alert, options),
        limit=MATRIX_LIMIT,
        wrap=True,
    )


def render_text(alert: Alert, options: Options) -> list[str]:
    """
    Plain text - for stderr, ``--dry-run`` and debug output

    :param alert: The event to render
    :param options: Options controlling frames, breadcrumbs and the footer

    :return list[str]:
    """

    return _compose(
        _plain(_header(alert, options)),
        _traceback_text(alert, options),
        _plain(_footer(alert, options)),
        limit=TELEGRAM_LIMIT,
        wrap=False,
    )


def _plain(html: str) -> str:
    """
    Strip the markup and put the original characters back

    :param html: The rendered HTML

    :return str:
    """

    return _TAG_RE.sub('', html).replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')


def _without_breadcrumbs(alert: Alert) -> Alert:
    """
    A copy of the event with the breadcrumbs dropped

    :param alert: The event to copy

    :return Alert:
    """

    from dataclasses import replace

    return replace(alert, breadcrumbs=())


__all__ = (
    'render_telegram',
    'TELEGRAM_LIMIT',
    'render_matrix',
    'MATRIX_LIMIT',
    'split_blocks',
    'escape_html',
    'render_text',
    'utf16_len',
)
