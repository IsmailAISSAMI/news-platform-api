from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings, *, read_only: bool = False) -> AsyncEngine:
    """An engine for ingestion/migrations (read-write) or for the API (`read_only=True`).

    Read-only engines connect with `default_transaction_read_only=on`, so PostgreSQL itself
    rejects any write, whatever the credentials. If `API_DATABASE_URL` is set (a login user
    in the SELECT-only `news_api` role, see scripts/db_roles.sql), it is used as a second,
    privilege-level layer.
    """
    url = settings.database_url
    connect_args: dict[str, object] = {"connect_timeout": 5}
    if read_only:
        url = settings.api_database_url or settings.database_url
        connect_args["options"] = "-c default_transaction_read_only=on"
    return create_async_engine(
        url.get_secret_value(), pool_pre_ping=True, connect_args=connect_args
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
