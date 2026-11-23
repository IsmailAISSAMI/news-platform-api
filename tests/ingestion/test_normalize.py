import pytest

from app.ingestion.normalize import ELLIPSIS, html_to_text, truncate


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("Plain   text\n here", "Plain text here"),
        ("<p>First</p><p>Second</p>", "First Second"),
        ("A<br>B", "A B"),
        ("Caf&eacute; &amp; bar", "Café & bar"),
        ("Rabat&amp;#8217;s port", "Rabat\u2019s port"),  # double-encoded &#8217;
        ("<script>alert(1)</script>Safe<style>p{}</style>", "Safe"),
        ("<p>Unclosed <b>bold", "Unclosed bold"),
        ("<![CDATA[x]]>Text", "Text"),
    ],
)
def test_html_to_text(raw: str | None, expected: str) -> None:
    assert html_to_text(raw) == expected


def test_truncate_keeps_short_values() -> None:
    assert truncate("Short text", 50) == "Short text"


def test_truncate_zero_means_store_nothing() -> None:
    assert truncate("Anything", 0) is None
    assert truncate("", 10) is None


def test_truncate_cuts_at_a_word_boundary_within_the_limit() -> None:
    result = truncate("The harbour expansion was approved on Monday", 20)

    assert result == "The harbour" + ELLIPSIS
    assert result is not None
    assert len(result) <= 20


@pytest.mark.parametrize("limit", [1, 2, 5, 13, 37, 100])
def test_truncate_never_exceeds_the_limit(limit: int) -> None:
    text = "word " * 50
    result = truncate(text.strip(), limit)

    assert result is not None
    assert len(result) <= limit


def test_truncate_rejects_negative_limits() -> None:
    with pytest.raises(ValueError, match="max_chars"):
        truncate("x", -1)
