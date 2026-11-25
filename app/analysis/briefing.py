"""The briefing: the part of an analysis the API may serve.

An analysis carries evidence quotes, which are article text, so the API never reads the
`article_analyses` table. At save time the pipeline also stores `make_briefing(cleaned,
unverified_paths)`: the cleaned analysis with every `evidence` key removed (at any depth)
and every item whose evidence could not be verified dropped. Only model-written fields
remain; they are not article text. The API reads it through the `analysis_briefings` view.

Unverified paths are JSON paths such as `$.who[2].evidence` or `$.when.event_date.evidence`.
A path names its item by prefix: `$.who[2].evidence` (or `$.who[2]` itself) drops `who[2]`.
For the event date, which is not a list item, the date value is set to null instead: a
date is kept only when its evidence verified.
"""

import copy
import re
from typing import Any

_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")

type _Token = tuple[int, str | int]  # (0, key) or (1, index): sortable without mixing types


def make_briefing(cleaned: dict[str, Any], unverified_paths: list[str]) -> dict[str, Any]:
    """The servable briefing: no evidence anywhere, no unverified item.

    Adds `review_required` (any unverified path) and `removed_items` (how many items were
    dropped or, for the event date, emptied). Pure: `cleaned` is not modified.
    """
    briefing = copy.deepcopy(cleaned)
    targets = {_target(path) for path in unverified_paths}
    targets.discard(())
    # An item inside an item that is removed anyway is not counted twice.
    targets = {t for t in targets if not any(t[: len(o)] == o and t != o for o in targets)}
    removed = 0
    # Descending order: later indexes (and deeper items) go first, so indexes stay valid.
    for target in sorted(targets, reverse=True):
        if _remove(briefing, target):
            removed += 1
    result: dict[str, Any] = _strip_evidence(briefing)
    result["review_required"] = bool(unverified_paths)
    result["removed_items"] = removed
    return result


def _target(path: str) -> tuple[_Token, ...]:
    """The item a path names: its tokens without a trailing `evidence`/`quote` part."""
    if not path.startswith("$"):
        return ()
    tokens: list[_Token] = []
    for key, index in _TOKEN.findall(path[1:]):
        tokens.append((1, int(index)) if index else (0, key))
    while tokens and tokens[-1] in ((0, "evidence"), (0, "quote"), (0, "paragraph")):
        tokens.pop()
    return tuple(tokens)


def _remove(root: dict[str, Any], target: tuple[_Token, ...]) -> bool:
    node: Any = root
    for _, step in target[:-1]:
        try:
            node = node[step]
        except (KeyError, IndexError, TypeError):
            return False
    _, last = target[-1]
    if isinstance(node, list) and isinstance(last, int) and last < len(node):
        del node[last]
        return True
    if isinstance(node, dict) and isinstance(last, str) and isinstance(node.get(last), dict):
        item = node[last]
        if "value" in item:  # the event date: keep the field, drop the unverified date
            item["value"] = None
            return True
        del node[last]
        return True
    return False


def _strip_evidence(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_evidence(v) for k, v in node.items() if k != "evidence"}
    if isinstance(node, list):
        return [_strip_evidence(item) for item in node]
    return node
