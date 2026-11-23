from enum import StrEnum


class RightsPolicy(StrEnum):
    """What may be fetched, stored, analysed and served for a source (architecture §5.3)."""

    DISABLED = "disabled"
    METADATA_ONLY = "metadata_only"
    RESTRICTED = "restricted"
    LICENSED_FULL_TEXT = "licensed_full_text"


class FeedKind(StrEnum):
    RSS = "rss"
    NEWS_SITEMAP = "news_sitemap"
    ARCHIVE_INDEX = "archive_index"


class FeedState(StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class DiscoveryChannel(StrEnum):
    """How an article first entered the database."""

    RSS = "rss"
    NEWS_SITEMAP = "news_sitemap"
    ARCHIVE = "archive"


class BacklogStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    FAILED = "failed"
    GONE = "gone"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


def check_in(column: str, enum: type[StrEnum]) -> str:
    """SQL for a CHECK constraint restricting `column` to the enum's values."""
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"
