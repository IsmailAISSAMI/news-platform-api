"""Read-only briefing endpoints.

Everything comes from the `analysis_briefings` view (succeeded and needs_review analyses
only, `briefing` column only): the API role has no right on `article_analyses`, whose
evidence quotes and raw answers are article text. When an article has several analyzer
versions, the most recently updated briefing is served.

The list uses the same keyset pagination as the catalog: (published_at DESC, id DESC).
The query walks articles in that order (the `ix_articles_published_at_id` index) and joins
each one LATERALly to its latest briefing. An extra `id IN (briefed articles)` semi-join
lets the planner skip unbriefed articles with a cheap index-only probe of the briefings
partial index. Measured on 20k synthetic articles (PA-004): 1 % briefed went from 8,083
buffers / 47 ms to 2,205 / 2 ms per page; 100 % briefed costs 112 buffers / 0.3 ms.

A stored briefing that no longer matches the response schema is skipped (list) or
answered 404 (detail), and logged by article id only, never by content.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    MetaData,
    Select,
    Table,
    Text,
    select,
    true,
    tuple_,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.api.caching import cached_json
from app.api.deps import DbSession
from app.api.v1.catalog import MAX_LIMIT, ArticleId, Cursor, _decode_cursor, _encode_cursor
from app.core.errors import NOT_FOUND
from app.models import Article, Source
from app.schemas.analysis import (
    BriefingActor,
    BriefingDetail,
    BriefingOut,
    BriefingPlace,
    BriefingSource,
    BriefingSummary,
)
from app.schemas.catalog import Page

logger = logging.getLogger(__name__)

router = APIRouter()

# The view, described for queries only: its own MetaData keeps it out of Alembic's models.
analysis_briefings = Table(
    "analysis_briefings",
    MetaData(),
    Column("article_id", BigInteger),
    Column("analyzer_version", Text),
    Column("status", Text),
    Column("briefing", JSONB),
    Column("updated_at", DateTime(timezone=True)),
)

_INVOLVEMENT = {"central": 0, "participant": 1, "quoted": 2, "mentioned": 3}
_PLACE_ROLE = {"event_location": 0, "actor_location": 1, "related_event": 2, "mentioned": 3}
TOP = 3


@router.get("/briefings", response_model=Page[BriefingSummary], tags=["Briefings"])
async def list_briefings(
    request: Request,
    db: DbSession,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 20,
) -> Response:
    stmt = _briefing_query()
    if cursor:
        after_published, after_id = _decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(Article.published_at, Article.id) < tuple_(after_published, after_id)
        )
    rows = (await db.execute(stmt.limit(limit + 1))).all()
    page = rows[:limit]
    items = [
        item
        for row in page
        if (item := _summary(row.Article, row.Source, row.briefing)) is not None
    ]
    last = page[-1].Article if page else None
    next_cursor = _encode_cursor(last.published_at, last.id) if len(rows) > limit and last else None
    return cached_json(request, Page[BriefingSummary](items=items, next_cursor=next_cursor))


@router.get(
    "/articles/{article_id}/briefing",
    response_model=BriefingDetail,
    tags=["Briefings"],
    responses=NOT_FOUND,
)
async def get_briefing(request: Request, db: DbSession, article_id: ArticleId) -> Response:
    row = (await db.execute(_briefing_query().where(Article.id == article_id))).first()
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Briefing not found")
    if row is None:
        raise not_found
    article: Article = row.Article
    source: Source = row.Source
    data = row.briefing
    briefing = _parse(article.id, data)
    if briefing is None:
        raise not_found
    detail = BriefingDetail(
        article_id=article.id,
        title=article.title,
        source_url=article.canonical_url,
        published_at=article.published_at,
        source=_source(source),
        analyzer_version=row.analyzer_version,
        status=row.status,
        review_required=data.get("review_required") is True,
        removed_items=_count(data.get("removed_items")),
        copied_items=_count(data.get("copied_items")),
        unverified_fields=_strings(data.get("unverified_fields")),
        updated_at=row.updated_at,
        briefing=briefing,
    )
    return cached_json(request, detail)


# --- helpers ---------------------------------------------------------------------------


def _briefing_query() -> Select[Any]:
    view = analysis_briefings.c
    latest = (
        select(view.analyzer_version, view.status, view.briefing, view.updated_at)
        .where(view.article_id == Article.id)
        .order_by(view.updated_at.desc())
        .limit(1)
        .lateral("latest")
    )
    return (
        select(
            Article,
            Source,
            latest.c.analyzer_version,
            latest.c.status,
            latest.c.briefing,
            latest.c.updated_at,
        )
        .select_from(Article)
        .join(latest, true())
        .join(Source, Article.source_id == Source.id)
        # Redundant with the inner LATERAL join, but visible to the planner as a semi-join:
        # unbriefed articles are rejected by an index-only probe of the briefings partial
        # index instead of a full LATERAL subquery each (PA-004).
        .where(Article.id.in_(select(view.article_id)))
        .order_by(Article.published_at.desc(), Article.id.desc())
    )


def _parse(article_id: int, data: Any) -> BriefingOut | None:
    try:
        return BriefingOut.model_validate(data)
    except ValidationError as exc:
        # The error text can echo stored values: log the id and error count only.
        logger.warning(
            "Skipping malformed stored briefing",
            extra={"article_id": article_id, "error_count": exc.error_count()},
        )
        return None


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _source(source: Source) -> BriefingSource:
    return BriefingSource(slug=source.slug, name=source.name, site_url=source.site_url)


def _summary(article: Article, source: Source, data: Any) -> BriefingSummary | None:
    briefing = _parse(article.id, data)
    if briefing is None:
        return None
    actors = sorted(briefing.who, key=lambda a: _INVOLVEMENT.get(a.involvement, 9))[:TOP]
    places = sorted(briefing.where, key=lambda p: _PLACE_ROLE.get(p.role, 9))[:TOP]
    return BriefingSummary(
        article_id=article.id,
        title=article.title,
        canonical_url=article.canonical_url,
        published_at=article.published_at,
        source=_source(source),
        headline=briefing.overview.headline,
        report_type=briefing.overview.report_type,
        event_status=briefing.overview.event_status,
        event_date=briefing.when.event_date.value,
        places=[BriefingPlace(name_as_written=p.name_as_written, role=p.role) for p in places],
        actors=[
            BriefingActor(
                full_name=a.full_name, type=a.type, role=a.role, involvement=a.involvement
            )
            for a in actors
        ],
        review_required=data.get("review_required") is True,
    )
