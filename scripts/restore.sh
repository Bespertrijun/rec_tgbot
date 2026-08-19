#!/bin/sh
set -eu
: "${DATABASE_URL:?DATABASE_URL is required}"
: "${DUMP_FILE:?DUMP_FILE is required}"
case "$DATABASE_URL" in
  postgresql+asyncpg://*) PGURL="postgresql://${DATABASE_URL#postgresql+asyncpg://}" ;;
  postgresql+psycopg://*) PGURL="postgresql://${DATABASE_URL#postgresql+psycopg://}" ;;
  *) PGURL="$DATABASE_URL" ;;
esac
pg_restore --clean --if-exists --no-owner --dbname="$PGURL" "$DUMP_FILE"
psql "$PGURL" -v ON_ERROR_STOP=1 -c "UPDATE service_state SET write_enabled = FALSE, reason = 'restore_reconcile_required', updated_at = NOW() WHERE id = 1;"
echo "Restore complete. Service remains write-disabled until health check, full sync, reconcile, and explicit recovery_enable."
