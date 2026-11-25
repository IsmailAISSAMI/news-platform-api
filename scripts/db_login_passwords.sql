-- Set the passwords of the login roles created by scripts/db_roles.sql.
--
-- Passwords come from psql variables, never from this file. Run db_roles.sql first, then:
--
--   read -rs API_PW && read -rs INGEST_PW
--   docker compose exec -T db psql -U news -d news -v ON_ERROR_STOP=1 \
--     -v api_password="$API_PW" -v ingest_password="$INGEST_PW" \
--     < scripts/db_login_passwords.sql
--   unset API_PW INGEST_PW
--
-- The server stores a SCRAM hash (password_encryption = scram-sha-256, the default).

\set ON_ERROR_STOP on
\if :{?api_password}
\else
  \echo 'missing -v api_password=...'
  \quit
\endif
\if :{?ingest_password}
\else
  \echo 'missing -v ingest_password=...'
  \quit
\endif

ALTER ROLE news_api_login PASSWORD :'api_password';
ALTER ROLE news_ingest_login PASSWORD :'ingest_password';
