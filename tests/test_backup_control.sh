#!/usr/bin/env bash
# Run only in a disposable PostgreSQL database with no application data.
set -Eeuo pipefail
SCRIPT="${1:-/opt/backup.sh}"
scratch="$(mktemp -d /tmp/backup-control-tests-XXXXXX)"
pid=""
cleanup() {
    if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi
    rm -rf -- "$scratch"
}
trap cleanup EXIT
export BACKUP_DIR="$scratch/backups" UPLOADS_DIR="$scratch/uploads"
export BACKUP_MANAGEMENT_ENABLED=1 BACKUP_READ_GID=1000 BACKUP_INTERVAL_SECONDS=86400
mkdir -p "$UPLOADS_DIR"
printf 'test contract document\n' > "$UPLOADS_DIR/document.pdf"
psql -X -v ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE app_settings (key text PRIMARY KEY, value text NOT NULL, updated_at text NOT NULL);
CREATE TABLE backup_test_business (id integer PRIMARY KEY, name text);
INSERT INTO backup_test_business VALUES (1, 'Recovery validation');
INSERT INTO app_settings VALUES ('backup_enabled', '0', now()::text), ('backup_retention_days', '14', now()::text);
SQL
bash "$SCRIPT" schedule > "$scratch/worker.log" 2>&1 &
pid=$!
wait_for() {
    local sql="$1" expected="$2" value
    for _ in {1..40}; do
        value="$(psql -X -At -c "$sql")"
        [[ "$value" != "$expected" ]] || return 0
        sleep 1
    done
    cat "$scratch/worker.log"
    printf 'Timed out: %s != %s\n' "$value" "$expected" >&2
    return 1
}
wait_for "SELECT value::jsonb->>'status' FROM app_settings WHERE key = 'backup_worker'" idle
[[ -z "$(find "$BACKUP_DIR" -maxdepth 1 -type d -name 'snapshot-*' -print)" ]]
bash "$SCRIPT" healthcheck
printf 'OK: paused automation remains healthy and reports heartbeat\n'
psql -X -v ON_ERROR_STOP=1 -c "INSERT INTO app_settings VALUES ('backup_request', '{\"id\":\"0123456789abcdef0123456789abcdef\",\"status\":\"queued\"}', now()::text)"
wait_for "SELECT value::jsonb->>'status' FROM app_settings WHERE key = 'backup_request'" success
first="$(cat "$BACKUP_DIR/.last-success")"
[[ "$(stat -c %g "$BACKUP_DIR/$first/database.dump")" == 1000 ]]
[[ "$(stat -c %a "$BACKUP_DIR/$first/database.dump")" == 640 ]]
[[ "$(stat -c %a "$BACKUP_DIR/$first")" == 750 ]]
bash "$SCRIPT" verify
printf 'OK: queued manual request creates and verifies a backup while automation is paused\n'
createdb backup_restore_validation
pg_restore --exit-on-error --single-transaction -d backup_restore_validation "$BACKUP_DIR/$first/database.dump"
[[ "$(psql -X -At -d backup_restore_validation -c 'SELECT name FROM backup_test_business WHERE id = 1')" == 'Recovery validation' ]]
[[ "$(tar -xOzf "$BACKUP_DIR/$first/uploads.tar.gz" ./document.pdf)" == 'test contract document' ]]
printf 'OK: real PostgreSQL restore and document extraction\n'
psql -X -v ON_ERROR_STOP=1 <<'SQL'
UPDATE app_settings SET value = '1' WHERE key = 'backup_enabled';
INSERT INTO app_settings VALUES ('backup_interval_seconds', '1', now()::text);
SQL
for _ in {1..40}; do
    [[ "$(cat "$BACKUP_DIR/.last-success")" == "$first" ]] || break
    sleep 1
done
[[ "$(cat "$BACKUP_DIR/.last-success")" != "$first" ]]
printf 'OK: changed database settings take effect without a worker restart\n'
kill "$pid"
wait "$pid" || true
pid=""
printf 'All backup management worker tests passed.\n'
