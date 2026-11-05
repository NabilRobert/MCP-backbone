#!/usr/bin/env bash
# Nightly dump of the local MCP bookkeeping database, age-encrypted. Keeps
# the last 14 encrypted copies locally and in S3 (off-server). The plaintext
# dump is deleted as soon as it's encrypted, and on any failure.
#
# Deployed on the VPS as ~/bin/backup_bookkeeping.sh (see deploy/RUNBOOK.md), run by cron daily at
# 03:17. Every setting comes from ~/MCP-backbone/.env at run time, the age
# public key included, so rotating the key never needs a code change and a
# redeploy can't bring an old key back. The public key can only encrypt; the
# matching private key is kept off the VPS.
#
# Alerting (optional): set BACKUP_HEALTHCHECK_URL in .env to a dead-man's-
# switch check, e.g. a free healthchecks.io check scheduled daily. The
# script pings <url>/start, then <url> on success or <url>/fail on any
# error, so you're emailed if a backup fails OR silently never runs.
set -euo pipefail
umask 077

BACKUP_DIR="$HOME/backups"
ENV_FILE="$HOME/MCP-backbone/.env"

HC_URL="$(grep '^BACKUP_HEALTHCHECK_URL=' "$ENV_FILE" | cut -d= -f2- || true)"
ping_hc() {
  if [ -n "$HC_URL" ]; then curl -fsS -m 10 --retry 3 "$HC_URL$1" >/dev/null || true; fi
}
# Whatever happens, no plaintext dump (or half-written file) stays on disk.
remove_plaintext() {
  if [ -n "${OUT:-}" ]; then rm -f -- "$OUT" "$OUT.partial" "$OUT.age.partial"; fi
}
trap 'rc=$?; remove_plaintext; if [ "$rc" -ne 0 ]; then ping_hc /fail; fi' EXIT
ping_hc /start

require() {
  local v
  v="$(grep "^$1=" "$ENV_FILE" | cut -d= -f2- || true)"
  if [ -z "$v" ]; then echo "backup: $1 is missing from $ENV_FILE" >&2; exit 1; fi
  printf '%s' "$v"
}

# Validate everything up front, before dumping anything.
DB_URL="$(require APP_DATABASE_URL | sed -E 's#^postgresql\+[a-z]+://#postgresql://#')"
AWS_KEY_ID="$(require AWS_ACCESS_KEY_ID)"
AWS_SECRET="$(require AWS_SECRET_ACCESS_KEY)"
S3_BUCKET="$(require S3_BUCKET)"
S3_REGION="$(require S3_REGION)"
AGE_PUBLIC_KEY="$(require AGE_PUBLIC_KEY)"

# A run killed outright (SIGKILL, power loss) skips the trap, so clear any
# plaintext an earlier run left behind before making a new one.
rm -f -- "$BACKUP_DIR"/jarvis_mcp_bookkeeping-*.dump "$BACKUP_DIR"/jarvis_mcp_bookkeeping-*.partial

OUT="$BACKUP_DIR/jarvis_mcp_bookkeeping-$(date +%Y%m%d-%H%M%S).dump"
pg_dump "$DB_URL" --format=custom --file="$OUT.partial"
mv "$OUT.partial" "$OUT"

# Encrypt before it leaves the machine, then delete the plaintext at once.
age -r "$AGE_PUBLIC_KEY" -o "$OUT.age.partial" "$OUT"
mv "$OUT.age.partial" "$OUT.age"
rm -f -- "$OUT"

# Credentials via env vars, not command-line args, so they don't show in ps.
export RCLONE_S3_PROVIDER=AWS
export RCLONE_S3_ACCESS_KEY_ID="$AWS_KEY_ID"
export RCLONE_S3_SECRET_ACCESS_KEY="$AWS_SECRET"
export RCLONE_S3_REGION="$S3_REGION"
# The IAM policy deliberately has no CreateBucket permission.
export RCLONE_S3_NO_CHECK_BUCKET=true
REMOTE=":s3:$S3_BUCKET/bookkeeping"

# Upload first, so a failed upload stops the script before any pruning.
rclone copy "$OUT.age" "$REMOTE/"

ls -1t "$BACKUP_DIR"/jarvis_mcp_bookkeeping-*.dump.age | tail -n +15 | xargs -r rm --

# Prune remote copies, but only files this script created.
rclone lsf "$REMOTE" --files-only --include 'jarvis_mcp_bookkeeping-*.dump.age' \
  | sort -r | tail -n +15 \
  | xargs -r -I{} rclone deletefile "$REMOTE/{}"

ping_hc ""
