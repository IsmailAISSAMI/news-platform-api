-- Database roles for least-privilege access (docs/architecture.md §6).
--
-- Run as the schema owner (the POSTGRES_USER), after `alembic upgrade head`:
--   docker compose exec -T db psql -U news -d news -v ON_ERROR_STOP=1 < scripts/db_roles.sql
--
-- Idempotent: safe to re-run after every migration.
--
-- The roles are NOLOGIN group roles. Login users that need them (for example the API's own
-- user, added in milestone 7) are created separately, with passwords kept out of git:
--   CREATE ROLE news_api_login LOGIN PASSWORD '...' IN ROLE news_api;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'news_ingest') THEN
        CREATE ROLE news_ingest NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'news_api') THEN
        CREATE ROLE news_api NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO news_ingest, news_api;

-- news_api: read-only. It must never modify data.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO news_api;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public FROM news_api;

-- news_ingest: reads and writes data, but cannot change the schema or truncate.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO news_ingest;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO news_ingest;
REVOKE CREATE ON SCHEMA public FROM news_ingest, news_api;

-- The migration bookkeeping table is the owner's business only.
REVOKE ALL ON alembic_version FROM news_ingest, news_api;

-- Full article text is stored privately for analysis and must never be served: the API
-- role cannot read it at all. (Default privileges below would otherwise grant SELECT on
-- every new table, so this explicit REVOKE must stay after the grants.)
REVOKE ALL ON article_bodies FROM news_api;
REVOKE ALL ON article_analyses FROM news_api;  -- evidence quotes are article text
REVOKE ALL ON api_calls, analysis_runs FROM news_api;  -- internal call and run log
-- Briefings (no evidence, no unverified item) reach the API only through this view, which
-- runs with its owner's privileges: news_api needs no right on article_analyses.
GRANT SELECT ON analysis_briefings TO news_api;

-- Tables created by future migrations get the same privileges automatically.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO news_api;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO news_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO news_ingest;
