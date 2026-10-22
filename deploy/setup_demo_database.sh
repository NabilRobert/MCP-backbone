#!/usr/bin/env bash
# Moves the public demo database (Pagila) onto this VPS's own Postgres, so
# it can have what the managed host couldn't give it: a real read-only
# login. Strangers' queries are then blocked from writing by the database
# itself, not only by the app's validator.
#
# Run on the VPS, as ubuntu, from anywhere:
#   bash ~/MCP-backbone/deploy/setup_demo_database.sh
# Prerequisite: the app is pulled and `alembic upgrade head` has run, so
# existing default workspaces follow APP_TARGET_DATABASE_URL instead of
# holding a copy of the old one (migration e8b2a6f1c3d9).
#
# What it does:
#   1. Creates database pagila_demo, owned by a loader-only role
#      pagila_owner, and stops PUBLIC from connecting to it or to the
#      bookkeeping database (Postgres lets every role connect to every
#      database by default; the app's own user keeps its access).
#   2. Loads Pagila into it with scripts/load_pagila_dataset.py.
#   3. Revokes CREATE on the schema and TEMP on the database from PUBLIC,
#      then creates pagila_ro with scripts/provision_readonly_role.py:
#      SELECT only, no EXECUTE on the schema's functions.
#   4. Locks pagila_owner (NOLOGIN, password removed).
#   5. Proves pagila_ro can read, can't write, and can't open the
#      bookkeeping database -- and stops before touching the app if not.
#   6. Points APP_TARGET_DATABASE_URL at pagila_ro (old .env kept as
#      .env.bak-<timestamp>) and restarts the service.
#
# Secrets are passed through environment variables and stdin, never
# printed. Undo: restore the .env.bak file and restart mcp-backbone.
# The demo database holds only public sample data, so the nightly
# bookkeeping backup deliberately doesn't include it: rerun this to rebuild.

set -euo pipefail

APP_DIR="$HOME/MCP-backbone"
DB=pagila_demo
OWNER=pagila_owner
RO=pagila_ro
PY="$APP_DIR/.venv/bin/python"
cd "$APP_DIR"

as_postgres() { sudo -u postgres psql -v ON_ERROR_STOP=1 -qAt "$@"; }

echo "== Checking prerequisites"
current=$(.venv/bin/alembic current 2>/dev/null | awk '{print $1}')
head=$(.venv/bin/alembic heads 2>/dev/null | awk '{print $1}')
if [ -z "$current" ] || [ "$current" != "$head" ]; then
    echo "Database schema is at '$current', not '$head'. Run: .venv/bin/alembic upgrade head" >&2
    exit 1
fi
if [ "$(as_postgres -c "SELECT 1 FROM pg_database WHERE datname = '$DB'")" = "1" ]; then
    echo "Database $DB already exists. Drop it first to rebuild:" >&2
    echo "  sudo -u postgres dropdb $DB" >&2
    exit 1
fi
read -r BOOKKEEPING_DB BOOKKEEPING_USER < <("$PY" -c '
from sqlalchemy.engine.url import make_url
from app.config import settings
url = make_url(settings.database_url)
print(url.database, url.username)')
echo "   bookkeeping database: $BOOKKEEPING_DB (app user $BOOKKEEPING_USER)"

echo "== 1. Creating $DB and restricting who can connect"
OWNER_PW=$(openssl rand -hex 24)
as_postgres <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$OWNER') THEN
    CREATE ROLE $OWNER;
  END IF;
END \$\$;
ALTER ROLE $OWNER LOGIN CREATEROLE PASSWORD '$OWNER_PW';
CREATE DATABASE $DB OWNER $OWNER;
REVOKE CONNECT ON DATABASE $DB FROM PUBLIC;
GRANT CONNECT ON DATABASE "$BOOKKEEPING_DB" TO "$BOOKKEEPING_USER";
REVOKE CONNECT ON DATABASE "$BOOKKEEPING_DB" FROM PUBLIC;
SQL

echo "== 2. Loading Pagila (downloads the dump on first run)"
export OWNER_URL="postgresql://$OWNER:$OWNER_PW@127.0.0.1:5432/$DB"
"$PY" -c '
import os
from pathlib import Path
from scripts.load_pagila_dataset import load
load("psql", os.environ["OWNER_URL"], Path.home() / ".cache" / "pagila")'

echo "== 3. Closing PUBLIC grants, then creating read-only role $RO"
# Belt and braces: the loader already drops the dump's own
# "GRANT ALL ON SCHEMA public TO PUBLIC", and TEMP is granted to PUBLIC on
# every new database by default. This database exists only for the demo,
# so nothing else depends on either.
as_postgres -d "$DB" <<SQL
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE TEMPORARY ON DATABASE $DB FROM PUBLIC;
SQL
RO_PW=$("$PY" -c '
import asyncio, os
from scripts.provision_readonly_role import provision
print(asyncio.run(provision(os.environ["OWNER_URL"], "'"$RO"'", "'"$DB"'", "public")))')
unset OWNER_URL OWNER_PW

echo "== 4. Locking the loader role"
as_postgres -c "ALTER ROLE $OWNER NOLOGIN NOCREATEROLE PASSWORD NULL"

echo "== 5. Proving $RO is read-only"
ro_psql() { PGPASSWORD="$RO_PW" psql -h 127.0.0.1 -U "$RO" -v ON_ERROR_STOP=1 -qAt "$@"; }
films=$(ro_psql -d "$DB" -c "SELECT count(*) FROM film")
echo "   reads: $films films"
[ "$films" -gt 0 ] || { echo "   FAILED: no rows readable" >&2; exit 1; }
for statement in "CREATE TABLE should_fail (x int)" \
                 "CREATE TEMP TABLE should_fail_tmp (x int)" \
                 "UPDATE film SET title = title WHERE false" \
                 "DELETE FROM rental WHERE false"; do
    if ro_psql -d "$DB" -c "$statement" >/dev/null 2>&1; then
        echo "   FAILED: '$statement' was allowed" >&2
        exit 1
    fi
    echo "   refused, as it should be: $statement"
done
if ro_psql -d "$BOOKKEEPING_DB" -c "SELECT 1" >/dev/null 2>&1; then
    echo "   FAILED: $RO can connect to the bookkeeping database" >&2
    exit 1
fi
echo "   refused, as it should be: connecting to $BOOKKEEPING_DB"

echo "== 6. Switching the app's default database and restarting"
backup=".env.bak-$(date +%Y%m%d-%H%M%S)"
cp .env "$backup"
chmod 600 "$backup"
NEW_URL="postgresql+asyncpg://$RO:$RO_PW@127.0.0.1:5432/$DB"
if grep -q '^APP_TARGET_DATABASE_URL=' .env; then
    sed -i "s|^APP_TARGET_DATABASE_URL=.*|APP_TARGET_DATABASE_URL=$NEW_URL|" .env
else
    echo "APP_TARGET_DATABASE_URL=$NEW_URL" >> .env
fi
chmod 600 .env
unset RO_PW NEW_URL
sudo systemctl restart mcp-backbone
sleep 3
systemctl is-active mcp-backbone

echo "== Done. Previous .env kept as $APP_DIR/$backup"
echo "   Undo: cp $backup .env && sudo systemctl restart mcp-backbone"
