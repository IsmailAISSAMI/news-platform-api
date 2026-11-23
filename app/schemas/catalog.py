from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, RootModel


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)


class SourceRef(_Schema):
    """Attribution for an article: who published it and where."""

    slug: str
    name: str
    site_url: str


class SourceOut(SourceRef):
    id: int
    language: str
    rights_policy: str
    active: bool


class FeedOut(_Schema):
    id: int
    source: str = Field(description="Source slug.")
    url: str
    kind: str
    state: str
    consecutive_failures: int
    last_success_at: datetime | None
    checkpoint_published_at: datetime | None
    uncovered_since: datetime | None = Field(
        description="Start of a window the RSS feed could not reach (archive backlog)."
    )
    uncovered_until: datetime | None


class ArticleOut(_Schema):
    """Article metadata. There is deliberately no body: read the original via `url`."""

    id: int
    title: str
    url: str = Field(description="Canonical link to the original article (always link out).")
    excerpt: str | None = Field(description="Only if the source's rights policy allows one.")
    author: str | None
    categories: list[str]
    language: str
    published_at: datetime
    modified_at: datetime | None
    source: SourceRef


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next page; null on the last page."
    )


class SourceList(RootModel[list[SourceOut]]):
    pass


class FeedList(RootModel[list[FeedOut]]):
    pass
