#!/usr/bin/env bash
# Host firewall (ufw) rules for MCP-backbone on the shared VPS. The cloud
# provider's own firewall is wide open, so ufw is the only filter in front
# of the machine.
#
#   bash ~/MCP-backbone/deploy/firewall.sh
#
# It adds only the rules this project needs, tagged with the comment
# "mcp-backbone", and touches nothing else: other projects on the server
# keep their own rules. (It used to run `ufw --force reset`, which wiped
# them.) A shared port another project already allows is left exactly as
# it is, and running the script again changes nothing.
#   - 22/tcp (SSH), 80/tcp, 443/tcp and 443/udp (the Caddy container that
#     proxies https://mcp.my.id), open to everyone;
#   - 8443/tcp (uvicorn) only from Caddy's Docker network.
# To remove this project's own rule later:
#   sudo ufw delete allow from 172.18.0.0/16 to any port 8443 proto tcp
# (22/80/443 are shared with the other sites, so leave those.)
#
# If ufw isn't on yet, it also sets the defaults (deny incoming, allow
# outgoing) and turns it on, with lockout protection: it first schedules
# `ufw disable` in 10 minutes. Test a fresh SSH login from your laptop,
# then cancel it with the command printed at the end. If ufw is already on,
# adding allow rules can't lock anyone out, so it leaves ufw as it is.
#
# Known limit: Docker's own iptables rules bypass ufw, so a port that a
# container publishes (like Caddy's 80/443) is reachable whatever ufw says.
#
# Needs DEPLOY_CADDY_CONTAINER in ~/MCP-backbone/.env: the name of that
# container, read here so it isn't in a public repo.

set -euo pipefail

ENV_FILE="$HOME/MCP-backbone/.env"
CADDY_NETWORK=172.18.0.0/16 # Caddy's Docker network; see deploy/caddy-mcp.caddyfile
APP_FROM_CADDY=https://172.18.0.1:8443/healthz
TAG=mcp-backbone

# Checked before any rule changes: it needs the container's name to prove
# the app is still reachable afterwards.
CADDY_CONTAINER="$(grep '^DEPLOY_CADDY_CONTAINER=' "$ENV_FILE" | cut -d= -f2- || true)"
if [ -z "$CADDY_CONTAINER" ]; then
    echo "DEPLOY_CADDY_CONTAINER is missing: add Caddy's container name to $ENV_FILE" >&2
    exit 1
fi

was_active=false
if sudo ufw status | grep -q '^Status: active'; then was_active=true; fi

# Opens a shared port to everyone, unless ufw already allows it: adding
# the same rule again would rewrite the other project's comment on it.
open_shared_port() {
    if sudo ufw status | grep -qE "^$1[[:space:]]+ALLOW[[:space:]]+Anywhere"; then
        echo "   $1 is already open: left as it is"
    else
        sudo ufw allow "$1" comment "$TAG"
    fi
}

echo "== Rules (only this project's)"
for port in 22/tcp 80/tcp 443/tcp 443/udp; do open_shared_port "$port"; done
sudo ufw allow from "$CADDY_NETWORK" to any port 8443 proto tcp comment "$TAG"

unit=""
if [ "$was_active" = false ]; then
    sudo ufw default deny incoming
    sudo ufw default allow outgoing
    echo "== Safety net: firewall turns itself off in 10 minutes unless cancelled"
    unit="ufw-safety-$(date +%s)"
    sudo systemd-run --unit="$unit" --on-active=10m /usr/sbin/ufw disable >/dev/null
    echo "== Enabling"
    sudo ufw --force enable
fi
sudo ufw status verbose

echo "== Checking Caddy can still reach the app on 8443"
if ! docker exec "$CADDY_CONTAINER" wget -qO- --no-check-certificate -T 10 "$APP_FROM_CADDY" >/dev/null; then
    echo "FAILED: $CADDY_CONTAINER can't reach $APP_FROM_CADDY." >&2
    if [ -n "$unit" ]; then
        # ufw was off before this run, so turning it off again is the way back.
        echo "Turning ufw back off." >&2
        sudo ufw disable
        sudo systemctl stop "$unit.timer" || true
    fi
    exit 1
fi
echo "   ok"

if [ -n "$unit" ]; then
    echo
    echo "Now, from your LAPTOP, open a new SSH session to this server."
    echo "If it connects, cancel the automatic undo here:"
    echo "   sudo systemctl stop $unit.timer"
    echo "If it doesn't, wait 10 minutes: the firewall switches itself off."
fi
