-- Database roles for least-privilege access (docs/architecture.md §6).
--
-- Run as the database owner (neondb_owner on Neon), after `alembic upgrade head`:
--   read -rs NEON_URL    # the owner connection string, not shown
--   docker run --rm -i postgres:18-alpine psql "$NEON_URL" -v ON_ERROR_STOP=1 < scripts/db_roles.sql
--
-- Idempotent: safe to re-run after every migration. Plain SQL (no psql meta-commands), so
-- the tests can apply it inside a rolled-back transaction.
--
-- Group roles (NOLOGIN) hold the privileges. Login roles inherit them and are created
-- here WITHOUT a password (they cannot log in yet). Set their passwords with
-- scripts/db_login_passwords.sql, which reads them from psql variables: no password
-- literal is ever written to git.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'news_ingest') THEN
        CREATE ROLE news_ingest NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'news_api') THEN
        CREATE ROLE news_api NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'news_api_login') THEN
        CREATE ROLE news_api_login LOGIN INHERIT IN ROLE news_api;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'news_ingest_login') THEN
        CREATE ROLE news_ingest_login LOGIN INHERIT IN ROLE news_ingest;
    END IF;
END
$$;

-- Login roles are plain users: never superuser, never able to create roles or databases.
-- CREATE ROLE already defaults to all of these. Only a real superuser may write the
-- SUPERUSER attribute, so on Neon (whose owner is not a superuser) the re-assertion is
-- skipped: a non-superuser cannot have granted any of these attributes in the first place.
DO $$
BEGIN
    IF (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        ALTER ROLE news_api_login NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
        ALTER ROLE news_ingest_login NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
END
$$;
GRANT news_api TO news_api_login;
GRANT news_ingest TO news_ingest_login;

GRANT USAGE ON SCHEMA public TO news_ingest, news_api;
REVOKE CREATE ON SCHEMA public FROM news_ingest, news_api;

-- Remove the old fail-open default privilege: new tables are NOT readable by news_api
-- until listed below.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM news_api;

-- news_api: an explicit SELECT allowlist (tests/integration/test_database_security.py
-- asserts it exactly). Everything else, current or future, is unreadable.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM news_api;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM news_api;
GRANT SELECT ON articles, feeds, sources TO news_api;
-- Briefings (no evidence, no unverified item) reach the API only through this view, which
-- runs with its owner's privileges: news_api needs no right on article_analyses.
GRANT SELECT ON analysis_briefings TO news_api;

-- news_ingest: reads and writes data, but cannot change the schema or truncate.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO news_ingest;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO news_ingest;
REVOKE TRUNCATE ON ALL TABLES IN SCHEMA public FROM news_ingest;
-- The view is read-only by design (and auto-updatable in PostgreSQL): no writes through it.
REVOKE INSERT, UPDATE, DELETE ON analysis_briefings FROM news_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO news_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO news_ingest;

-- The migration bookkeeping table is the owner's business. news_ingest may only read it:
-- `news ingest` refuses to run unless the database is at the code's Alembic head.
REVOKE ALL ON alembic_version FROM news_ingest, news_api;
GRANT SELECT ON alembic_version TO news_ingest;
