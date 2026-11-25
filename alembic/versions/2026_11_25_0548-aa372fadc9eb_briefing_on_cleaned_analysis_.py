"""briefing on the cleaned analysis, originality filter, attempt history

1. Adds `article_analyses.attempt_history` (JSONB list, default []): one entry per
   attempt, appended by the pipeline. Existing rows start empty (earlier attempts were
   overwritten and cannot be recovered).
2. Recomputes, for every answered analysis, the evidence statuses (raw answer and
   cleaned analysis), `unverified_paths`, `status` and `briefing` with the rules as of
   this revision: whole-word quote match, minimum quote length, quarantine applied to
   the cleaned analysis (the object the briefing is made from), the originality filter
   against the article body, and the `unverified_fields`, `copied_items` and `copied_paths` keys.

The transform is a FROZEN copy below: a migration must not import app code, which keeps
changing. Article bodies are read from `article_bodies` for the originality check and
the quote checks; they are never logged or written anywhere. An answered analysis whose
article has no body any more cannot be checked: its briefing is set to NULL (not served).

Revision ID: aa372fadc9eb
Revises: 7c3e9a1f5b20
Create Date: 2026-11-25 05:48:32.802036

"""

import copy
import json
import re
import unicodedata
from collections.abc import Iterator, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aa372fadc9eb"
down_revision: str | Sequence[str] | None = "7c3e9a1f5b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# --- Frozen copy of app.analysis.evidence / briefing, as of this revision ------------------

_QUOTES = str.maketrans(
    {
        "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u2013": "-",
        "\u2014": "-", "\u00a0": " ", "\u2010": "-", "\u2012": "-", "\u2015": "-",
        "\u2212": "-", "\u00ad": None, "\u200b": None, "\u200c": None, "\u200d": None,
        "\u2060": None, "\ufeff": None,
    }
)  # fmt: skip
_DROP_QUOTES = str.maketrans("", "", "\"'`")
_SPACE = re.compile(r"\s+")
_EDGE_PUNCT = " .,;:'\"\u2026"
UNVERIFIED = {"fabricated", "misplaced", "bad_paragraph", "too_short"}
MIN_QUOTE_TOKENS = 4
MIN_QUOTE_CHARS = 20
EVENT_DATE_EVIDENCE = "$.when.event_date.evidence"
MIN_COPIED_RUN = 8
NAME_FIELDS = {"name_as_written", "full_name", "attributed_to"}
UNEVIDENCED_FIELDS = (
    "$.overview.headline", "$.what.main_event", "$.what.secondary_events", "$.how",
    "$.context", "$.open_questions", "$.topics", "$.tone.explanation",
)  # fmt: skip
_PATH_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")
_WORD = re.compile(r"\w+")
_EVIDENCE_KEYS = {"paragraph", "quote"}

Target = tuple[tuple[int, Any], ...]


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES).translate(_DROP_QUOTES)
    text = text.replace("...", "\u2026")
    return _SPACE.sub(" ", text).strip().casefold()


def _contains(paragraph: str, quote: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(quote)}(?!\w)", paragraph) is not None


def _check(evidence: dict[str, Any], normalized: list[str], *, allow_short: bool) -> str:
    quote = _normalize(str(evidence.get("quote") or "")).strip(_EDGE_PUNCT)
    if not quote:
        return "fabricated"
    short = len(quote.split()) < MIN_QUOTE_TOKENS and len(quote) < MIN_QUOTE_CHARS
    if short and not allow_short:
        return "too_short"
    number = evidence.get("paragraph")
    if not isinstance(number, int) or not 1 <= number <= len(normalized):
        found = any(_contains(p, quote) for p in normalized)
        return "misplaced" if found else "bad_paragraph"
    if _contains(normalized[number - 1], quote):
        return "verified"
    return "misplaced" if any(_contains(p, quote) for p in normalized) else "fabricated"


def _evidence(node: Any, path: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every evidence object of a stored analysis (dict form), with its JSON path."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}"
            if key == "evidence" and isinstance(value, dict) and set(value) >= _EVIDENCE_KEYS:
                yield child, value
            else:
                yield from _evidence(value, child)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _evidence(item, f"{path}[{index}]")


def _verify(analysis: dict[str, Any], normalized: list[str]) -> dict[str, str]:
    return {
        path: _check(ev, normalized, allow_short=path == EVENT_DATE_EVIDENCE)
        for path, ev in _evidence(analysis, "$")
    }


def _target(path: str) -> Target:
    if not path.startswith("$"):
        return ()
    rest, tokens, position = path[1:], [], 0
    for match in _PATH_TOKEN.finditer(rest):
        if match.start() != position:
            return ()
        position = match.end()
        key, index = match.groups()
        tokens.append((1, int(index)) if index is not None else (0, key))
    if position != len(rest):
        return ()
    while tokens and tokens[-1] in ((0, "evidence"), (0, "quote"), (0, "paragraph")):
        tokens.pop()
    return tuple(tokens)


def _remove(root: dict[str, Any], target: Target) -> None:
    node: Any = root
    for _, step in target[:-1]:
        node = node[step]  # raises on an unresolvable path: never ignored
    _, last = target[-1]
    if isinstance(node, list) and isinstance(last, int) and last < len(node):
        del node[last]
    elif isinstance(node, dict) and isinstance(last, str) and last in node:
        item = node[last]
        if isinstance(item, dict) and "value" in item:
            item["value"] = None
        elif isinstance(item, dict):
            del node[last]
        else:
            node[last] = None
    else:
        msg = f"path does not resolve: {target!r}"
        raise ValueError(msg)


def _remove_all(root: dict[str, Any], targets: set[Target]) -> int:
    if () in targets:
        msg = "malformed path"
        raise ValueError(msg)
    targets = {t for t in targets if not any(t[: len(o)] == o and t != o for o in targets)}
    for target in sorted(targets, reverse=True):
        _remove(root, target)
    return len(targets)


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k != "evidence"}
    if isinstance(node, list):
        return [_strip(item) for item in node]
    return node


def _runs(words: list[str]) -> Iterator[tuple[str, ...]]:
    for start in range(len(words) - MIN_COPIED_RUN + 1):
        yield tuple(words[start : start + MIN_COPIED_RUN])


def _strings(node: Any, path: str, key: str | None) -> Iterator[tuple[str, str | None, str]]:
    if isinstance(node, str):
        yield path, key, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, f"{path}.{k}", k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]", key)


def _copied_target(target: Target) -> Target:
    for cut in range(len(target), 0, -1):
        if target[cut - 1][0] == 1:
            return target[:cut]
    return target


def _present(briefing: dict[str, Any], path: str) -> bool:
    node: Any = briefing
    for _, step in _target(path):
        if not isinstance(node, dict) or step not in node:
            return False
        node = node[step]
    return node not in (None, "", [])


def _briefing(
    cleaned: dict[str, Any], quarantined: list[str], paragraphs: list[str], review: bool
) -> dict[str, Any]:
    data = copy.deepcopy(cleaned)
    removed = _remove_all(data, {_target(p) for p in quarantined})
    result: dict[str, Any] = _strip(data)
    article: set[tuple[str, ...]] = set()
    for paragraph in paragraphs:
        article.update(_runs(_WORD.findall(_normalize(paragraph))))
    copied = [
        path
        for path, key, value in _strings(result, "$", None)
        if key not in NAME_FIELDS
        and any(run in article for run in _runs(_WORD.findall(_normalize(value))))
    ]
    copied_items = _remove_all(result, {_copied_target(_target(p)) for p in copied})
    removed += copied_items
    result["review_required"] = review
    result["removed_items"] = removed
    result["copied_items"] = copied_items
    result["copied_paths"] = copied
    result["unverified_fields"] = [p for p in UNEVIDENCED_FIELDS if _present(result, p)]
    return result


# --- Migration ----------------------------------------------------------------------------


def upgrade() -> None:
    """Add attempt_history; recompute evidence, status and briefing of answered analyses."""
    op.add_column(
        "article_analyses",
        sa.Column(
            "attempt_history",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT a.id, a.analysis, a.cleaned, a.evidence, b.body_text "
            "FROM article_analyses a LEFT JOIN article_bodies b ON b.article_id = a.article_id "
            "WHERE a.status IN ('succeeded', 'needs_review') "
            "AND a.analysis IS NOT NULL AND a.cleaned IS NOT NULL"
        )
    ).all()
    for row_id, analysis, cleaned, stored_evidence, body in rows:
        if body is None:
            connection.execute(
                sa.text("UPDATE article_analyses SET briefing = NULL WHERE id = :id"),
                {"id": row_id},
            )
            continue
        # Same split as app.analysis.prompt.prepare, as of this revision.
        paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
        normalized = [_normalize(p) for p in paragraphs]
        raw = _verify(analysis, normalized)
        clean = _verify(cleaned, normalized)
        unverified = sorted(p for p, s in raw.items() if s in UNVERIFIED)
        quarantined = sorted(p for p, s in clean.items() if s in UNVERIFIED)
        status = "needs_review" if unverified else "succeeded"
        issues = {k: v for k, v in (stored_evidence or {}).items() if k.startswith("issue:")}
        evidence = {**raw, **issues, **{f"cleaned:{k}": v for k, v in clean.items()}}
        briefing = _briefing(cleaned, quarantined, paragraphs, status == "needs_review")
        connection.execute(
            sa.text(
                "UPDATE article_analyses SET status = :status, "
                "evidence = CAST(:evidence AS jsonb), "
                "unverified_paths = CAST(:paths AS jsonb), "
                "briefing = CAST(:briefing AS jsonb), updated_at = now() WHERE id = :id"
            ),
            {
                "id": row_id,
                "status": status,
                "evidence": json.dumps(evidence),
                "paths": json.dumps(unverified),
                "briefing": json.dumps(briefing),
            },
        )


def downgrade() -> None:
    """Drops attempt_history; the recomputed data is kept (no-op for the data).

    The recomputed statuses and briefings are the correct state: restoring the old
    briefings would serve items whose quotes are unverified or copied from the article.
    """
    op.drop_column("article_analyses", "attempt_history")
