#!/bin/bash
# Read-only role for Grafana; password comes from the compose environment.
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
create role grafana login password '${GRAFANA_DB_PASSWORD}';
grant usage on schema public to grafana;
grant select on all tables in schema public to grafana;
alter default privileges in schema public grant select on tables to grafana;
SQL
