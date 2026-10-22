#!/usr/bin/env bash
# Host firewall (ufw) for the VPS. The cloud provider's own firewall is
# wide open, so this is the only filter in front of the machine.
#
#   bash ~/MCP-backbone/deploy/firewall.sh
#
# Open to everyone: 22 (SSH), 80/443 and 443/udp (the Tamara site's Caddy,
# which also proxies https://mcp.my.id). Port 8443 (uvicorn) is open ONLY to
# Caddy's Docker network: the public reaches the app through Caddy on 443,
# so nothing else needs 8443. Everything else incoming is dropped.
#
# Lockout protection: before enabling, it schedules `ufw disable` in 10
# minutes. Test a fresh SSH login from your laptop, then cancel it with the
# command printed at the end. Do nothing and the firewall turns itself off.
# It also rolls back on its own, immediately, if Caddy can no longer reach
# the app on 8443 once the firewall is on.
#
# Known limit: Docker's own iptables rules bypass ufw, so a port that a
# container publishes (like Tamara's 80/443) is reachable whatever ufw says.

set -euo pipefail

CADDY_NETWORK=172.18.0.0/16 # tamara-deploy_default; see deploy/caddy-mcp.caddyfile
CADDY_CONTAINER=tamara-site
APP_FROM_CADDY=https://172.18.0.1:8443/healthz

echo "== Rules"
sudo ufw --force reset >/dev/null # start from exactly these rules, nothing left over
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp
sudo ufw allow from "$CADDY_NETWORK" to any port 8443 proto tcp

echo "== Safety net: firewall turns itself off in 10 minutes unless cancelled"
unit="ufw-safety-$(date +%s)"
sudo systemd-run --unit="$unit" --on-active=10m /usr/sbin/ufw disable >/dev/null

echo "== Enabling"
sudo ufw --force enable
sudo ufw status verbose

echo "== Checking Caddy can still reach the app on 8443"
if ! docker exec "$CADDY_CONTAINER" wget -qO- --no-check-certificate -T 10 "$APP_FROM_CADDY" >/dev/null; then
    echo "FAILED: $CADDY_CONTAINER can't reach $APP_FROM_CADDY. Rolling back." >&2
    sudo ufw disable
    sudo systemctl stop "$unit.timer" || true
    exit 1
fi
echo "   ok"

echo
echo "Now, from your LAPTOP, open a new SSH session to this server."
echo "If it connects, cancel the automatic undo here:"
echo "   sudo systemctl stop $unit.timer"
echo "If it doesn't, wait 10 minutes: the firewall switches itself off."
