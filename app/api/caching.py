"""Conditional responses: a weak ETag over the JSON body, and 304 when it matches.

`Cache-Control: no-cache` means clients may store responses but must revalidate, which
costs one cheap request that answers 304 when nothing changed.
"""

import hashlib

from fastapi import Request, Response
from pydantic import BaseModel

CACHE_CONTROL = "no-cache"


def cached_json(request: Request, payload: BaseModel) -> Response:
    body = payload.model_dump_json().encode()
    etag = f'W/"{hashlib.sha256(body).hexdigest()[:32]}"'
    headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL}
    tags = _parse_if_none_match(request.headers.get("If-None-Match"))
    if "*" in tags or etag in tags:  # `*` matches any current representation (RFC 9110)
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def _parse_if_none_match(value: str | None) -> set[str]:
    if not value:
        return set()
    tags = {tag.strip() for tag in value.split(",")}
    # Weak comparison (RFC 9110 §13.1.2): W/"x" matches "x".
    return tags | {f"W/{tag}" for tag in tags if not tag.startswith("W/")}
