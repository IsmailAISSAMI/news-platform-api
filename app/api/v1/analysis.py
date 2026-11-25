"""Read-only briefing endpoints.

Everything comes from the `analysis_briefings` view (succeeded and needs_review analyses
only, `briefing` column only): the API role has no right on `article_analyses`, whose
evidence quotes and raw answers are article text. When an article has several analyzer
versions, the most recently updated briefing is served.

The list uses the same keyset pagination as the catalog: (published_at DESC, id DESC).
"""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, status
from sqlalchemy import BigInteger, Column, DateTime, MetaData, Select, Table, Text, select, tuple_
from sqlalchemy.dialects.postgresql import JSONB, distinct_on

from app.api.caching import cached_json
from app.api.deps import DbSession
from app.api.v1.catalog import MAX_LIMIT, _decode_cursor, _encode_cursor
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
    cursor: Annotated[str | None, Query(description="`next_cursor` from a previous page.")] = None,
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
    items = [_summary(row.Article, row.briefing) for row in page]
    last = page[-1].Article if page else None
    next_cursor = _encode_cursor(last.published_at, last.id) if len(rows) > limit and last else None
    return cached_json(request, Page[BriefingSummary](items=items, next_cursor=next_cursor))


@router.get(
    "/articles/{article_id}/briefing",
    response_model=BriefingDetail,
    tags=["Briefings"],
    responses={404: {"description": "No briefing for this article"}},
)
async def get_briefing(
    request: Request, db: DbSession, article_id: Annotated[int, Path(ge=1)]
) -> Response:
    row = (await db.execute(_briefing_query().where(Article.id == article_id))).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Briefing not found")
    article: Article = row.Article
    source: Source = row.Source
    data: dict[str, Any] = row.briefing
    detail = BriefingDetail(
        article_id=article.id,
        title=article.title,
        url=article.canonical_url,
        source_url=article.canonical_url,
        published_at=article.published_at,
        source=BriefingSource(slug=source.slug, name=source.name, site_url=source.site_url),
        analyzer_version=row.analyzer_version,
        status=row.status,
        review_required=bool(data.get("review_required")),
        removed_items=int(data.get("removed_items") or 0),
        updated_at=row.updated_at,
        briefing=BriefingOut.model_validate(data),
    )
    return cached_json(request, detail)


# --- helpers ---------------------------------------------------------------------------


def _briefing_query() -> Select[Any]:
    latest = (
        select(analysis_briefings)
        .ext(distinct_on(analysis_briefings.c.article_id))
        .order_by(analysis_briefings.c.article_id, analysis_briefings.c.updated_at.desc())
        .subquery()
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
        .join(latest, latest.c.article_id == Article.id)
        .join(Source, Article.source_id == Source.id)
        .order_by(Article.published_at.desc(), Article.id.desc())
    )


def _summary(article: Article, data: dict[str, Any]) -> BriefingSummary:
    briefing = BriefingOut.model_validate(data)
    actors = sorted(briefing.who, key=lambda a: _INVOLVEMENT.get(a.involvement, 9))[:TOP]
    places = sorted(briefing.where, key=lambda p: _PLACE_ROLE.get(p.role, 9))[:TOP]
    return BriefingSummary(
        article_id=article.id,
        title=article.title,
        canonical_url=article.canonical_url,
        published_at=article.published_at,
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
        review_required=bool(data.get("review_required")),
    )
