#!/usr/bin/env bash
# Only numeric settings and opaque request IDs cross the database/worker boundary.
control_sql() { timeout 20 psql -X -q -A -t -F '|' -v ON_ERROR_STOP=1 "$@"; }

load_control() {
    PENDING_REQUEST=""
    WORKER_HEARTBEAT=0
    [[ "${BACKUP_MANAGEMENT_ENABLED:-0}" == 1 ]] || return 0
    local rows key value
    if ! rows="$(control_sql <<'SQL'
SELECT key, value FROM app_settings WHERE key IN (
 'backup_enabled', 'backup_interval_seconds', 'backup_retention_days', 'backup_retry_seconds', 'backup_timeout_seconds');
SELECT 'request', value::jsonb->>'id' FROM app_settings
 WHERE key = 'backup_request' AND value::jsonb->>'status' IN ('queued', 'running');
SELECT 'heartbeat', value::jsonb->>'heartbeat' FROM app_settings WHERE key = 'backup_worker';
SQL
    )"; then
        log 'ERROR: Could not read backup management settings.' >&2
        return 1
    fi
    while IFS='|' read -r key value; do
        case "$key" in
            backup_enabled) [[ "$value" =~ ^[01]$ ]] && BACKUP_ENABLED="$value" ;;
            backup_interval_seconds) [[ "$value" =~ ^[1-9][0-9]{0,7}$ ]] && BACKUP_INTERVAL_SECONDS="$value" ;;
            backup_retention_days) [[ "$value" =~ ^[1-9][0-9]{0,7}$ ]] && BACKUP_RETENTION_DAYS="$value" ;;
            backup_retry_seconds) [[ "$value" =~ ^[1-9][0-9]{0,7}$ ]] && BACKUP_RETRY_SECONDS="$value" ;;
            backup_timeout_seconds) [[ "$value" =~ ^[1-9][0-9]{0,7}$ ]] && BACKUP_TIMEOUT_SECONDS="$value" ;;
            request) [[ "$value" =~ ^[a-f0-9]{32}$ ]] && PENDING_REQUEST="$value" ;;
            heartbeat) [[ "$value" =~ ^[0-9]{1,12}$ ]] && WORKER_HEARTBEAT="$value" ;;
        esac
    done <<< "$rows"
    return 0
}

record_worker() {
    [[ "${BACKUP_MANAGEMENT_ENABLED:-0}" == 1 ]] || return 0
    control_sql -v state="${1:-}" <<'SQL' >/dev/null || return 0
INSERT INTO app_settings (key, value, updated_at)
VALUES ('backup_worker', jsonb_build_object('status', COALESCE(NULLIF(:'state', ''), 'idle'),
 'heartbeat', floor(extract(epoch FROM clock_timestamp())), 'version', 1)::text, now()::text)
ON CONFLICT(key) DO UPDATE SET value = (app_settings.value::jsonb || jsonb_build_object(
 'heartbeat', floor(extract(epoch FROM clock_timestamp())), 'version', 1) ||
 CASE WHEN :'state' = '' THEN '{}'::jsonb ELSE jsonb_build_object('status', :'state') END)::text,
 updated_at = now()::text;
SQL
}

record_request() {
    [[ "${BACKUP_MANAGEMENT_ENABLED:-0}" == 1 && "${BACKUP_REQUEST_ID:-}" =~ ^[a-f0-9]{32}$ ]] || return 0
    control_sql -v request_id="$BACKUP_REQUEST_ID" -v state="$1" -v snapshot="${2:-}" <<'SQL' >/dev/null || return 0
UPDATE app_settings SET value = (value::jsonb || jsonb_build_object(
 'status', :'state', 'snapshot', :'snapshot', 'updated_at', floor(extract(epoch FROM clock_timestamp()))))::text,
 updated_at = now()::text WHERE key = 'backup_request' AND value::jsonb->>'id' = :'request_id';
SQL
}

share_snapshot() {
    [[ "${BACKUP_MANAGEMENT_ENABLED:-0}" == 1 ]] || return 0
    local directory="$1" filename
    valid_snapshot_name "${directory##*/}" && [[ ! -L "$directory" && -d "$directory" ]] || return 0
    chgrp -- "${BACKUP_READ_GID:-1000}" "$directory"
    chmod 0750 -- "$directory"
    for filename in database.dump uploads.tar.gz SHA256SUMS manifest.txt completed-at; do
        [[ -f "$directory/$filename" && ! -L "$directory/$filename" ]] || continue
        chgrp -- "${BACKUP_READ_GID:-1000}" "$directory/$filename"
        chmod 0640 -- "$directory/$filename"
    done
}

share_existing_snapshots() {
    [[ "${BACKUP_MANAGEMENT_ENABLED:-0}" == 1 ]] || return 0
    mkdir -p -- "$BACKUP_DIR"
    chgrp -- "${BACKUP_READ_GID:-1000}" "$BACKUP_DIR"
    chmod 0750 -- "$BACKUP_DIR"
    local directory
    while IFS= read -r -d '' directory; do share_snapshot "$directory"; done < <(
        find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name 'snapshot-*' -print0
    )
}
