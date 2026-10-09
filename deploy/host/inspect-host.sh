#!/bin/sh
# Read-only preflight. Never reads WireGuard keys, application secrets or container env.
set -u
hostname
id
uname -m
lsb_release -ds
systemctl is-active docker wg-quick@beresta
systemctl is-enabled docker wg-quick@beresta
ip -4 -o addr show dev beresta
wg show beresta latest-handshakes
wg show beresta endpoints
ufw status verbose
iptables -S DOCKER-USER
iptables -S FORWARD
ss -lntup
docker version --format '{{.Server.Version}}'
docker compose version
docker ps -a --format '{{.Names}} {{.Status}} {{.Ports}}'
docker system df
df -h / /var/lib/docker
free -m
python3 - <<'PY'
import json
from pathlib import Path
path = Path('/etc/docker/daemon.json')
config = json.loads(path.read_text()) if path.exists() else {}
print(json.dumps({k: config.get(k, 'default') for k in ('firewall-backend', 'iptables', 'log-driver', 'log-opts')}))
print('Existing deployment:', Path('/opt/beresta').exists())
PY
