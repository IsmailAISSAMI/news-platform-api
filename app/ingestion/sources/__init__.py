"""Per-source adapters: how to page a source's feed and map its entries to `FetchedItem`."""

from app.ingestion.sources.wordpress import WordPressAdapter

# Keyed by `sources.slug`.
ADAPTERS: dict[str, WordPressAdapter] = {
    "hespress-en": WordPressAdapter(),
}

__all__ = ["ADAPTERS", "WordPressAdapter"]
