#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT="${1:-/opt/backup.sh}"
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
scratch="$(mktemp -d /tmp/backup-tests-XXXXXX)"
trap 'rm -rf -- "$scratch"' EXIT
mkdir -p "$scratch/bin" "$scratch/uploads/contract_documents" "$scratch/uploads/company_logos"
cp "$TESTS_DIR"/backup-mocks/* "$scratch/bin/"
chmod +x "$scratch/bin/"*
export PATH="$scratch/bin:$PATH"
export BACKUP_DIR="$scratch/backups" UPLOADS_DIR="$scratch/uploads"
export BACKUP_INTERVAL_SECONDS=86400 BACKUP_RETENTION_DAYS=14 BACKUP_RETRY_SECONDS=1 BACKUP_TIMEOUT_SECONDS=10
printf 'test-pdf\n' > "$UPLOADS_DIR/contract_documents/test.pdf"
printf 'test-logo\n' > "$UPLOADS_DIR/company_logos/test.png"

expect_failure() {
    if "$@"; then printf 'FAIL: command unexpectedly succeeded\n' >&2; exit 1; fi
}
count_snapshots() { find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name 'snapshot-*' | wc -l; }

expect_failure bash "$SCRIPT" healthcheck
bash "$SCRIPT" run
first="$(cat "$BACKUP_DIR/.last-success")"
[[ "$(cat "$BACKUP_DIR/$first/database.dump")" == test-database ]]
bash "$SCRIPT" verify
bash "$SCRIPT" healthcheck
[[ "$(/usr/bin/tar -xOzf "$BACKUP_DIR/$first/uploads.tar.gz" ./contract_documents/test.pdf)" == test-pdf ]]
[[ "$(/usr/bin/tar -xOzf "$BACKUP_DIR/$first/uploads.tar.gz" ./company_logos/test.png)" == test-logo ]]
printf 'OK: complete database and uploads backup, verification, health\n'

for failure in FAKE_PGDUMP_FAIL FAKE_BAD_DUMP FAKE_TAR_FAIL; do
    expect_failure env "$failure=1" bash "$SCRIPT" run
    [[ "$(cat "$BACKUP_DIR/.last-success")" == "$first" ]]
    [[ "$(count_snapshots)" == 1 ]]
    [[ -z "$(find "$BACKUP_DIR" -maxdepth 1 -name '.incomplete-*' -print)" ]]
done
printf 'OK: failed/corrupt dumps and failed archives do not publish or prune\n'

old="$BACKUP_DIR/snapshot-20000101T000000Z-OLD123"
recent="$BACKUP_DIR/snapshot-20000102T000000Z-NEW123"
cp -a "$BACKUP_DIR/$first" "$old"
cp -a "$BACKUP_DIR/$first" "$recent"
printf '%s\n' "$(( $(date +%s) - 15 * 86400 ))" > "$old/completed-at"
printf '%s\n' "$(( $(date +%s) - 13 * 86400 ))" > "$recent/completed-at"
printf 'manual\n' > "$BACKUP_DIR/manual.dump"
mkdir "$BACKUP_DIR/snapshot-not-managed"
expect_failure env FAKE_PGDUMP_FAIL=1 bash "$SCRIPT" run
[[ -d "$old" ]]
bash "$SCRIPT" run
[[ ! -e "$old" && -d "$recent" && -d "$BACKUP_DIR/snapshot-not-managed" && -f "$BACKUP_DIR/manual.dump" ]]
printf 'OK: 14-day retention only removes managed expired snapshots after success\n'

(
    exec 8>"$BACKUP_DIR/.backup.lock"
    flock 8
    expect_failure bash "$SCRIPT" run
)
printf 'OK: parallel backup is blocked\n'

before="$(count_snapshots)"
expect_failure timeout 2 bash "$SCRIPT" schedule
[[ "$(count_snapshots)" == "$before" ]]
latest="$(cat "$BACKUP_DIR/.last-success")"
printf '%s\n' "$(( $(date +%s) - 2 * 86400 ))" > "$BACKUP_DIR/$latest/completed-at"
expect_failure bash "$SCRIPT" healthcheck
expect_failure timeout 2 bash "$SCRIPT" schedule
[[ "$(count_snapshots)" -gt "$before" ]]
bash "$SCRIPT" healthcheck
printf 'OK: restart-safe daily schedule and overdue health detection\n'

latest="$(cat "$BACKUP_DIR/.last-success")"
before="$(count_snapshots)"
printf '%s\n' "$(( $(date +%s) - 2 * 86400 ))" > "$BACKUP_DIR/$latest/completed-at"
expect_failure env FAKE_PGDUMP_FAIL=1 timeout 3 bash "$SCRIPT" schedule > "$scratch/retry.log" 2>&1
[[ "$(grep -c 'retry in' "$scratch/retry.log")" -ge 2 ]]
[[ "$(count_snapshots)" == "$before" && "$(cat "$BACKUP_DIR/.last-success")" == "$latest" ]]
printf 'OK: scheduler retries failed backups without removing existing snapshots\n'

printf 'tampered\n' >> "$BACKUP_DIR/$latest/database.dump"
expect_failure bash "$SCRIPT" verify
expect_failure env BACKUP_RETENTION_DAYS=0 bash "$SCRIPT" run
expect_failure env BACKUP_DIR=/ bash "$SCRIPT" run
printf 'OK: tamper detection and invalid configuration rejection\n'
printf 'All backup tests passed.\n'
