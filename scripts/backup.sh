#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

BACKUP_DIR="${BACKUP_DIR:-/backups}"
UPLOADS_DIR="${UPLOADS_DIR:-/app-data}"
BACKUP_INTERVAL_SECONDS="${BACKUP_INTERVAL_SECONDS:-86400}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
BACKUP_RETRY_SECONDS="${BACKUP_RETRY_SECONDS:-300}"
BACKUP_TIMEOUT_SECONDS="${BACKUP_TIMEOUT_SECONDS:-3600}"
BACKUP_ENABLED="${BACKUP_ENABLED:-1}"
export PGHOST="${PGHOST:-postgres}"
export PGPORT="${PGPORT:-5432}"
export PGDATABASE="${PGDATABASE:-${POSTGRES_DB:-contracts}}"
export PGUSER="${PGUSER:-${POSTGRES_USER:-user}}"
export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
source "$(dirname "$0")/backup-control.sh"

validate_config() {
    local variable value
    for variable in BACKUP_INTERVAL_SECONDS BACKUP_RETENTION_DAYS BACKUP_RETRY_SECONDS BACKUP_TIMEOUT_SECONDS; do
        value="${!variable}"
        if [[ ! "$value" =~ ^[1-9][0-9]{0,7}$ ]]; then
            log "ERROR: $variable must be a positive integer (at most 8 digits)." >&2
            return 1
        fi
    done
    BACKUP_DIR="$(realpath -m -- "$BACKUP_DIR")"
    UPLOADS_DIR="$(realpath -m -- "$UPLOADS_DIR")"
    if [[ "$BACKUP_DIR" == / || "$UPLOADS_DIR" == / || "$BACKUP_DIR" == "$UPLOADS_DIR" || "$BACKUP_DIR" == "$UPLOADS_DIR/"* ]]; then
        log "ERROR: Backup and upload directories must be separate, non-root directories." >&2
        return 1
    fi
}

valid_snapshot_name() { [[ "$1" =~ ^snapshot-[0-9]{8}T[0-9]{6}Z-[a-zA-Z0-9]{6}$ ]]; }

last_success() {
    local name timestamp
    name="$(cat "$BACKUP_DIR/.last-success" 2>/dev/null || true)"
    if valid_snapshot_name "$name" && [[ ! -L "$BACKUP_DIR/$name" && -s "$BACKUP_DIR/$name/database.dump" && -s "$BACKUP_DIR/$name/uploads.tar.gz" ]]; then
        timestamp="$(cat "$BACKUP_DIR/$name/completed-at" 2>/dev/null || true)"
        if [[ "$timestamp" =~ ^[0-9]{1,12}$ ]]; then
            printf '%s\n' "$timestamp"
            return
        fi
    fi
    printf '0\n'
}

prune_snapshots() {
    local directory name cutoff timestamp
    cutoff=$(( $(date +%s) - BACKUP_RETENTION_DAYS * 86400 ))
    while IFS= read -r -d '' directory; do
        name="${directory##*/}"
        valid_snapshot_name "$name" || continue
        [[ -f "$directory/completed-at" && -f "$directory/SHA256SUMS" ]] || continue
        timestamp="$(cat "$directory/completed-at")"
        [[ "$timestamp" =~ ^[0-9]{1,12}$ ]] || continue
        if (( timestamp < cutoff )) && [[ "$name" != "$(cat "$BACKUP_DIR/.last-success")" ]]; then
            rm -rf -- "$directory"
            log "Removed expired automatic backup: $name"
        fi
    done < <(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name 'snapshot-*' -print0)
}

run_backup() (
    set -Eeuo pipefail
    mkdir -p -- "$BACKUP_DIR"
    exec 9>"$BACKUP_DIR/.backup.lock"
    if ! flock -n 9; then
        log "Another backup is already running." >&2
        exit 75
    fi
    load_control || true
    record_worker running
    record_request running
    heartbeat_pid=""
    staging=""
    marker=""
    snapshot=""
    finish_run() {
        local code=$?
        trap - EXIT
        if [[ -n "$heartbeat_pid" ]]; then kill "$heartbeat_pid" 2>/dev/null || true; wait "$heartbeat_pid" 2>/dev/null || true; fi
        [[ -z "$staging" ]] || rm -rf -- "$staging"
        [[ -z "$marker" ]] || rm -f -- "$marker"
        if (( code == 0 )); then record_worker idle; record_request success "$snapshot";
        else record_worker failed; record_request failed; fi
        exit "$code"
    }
    trap finish_run EXIT
    if [[ "${BACKUP_MANAGEMENT_ENABLED:-0}" == 1 ]]; then
        (exec 9>&-; while sleep 30; do record_worker running; done) &
        heartbeat_pid=$!
    fi
    [[ -d "$UPLOADS_DIR" ]] || { log "ERROR: Upload volume is not mounted." >&2; exit 1; }

    # Only directories made by this script are ever removed. Legacy/manual dumps stay untouched.
    while IFS= read -r -d '' abandoned; do
        [[ "${abandoned##*/}" =~ ^\.incomplete-[0-9]{8}T[0-9]{6}Z-[a-zA-Z0-9]{6}$ ]] && rm -rf -- "$abandoned"
    done < <(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '.incomplete-*' -print0)

    staging="$(mktemp -d "$BACKUP_DIR/.incomplete-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
    marker="$BACKUP_DIR/.last-success.$$"
    log "Creating database and upload backup."
    timeout "$BACKUP_TIMEOUT_SECONDS" pg_dump --format=custom --lock-wait-timeout=60s --file="$staging/database.dump"
    [[ -s "$staging/database.dump" ]]
    timeout "$BACKUP_TIMEOUT_SECONDS" pg_restore --file=/dev/null "$staging/database.dump"

    # Uploads have immutable random filenames; copy after the database snapshot.
    # Concurrently changing files make tar fail, so no incomplete set is published.
    timeout "$BACKUP_TIMEOUT_SECONDS" tar -czf "$staging/uploads.tar.gz" -C "$UPLOADS_DIR" .
    timeout "$BACKUP_TIMEOUT_SECONDS" tar -tzf "$staging/uploads.tar.gz" > /dev/null
    (cd "$staging" && sha256sum database.dump uploads.tar.gz > SHA256SUMS)
    date +%s > "$staging/completed-at"
    printf 'format=1\ncreated_utc=%s\ndatabase=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PGDATABASE" > "$staging/manifest.txt"
    snapshot="snapshot-${staging##*/.incomplete-}"
    mv -- "$staging" "$BACKUP_DIR/$snapshot"
    share_snapshot "$BACKUP_DIR/$snapshot"
    printf '%s\n' "$snapshot" > "$marker"
    mv -f -- "$marker" "$BACKUP_DIR/.last-success"
    log "Backup completed and verified: $snapshot"
    prune_snapshots
)

verify_snapshot() (
    set -Eeuo pipefail
    local name="${1:-$(cat "$BACKUP_DIR/.last-success" 2>/dev/null || true)}"
    valid_snapshot_name "$name" || { log 'ERROR: Invalid snapshot name.' >&2; exit 1; }
    [[ ! -L "$BACKUP_DIR/$name" ]]
    cd "$BACKUP_DIR/$name"
    sha256sum --check SHA256SUMS
    timeout "$BACKUP_TIMEOUT_SECONDS" pg_restore --file=/dev/null database.dump
    timeout "$BACKUP_TIMEOUT_SECONDS" tar -tzf uploads.tar.gz > /dev/null
    log "Verified: $name"
)

validate_config
load_control || true
case "${1:-schedule}" in
    run) run_backup ;;
    verify) verify_snapshot "${2:-}" ;;
    healthcheck)
        if [[ "${BACKUP_MANAGEMENT_ENABLED:-0}" == 1 ]]; then
            (( WORKER_HEARTBEAT > 0 && $(date +%s) - WORKER_HEARTBEAT < 120 )) || exit 1
            [[ "$BACKUP_ENABLED" != 0 ]] || exit 0
        fi
        last="$(last_success)"
        # Allow one full run (dump, validation, archive, validation) plus retries.
        max_age=$(( BACKUP_INTERVAL_SECONDS + 4 * BACKUP_TIMEOUT_SECONDS + 2 * BACKUP_RETRY_SECONDS ))
        (( last > 0 && $(date +%s) - last <= max_age ))
        ;;
    schedule)
        share_existing_snapshots
        # Clear a stale running state after a restart, but never mask an active manual run.
        if flock -n "$BACKUP_DIR/.backup.lock" true; then record_worker idle; fi
        log "Automatic backups: every $BACKUP_INTERVAL_SECONDS seconds, retain $BACKUP_RETENTION_DAYS days."
        retry_at=0
        while true; do
            load_control || true
            record_worker
            remaining=$(( $(last_success) + BACKUP_INTERVAL_SECONDS - $(date +%s) ))
            if [[ -n "$PENDING_REQUEST" ]] || (( BACKUP_ENABLED == 1 && remaining <= 0 && $(date +%s) >= retry_at )); then
                # Run in a separate shell so errexit remains active even inside this condition.
                if BACKUP_REQUEST_ID="$PENDING_REQUEST" bash "$0" run; then
                    retry_at=0
                    continue
                else
                    log "ERROR: Backup failed. Existing backups retained; retry in $BACKUP_RETRY_SECONDS seconds." >&2
                    retry_at=$(( $(date +%s) + BACKUP_RETRY_SECONDS ))
                fi
            fi
            # Poll settings and queued requests without restarting the container.
            pause=10
            (( BACKUP_RETRY_SECONDS < pause )) && pause="$BACKUP_RETRY_SECONDS"
            sleep "$pause"
        done
        ;;
    *) printf 'Usage: %s {schedule|run|verify [snapshot-name]|healthcheck}\n' "$0" >&2; exit 2 ;;
esac
