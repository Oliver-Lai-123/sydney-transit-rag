#!/bin/bash
# Creates the role that executes LLM-generated SQL.
# Defence in depth: even if the SQL validator misses something, this role
# can only SELECT from the gtfs schema, cannot see docs or admin tables,
# runs every transaction read-only and gets a short statement timeout.
# No `set -u`: if this file loses its executable bit (common when committed
# from Windows), the Postgres entrypoint sources it instead of running it,
# and -u would then leak into the entrypoint's own shell.
set -eo pipefail

psql -v ON_ERROR_STOP=1 \
     -v ro_password="${READONLY_PASSWORD}" \
     -v db_name="${POSTGRES_DB}" \
     -v admin_user="${POSTGRES_USER}" \
     --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<-'EOSQL'
	CREATE ROLE transit_reader LOGIN PASSWORD :'ro_password';

	REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
	GRANT CONNECT ON DATABASE :"db_name" TO transit_reader;
	REVOKE CREATE ON SCHEMA public FROM PUBLIC;

	GRANT USAGE ON SCHEMA gtfs TO transit_reader;
	GRANT SELECT ON ALL TABLES IN SCHEMA gtfs TO transit_reader;
	ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_user" IN SCHEMA gtfs
	    GRANT SELECT ON TABLES TO transit_reader;

	ALTER ROLE transit_reader SET default_transaction_read_only = on;
	ALTER ROLE transit_reader SET statement_timeout = '5s';
	ALTER ROLE transit_reader SET idle_in_transaction_session_timeout = '10s';
	ALTER ROLE transit_reader SET search_path = gtfs;
EOSQL
