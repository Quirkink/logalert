"""
Logalert Tests Split

Message splitting: the chunks must reassemble, fit the limit, and never be split
needlessly.

:copyright: (c) 2025 Aiko Sora
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from logalert.render import escape_html, split_blocks, utf16_len


@given(
    text=st.text(max_size=2000),
    limit=st.integers(min_value=1, max_value=200),
)
@settings(max_examples=500, deadline=None)
def test_chunks_reassemble_exactly(text: str, limit: int) -> None:
    """
    Chunks are contiguous slices, so joining them gives back the original
    """

    assert ''.join(split_blocks(text, limit=limit, measure=utf16_len)) == text


@given(
    text=st.text(min_size=1, max_size=2000),
    limit=st.integers(min_value=3, max_value=200),
)
@settings(max_examples=500, deadline=None)
def test_every_chunk_fits_but_always_progresses(text: str, limit: int) -> None:
    """
    A chunk fits within the limit, and if it cannot, it is a single character
    """

    for chunk in split_blocks(text, limit=limit, measure=utf16_len):
        if utf16_len(chunk) > limit:
            assert len(chunk) == 1, f'chunk {utf16_len(chunk)} > {limit}, length {len(chunk)}'


def test_no_needless_splitting() -> None:
    """
    Text that fits as a whole is not split
    """

    assert split_blocks('first\nsecond', limit=4096, measure=utf16_len) == ['first\nsecond']


def test_hard_split_of_a_single_long_line() -> None:
    """
    A line longer than the limit is split by force
    """

    line = 'x' * 500
    chunks = split_blocks(line, limit=50, measure=utf16_len)

    # Checking that the line was cut into pieces that still add up to the
    # original
    assert len(chunks) > 1
    assert ''.join(chunks) == line
    assert all(utf16_len(c) <= 50 for c in chunks)


def test_limit_below_one_is_rejected() -> None:
    """
    If the limit value is less than one, the operation is rejected.
    """

    with pytest.raises(ValueError):
        split_blocks('text', limit=0, measure=utf16_len)


def test_empty_text_yields_one_empty_chunk() -> None:
    """
    An empty text yields one empty fragment.
    """

    assert split_blocks('', limit=100, measure=utf16_len) == ['']


def test_utf16_counts_surrogate_pairs() -> None:
    """
    Telegram measures length in UTF-16, while Python's `len()` counts code
    points
    """

    # Checking the two ways of measuring the same text
    assert utf16_len('😀') == 2
    assert len('😀') == 1
    assert utf16_len('日本語です。') == 6


def test_escaped_length_is_what_gets_measured() -> None:
    """
    Escaping inflates the text, and the escaped form is what has to be measured
    """

    assert len(escape_html('&' * 100)) == 500
    assert escape_html('&lt;') == '&amp;lt;'
    assert escape_html('a < b > c') == 'a &lt; b &gt; c'


__all__ = (
    'test_every_chunk_fits_but_always_progresses',
    'test_escaped_length_is_what_gets_measured',
    'test_empty_text_yields_one_empty_chunk',
    'test_hard_split_of_a_single_long_line',
    'test_utf16_counts_surrogate_pairs',
    'test_limit_below_one_is_rejected',
    'test_chunks_reassemble_exactly',
    'test_no_needless_splitting',
)
