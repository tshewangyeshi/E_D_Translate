#!/usr/bin/env bash
# Nightly backup of the dzweb database on the GovTech VM.
#
# Run by dzweb-backup.timer (deploy/vm/setup-root.sh) as the app user, with
# DZWEB_PG_DSN from ~/dzweb/.env.infra. By hand:
#
#     set -a; . ~/dzweb/.env.infra; set +a; bash ~/dzweb/deploy/vm/backup.sh
#
# What it protects: approved translations (DCDD's work, FR-413), the audit trail
# (FR-620) and error reports. The cache and queue are rebuilt on their own.
#
# Each backup is a pg_dump custom-format file in ~/dzweb-backups, readable by
# the app user only (the error reports may hold what a citizen typed, NFR-303).
# A backup counts only once pg_restore can read its table of contents; one that
# cannot is deleted and the run fails, so the timer's failure shows in
# `systemctl status dzweb-backup`. Backups older than DZWEB_BACKUP_DAYS (14)
# are removed.
#
# These copies live on the same VM: they undo a bad migration, a mistaken
# import or a deleted row, not the loss of the VM. Copy them off it as well.
#
# Restore (stops the services; replaces the database's contents):
#     sudo systemctl stop dzweb-api dzweb-worker
#     set -a; . ~/dzweb/.env.infra; set +a
#     PGPASSWORD=... pg_restore --clean --if-exists --no-owner -h 127.0.0.1 -U dzweb -d dzweb FILE
#     sudo systemctl start dzweb-api dzweb-worker
set -euo pipefail

: "${DZWEB_PG_DSN:?DZWEB_PG_DSN is not set (it is in ~/dzweb/.env.infra)}"
DIR="${DZWEB_BACKUP_DIR:-$HOME/dzweb-backups}"
DAYS="${DZWEB_BACKUP_DAYS:-14}"

# postgresql://user:password@host:port/db -- the password goes to libpq through
# the environment, never on the command line, where other users of this shared
# VM could read it in the process list.
rest="${DZWEB_PG_DSN#postgresql://}"
case "$rest" in
  *@*) creds="${rest%%@*}"; hostpart="${rest#*@}" ;;
  *) creds=""; hostpart="$rest" ;;
esac
export PGUSER="${creds%%:*}"
case "$creds" in
  *:*) export PGPASSWORD="${creds#*:}" ;;
esac
hostport="${hostpart%%/*}"
export PGHOST="${hostport%%:*}"
export PGPORT="${hostport##*:}"
[ "$PGPORT" = "$hostport" ] && PGPORT=5432
export PGDATABASE="${hostpart#*/}"
PGDATABASE="${PGDATABASE%%\?*}"

umask 077
mkdir -p "$DIR"
chmod 700 "$DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$DIR/dzweb-$stamp.dump"
partial="$target.partial"
trap 'rm -f "$partial"' EXIT

pg_dump --format=custom --compress=6 --no-owner --file="$partial"
if ! pg_restore --list "$partial" > /dev/null; then
  echo "backup unreadable: $target not kept" >&2
  exit 1
fi
mv "$partial" "$target"
echo "backup $target $(du -h "$target" | cut -f1)"

find "$DIR" -maxdepth 1 -name 'dzweb-*.dump' -type f -mtime "+$DAYS" -print -delete \
  | sed 's/^/removed /'
