"""SQLAlchemy models. Import every model module here so Alembic autogenerate sees it."""

from app.models.analysis_run import AnalysisRun
from app.models.api_call import ApiCall
from app.models.article import Article
from app.models.article_analysis import ArticleAnalysis
from app.models.article_body import ArticleBody
from app.models.discovered_url import DiscoveredUrl
from app.models.feed import Feed
from app.models.ingestion_run import IngestionRun
from app.models.source import Source

__all__ = [
    "AnalysisRun",
    "ApiCall",
    "Article",
    "ArticleAnalysis",
    "ArticleBody",
    "DiscoveredUrl",
    "Feed",
    "IngestionRun",
    "Source",
]
