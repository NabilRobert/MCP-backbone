#!/usr/bin/env bash
# Nightly dump of the local MCP bookkeeping database. Keeps the last 14
# locally, and an age-encrypted copy of the last 14 in S3 (off-server).
#
# Deployed on the VPS as ~/bin/backup_bookkeeping.sh, run by cron daily at
# 03:17. Every secret comes from ~/MCP-backbone/.env at run time; nothing
# sensitive lives in this file. The age public key below can only encrypt;
# the matching private key is kept off the VPS.
set -euo pipefail
umask 077

BACKUP_DIR="$HOME/backups"
ENV_FILE="$HOME/MCP-backbone/.env"

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

OUT="$BACKUP_DIR/jarvis_mcp_bookkeeping-$(date +%Y%m%d-%H%M%S).dump"
pg_dump "$DB_URL" --format=custom --file="$OUT.partial"
mv "$OUT.partial" "$OUT"

# Encrypt before it leaves the machine. The public key can only encrypt;
# the private key lives off this VPS.
AGE_PUBLIC_KEY="age1upeqgp4yylfausc24hsht4d0xzyay9pu0we64saqv4mx6azgyqgqknae43"
age -r "$AGE_PUBLIC_KEY" -o "$OUT.age.partial" "$OUT"
mv "$OUT.age.partial" "$OUT.age"

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

ls -1t "$BACKUP_DIR"/jarvis_mcp_bookkeeping-*.dump     | tail -n +15 | xargs -r rm --
ls -1t "$BACKUP_DIR"/jarvis_mcp_bookkeeping-*.dump.age | tail -n +15 | xargs -r rm --

# Prune remote copies, but only files this script created.
rclone lsf "$REMOTE" --files-only --include 'jarvis_mcp_bookkeeping-*.dump.age' \
  | sort -r | tail -n +15 \
  | xargs -r -I{} rclone deletefile "$REMOTE/{}"
