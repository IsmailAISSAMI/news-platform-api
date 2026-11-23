"""Text normalization for publisher-supplied fields. Pure functions, no I/O.

Publisher HTML is never rendered or stored as HTML: it is reduced to plain text here.
"""

import re
from html import unescape
from html.parser import HTMLParser

_WHITESPACE = re.compile(r"\s+")
_SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template"})
_BLOCK_TAGS = frozenset(
    {"p", "br", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr"}
)
ELLIPSIS = "…"
# Marks a block boundary (<p>, <li>, ...). Raw newlines in HTML source are just whitespace.
_BREAK = "\x1e"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(_BREAK)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(_BREAK)

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def collapse_whitespace(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def html_to_text(value: str | None) -> str:
    """Plain text from an HTML fragment: tags dropped, entities decoded, spaces collapsed."""
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    # Feeds often double-encode entities (e.g. "&amp;#8217;"), so decode once more.
    return collapse_whitespace(unescape("".join(parser.parts)).replace(_BREAK, " "))


def html_to_paragraphs(value: str | None) -> str:
    """Plain text that keeps the paragraph structure (blank line between blocks).

    For article bodies: an AI reading the text benefits from knowing where paragraphs,
    list items and headings start. Tags, scripts and styles are removed as in
    `html_to_text`.
    """
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    blocks = (collapse_whitespace(block) for block in unescape("".join(parser.parts)).split(_BREAK))
    return "\n\n".join(block for block in blocks if block)


def truncate(value: str, max_chars: int) -> str | None:
    """Cut at a word boundary so the result, ellipsis included, is at most `max_chars`.

    Returns None for an empty value or `max_chars == 0` (store nothing).
    """
    if max_chars < 0:
        msg = "max_chars must be >= 0"
        raise ValueError(msg)
    if max_chars == 0 or not value:
        return None
    if len(value) <= max_chars:
        return value
    if max_chars == 1:
        return ELLIPSIS
    cut = value[: max_chars - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    # Trailing punctuation, including en (U+2013) and em (U+2014) dashes.
    return cut.rstrip(" ,;:.-\u2013\u2014") + ELLIPSIS
