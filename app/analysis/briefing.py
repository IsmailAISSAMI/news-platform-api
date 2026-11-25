"""The briefing: the part of an analysis the API may serve.

An analysis carries evidence quotes, which are article text, so the API never reads the
`article_analyses` table. At save time the pipeline also stores
`make_briefing(cleaned, unverified_paths, paragraphs=...)`, built in this order:

1. Quarantine. `unverified_paths` are JSON paths INTO `cleaned` (the pipeline verifies
   the cleaned analysis, the same object the briefing is made from, so indexes cannot
   shift between verification and removal). A path names its item by prefix:
   `$.who[2].evidence` (or `$.who[2]`) drops `who[2]`. For the event date, which is not a
   list item, the date value is set to null instead. A path that does not resolve is a
   bug and raises `ValueError`: it is never ignored, because ignoring it could serve the
   very item it was meant to remove.
2. Every `evidence` key is removed, at any depth.
3. Originality (owner rule: article text is never served, outputs must be original).
   Every remaining string is compared with the article: if it shares a run of
   `MIN_COPIED_RUN` or more consecutive tokens (after normalization) with any paragraph,
   the enclosing list item is dropped, or a scalar field outside any list is set to
   null. Proper-name fields (`NAME_FIELDS`) and strings shorter than the run are exempt.
   The paths found are listed in `copied_paths`.

Added keys: `review_required` (the caller's verdict), `removed_items` (items dropped or
emptied, by quarantine and by the originality filter), `copied_paths`,
`copied_items` (how many of those removals the originality filter made), and
`unverified_fields`: the served fields that have no evidence at all (headline, main
event, ...). They are model-written summaries that code cannot verify against the
article, so a reader must treat them as unverified.
"""

import copy
import re
from collections.abc import Iterator, Sequence
from typing import Any

from app.analysis.evidence import normalize

MIN_COPIED_RUN = 8  # consecutive tokens shared with the article that count as copied
NAME_FIELDS = frozenset({"name_as_written", "full_name", "attributed_to"})
# Fields the schema gives no evidence: served, but always marked unverified.
UNEVIDENCED_FIELDS = (
    "$.overview.headline",
    "$.what.main_event",
    "$.what.secondary_events",
    "$.how",
    "$.context",
    "$.open_questions",
    "$.topics",
    "$.tone.explanation",
)

_PATH_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")
_WORD = re.compile(r"\w+")

type _Token = tuple[int, str | int]  # (0, key) or (1, index): sortable without mixing types
type _Target = tuple[_Token, ...]


class UnresolvablePathError(ValueError):
    """An unverified path names nothing in the analysis: verification and briefing disagree."""


def make_briefing(
    cleaned: dict[str, Any],
    unverified_paths: list[str],
    *,
    paragraphs: Sequence[str] | None = None,
    review_required: bool | None = None,
) -> dict[str, Any]:
    """The servable briefing. Pure: `cleaned` is not modified.

    `paragraphs` (the article body) enables the originality filter; the pipeline always
    passes it. `review_required` defaults to "any unverified path".
    """
    briefing = copy.deepcopy(cleaned)
    targets = {target_of(path) for path in unverified_paths}
    if () in targets:
        msg = f"unverified path is not a JSON path into the analysis: {unverified_paths!r}"
        raise UnresolvablePathError(msg)
    removed = _remove_all(briefing, targets)
    result: dict[str, Any] = _strip_evidence(briefing)
    copied = copied_paths(result, paragraphs) if paragraphs else []
    copied_items = _remove_all(result, {_copied_target(target_of(p)) for p in copied})
    removed += copied_items
    result["review_required"] = (
        bool(unverified_paths) if review_required is None else review_required
    )
    result["removed_items"] = removed
    result["copied_items"] = copied_items
    result["copied_paths"] = copied
    result["unverified_fields"] = [p for p in UNEVIDENCED_FIELDS if _present(result, p)]
    return result


def target_of(path: str) -> _Target:
    """The item a path names: its tokens without a trailing `evidence`/`quote` part."""
    if not path.startswith("$"):
        return ()
    rest = path[1:]
    tokens: list[_Token] = []
    position = 0
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


def _remove_all(root: dict[str, Any], targets: set[_Target]) -> int:
    """Remove every target; an item inside another removed item is not counted twice."""
    targets = {t for t in targets if not any(t[: len(o)] == o and t != o for o in targets)}
    # Descending order: later indexes (and deeper items) go first, so indexes stay valid.
    for target in sorted(targets, reverse=True):
        _remove(root, target)
    return len(targets)


def _remove(root: dict[str, Any], target: _Target) -> None:
    node: Any = root
    for _, step in target[:-1]:
        try:
            node = node[step]
        except (KeyError, IndexError, TypeError):
            msg = f"path does not resolve: {_render(target)}"
            raise UnresolvablePathError(msg) from None
    _, last = target[-1]
    if isinstance(node, list) and isinstance(last, int) and last < len(node):
        del node[last]
        return
    if isinstance(node, dict) and isinstance(last, str) and last in node:
        item = node[last]
        if isinstance(item, dict) and "value" in item:  # the event date: drop the date only
            item["value"] = None
        elif isinstance(item, dict):
            del node[last]
        else:  # a scalar field (originality filter): keep the key, drop the text
            node[last] = None
        return
    msg = f"path does not resolve: {_render(target)}"
    raise UnresolvablePathError(msg)


def _render(target: _Target) -> str:
    return "$" + "".join(f"[{s}]" if kind else f".{s}" for kind, s in target)


def _strip_evidence(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_evidence(v) for k, v in node.items() if k != "evidence"}
    if isinstance(node, list):
        return [_strip_evidence(item) for item in node]
    return node


# --- Originality ------------------------------------------------------------------------


def tokens(text: str) -> list[str]:
    return _WORD.findall(normalize(text))


def _runs(words: list[str]) -> Iterator[tuple[str, ...]]:
    for start in range(len(words) - MIN_COPIED_RUN + 1):
        yield tuple(words[start : start + MIN_COPIED_RUN])


def copied_paths(briefing: dict[str, Any], paragraphs: Sequence[str]) -> list[str]:
    """Paths of served strings sharing MIN_COPIED_RUN consecutive tokens with the article."""
    article: set[tuple[str, ...]] = set()
    for paragraph in paragraphs:
        article.update(_runs(tokens(paragraph)))
    found = []
    for path, key, value in _strings(briefing, "$", None):
        if key in NAME_FIELDS:
            continue
        if any(run in article for run in _runs(tokens(value))):
            found.append(path)
    return found


def _strings(node: Any, path: str, key: str | None) -> Iterator[tuple[str, str | None, str]]:
    if isinstance(node, str):
        yield path, key, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, f"{path}.{k}", k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]", key)


def _copied_target(target: _Target) -> _Target:
    """The enclosing list item of a copied string, or the string's own field if none."""
    for cut in range(len(target), 0, -1):
        if target[cut - 1][0] == 1:
            return target[:cut]
    return target


def _present(briefing: dict[str, Any], path: str) -> bool:
    node: Any = briefing
    for _, step in target_of(path):
        if not isinstance(node, dict) or step not in node:
            return False
        node = node[step]
    return node not in (None, "", [])
