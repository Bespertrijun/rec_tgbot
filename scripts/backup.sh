#!/bin/sh
set -eu
: "${DATABASE_URL:?DATABASE_URL is required}"
: "${BACKUP_DIR:=./backups}"
case "$DATABASE_URL" in
  postgresql+asyncpg://*) PGURL="postgresql://${DATABASE_URL#postgresql+asyncpg://}" ;;
  postgresql+psycopg://*) PGURL="postgresql://${DATABASE_URL#postgresql+psycopg://}" ;;
  *) PGURL="$DATABASE_URL" ;;
esac
mkdir -p "$BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
pg_dump "$PGURL" --format=custom --file="$BACKUP_DIR/reclaude-$stamp.dump"
find "$BACKUP_DIR" -type f -name 'reclaude-*.dump' -mtime +30 -delete
