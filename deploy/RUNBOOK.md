# Runbook

How to run the deployed server (`https://mcp.my.id`, an Ubuntu VPS shared
with the unrelated Tamara site) and how to rebuild it from nothing. Every
file referenced lives in `deploy/` unless stated otherwise.

## Layout on the server

| Piece | Where |
|---|---|
| App | `~/MCP-backbone` (git clone), `.venv`, `.env` (mode 600, all secrets) |
| Process | systemd unit `mcp-backbone` → uvicorn on 8443 (TLS) |
| Public entry | Tamara's Caddy container proxies `mcp.my.id:443` → `https://172.18.0.1:8443` |
| Bookkeeping DB | local Postgres 16, database `jarvis_mcp_bookkeeping` (loopback only) |
| Demo DB | same Postgres, database `pagila_demo`, read-only login `pagila_ro` |
| TLS cert (8443) | Let's Encrypt via Cloudflare DNS-01, copied by a certbot deploy hook |
| Backups | nightly `pg_dump` → `age` encryption → S3 (`~/bin/backup_bookkeeping.sh`, cron 03:17) |

## Routine operations

**Deploy a change**
```bash
cd ~/MCP-backbone && git pull && uv pip install -q --python .venv/bin/python -e . \
  && .venv/bin/alembic upgrade head && sudo systemctl restart mcp-backbone \
  && systemctl is-active mcp-backbone && curl -s https://mcp.my.id/healthz
```
Run `~/bin/backup_bookkeeping.sh` first when a migration rewrites data.
A restart logs a `CancelledError ... graceful shutdown` traceback from the
*old* process: open MCP streams are cut after 5 seconds. That's expected.

**Health:** `https://mcp.my.id/healthz` returns 200 only when the
bookkeeping database is reachable, and 503 otherwise. Its `version` field
is the running commit (`-dirty` if the checkout has local edits), so after
a deploy it should match `git rev-parse --short=12 HEAD`.

**Invite someone:** `.venv/bin/python -m scripts.create_invite --note "name"`

**A workspace with its own rate limits** (e.g. for benchmark runs):
`.venv/bin/python -m scripts.seed_workspace --name eval --console-username eval --query-rate-limit 2000 --sample-rate-limit 2000`.
Without the flags a workspace keeps the server defaults. To change one later:
`UPDATE workspaces SET query_rate_limit = <n> WHERE name = '<name>';` (NULL = back to the default).

**Revoke access:** a workspace's own console (Keys page: revoke the key, or
log out everywhere), or `scripts/revoke_key.py`.

**Logs:** `journalctl -u mcp-backbone -f` (app), `docker logs tamara-site`
(Caddy), `~/backups/backup.log` (backups).

## Monitoring (set up once, outside the server)

1. **Uptime:** point any free HTTP monitor (UptimeRobot, Better Stack) at
   `https://mcp.my.id/healthz`, alerting on anything but 200.
2. **Backups:** create a daily check on healthchecks.io and put its ping URL
   in `.env` as `BACKUP_HEALTHCHECK_URL`. The backup script pings it on
   success and `/fail` on error, so a failing *or silently missing* backup
   emails you.

## Restore the bookkeeping database

Needs the age **private** key (kept off the server) on the machine doing
the decrypting.
```bash
rclone lsf ":s3:$S3_BUCKET/bookkeeping"       # pick a dump (RCLONE_S3_* env as in the backup script)
rclone copy ":s3:$S3_BUCKET/bookkeeping/<file>.dump.age" .
age -d -i <private-key-file> -o restore.dump <file>.dump.age
createdb restore_check && pg_restore -d restore_check restore.dump   # inspect before replacing anything
```
Restoring needs the same `APP_CONNECTION_ENCRYPTION_KEY` the dump was made
with, or the stored connection URLs can't be decrypted.

## Rebuild from nothing

1. **Base:** Ubuntu with Docker, Postgres 16, Python 3.12, `uv`, `age`, `rclone`,
   `certbot` + `python3-certbot-dns-cloudflare`.
2. **Bookkeeping DB:** create a login and a database owned by it (loopback
   only, password auth).
3. **App:** clone to `~/MCP-backbone`, `uv venv .venv`,
   `uv pip install --python .venv/bin/python -e .` (a uv venv has no
   `pip`), copy `.env.example` → `.env` (mode 600)
   and fill it in. Restore `APP_CONNECTION_ENCRYPTION_KEY` from its
   off-server copy if restoring data. Then `.venv/bin/alembic upgrade head`.
4. **TLS for 8443:** `certbot certonly --dns-cloudflare
   --dns-cloudflare-credentials <token file, mode 600> -d mcp.my.id`, install
   `certbot-deploy-hook.sh` as
   `/etc/letsencrypt/renewal-hooks/deploy/mcp-backbone.sh`, and run it once.
5. **Service:** install `mcp-backbone.service` (header has the commands).
6. **Public entry:** add `caddy-mcp.caddyfile` to the Tamara Caddyfile (its
   header has the validate-then-reload steps).
7. **Demo database:** `bash deploy/setup_demo_database.sh`.
8. **Backups:** copy `backup_bookkeeping.sh` to `~/bin/`, add the AWS/S3
   values to `.env`, `crontab deploy/crontab` (merge with any existing
   lines), run it once by hand.
9. **Firewall:** `bash deploy/firewall.sh`, then test SSH from outside and
   cancel its safety timer.
10. **Check:** `/healthz` is 200 from outside, a signup works, and a
    `list_tables` through the plugin answers.
