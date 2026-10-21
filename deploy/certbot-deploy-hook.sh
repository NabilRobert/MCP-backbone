#!/bin/sh
# Installed on the VPS as /etc/letsencrypt/renewal-hooks/deploy/mcp-backbone.sh
# (root-owned, mode 755). certbot runs it after each successful renewal.
#
# Copies the renewed cert to where the mcp-backbone service (running as
# ubuntu) can read it, then restarts the service to pick it up.
set -e
install -d -o root -g ubuntu -m 750 /etc/mcp-backbone/tls
install -o root -g ubuntu -m 640 /etc/letsencrypt/live/mcp.my.id/fullchain.pem /etc/mcp-backbone/tls/fullchain.pem
install -o root -g ubuntu -m 640 /etc/letsencrypt/live/mcp.my.id/privkey.pem /etc/mcp-backbone/tls/privkey.pem
systemctl restart mcp-backbone
