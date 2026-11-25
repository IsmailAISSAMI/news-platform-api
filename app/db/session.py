import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# The API's queries are small keyset pages: anything slower is a bug or an abuse.
API_STATEMENT_TIMEOUT_MS = 5000


class PrivilegeError(RuntimeError):
    """The read-only engine connects with more rights than the API may have."""


def create_engine(settings: Settings, *, read_only: bool = False) -> AsyncEngine:
    """An engine for ingestion/migrations (read-write) or for the API (`read_only=True`).

    Read-only engines connect with `default_transaction_read_only=on` and a statement
    timeout, so PostgreSQL itself rejects any write, whatever the credentials. If
    `API_DATABASE_URL` is set (a login user in the SELECT-only `news_api` role, see
    scripts/db_roles.sql), it is used as a second, privilege-level layer; see
    `assert_least_privilege`.

    `hide_parameters=True`: bound values (which can be article text) never reach logs or
    exception messages.
    """
    url = settings.database_url
    connect_args: dict[str, object] = {"connect_timeout": 5}
    if read_only:
        url = settings.api_database_url or settings.database_url
        connect_args["options"] = (
            f"-c default_transaction_read_only=on -c statement_timeout={API_STATEMENT_TIMEOUT_MS}"
        )
    return create_async_engine(
        url.get_secret_value(),
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args=connect_args,
    )


async def assert_least_privilege(engine: AsyncEngine, settings: Settings | None = None) -> None:
    """Refuse (production) or warn (elsewhere) if the API's login could read article text.

    Call it on the read-only engine at startup. A superuser, or any role that can SELECT
    `article_bodies`, breaks the rights model (docs/architecture.md 5.4). `settings`
    defaults to `get_settings()`.
    """
    settings = settings or get_settings()
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT current_user, r.rolsuper,"
                    " CASE WHEN to_regclass('public.article_bodies') IS NULL THEN false"
                    " ELSE has_table_privilege(current_user, 'public.article_bodies', 'SELECT')"
                    " END"
                    " FROM pg_roles r WHERE r.rolname = current_user"
                )
            )
        ).one()
    user, superuser, reads_bodies = str(row[0]), bool(row[1]), bool(row[2])
    if not (superuser or reads_bodies):
        return
    msg = (
        f"the API connects as {user!r} (superuser={superuser}, can read article_bodies="
        f"{reads_bodies}); set API_DATABASE_URL to a news_api_login user"
    )
    if settings.is_production:
        raise PrivilegeError(msg)
    logger.warning("Least privilege not met: %s", msg)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
