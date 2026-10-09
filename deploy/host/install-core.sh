#!/bin/sh
# Run from the reviewed release checkout after inspecting the host.
set -eu
test "$(id -u)" = 0
test -d /sys/class/net/beresta
ip -4 -o addr show dev beresta | grep -q ' inet 10\.77\.0\.1/'
iptables -S DOCKER-USER >/dev/null
install -d -m 700 /etc/beresta /var/backups/beresta
install -m 755 deploy/host/beresta-firewall /usr/local/sbin/beresta-firewall
install -m 644 deploy/host/beresta-firewall.service /etc/systemd/system/
install -d -m 755 /etc/systemd/system/docker.service.d
install -m 644 deploy/host/docker-beresta.conf /etc/systemd/system/docker.service.d/beresta.conf
install -m 755 deploy/host/core-compose /usr/local/sbin/beresta-core
install -m 755 deploy/host/backup /usr/local/sbin/beresta-backup
install -m 644 deploy/host/beresta-backup.service deploy/host/beresta-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now beresta-firewall.service
# Timer is enabled only after an actual coordinated backup succeeds.
python3 - <<'PY'
import os
import secrets
from pathlib import Path
root = Path('secrets')
root.mkdir(mode=0o700, exist_ok=True)
root.chmod(0o700)
for name in ('postgres_password', 'telegram_service_token', 'analytics_pseudonym_key'):
    path = root / (name + '.txt')
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        with os.fdopen(fd, 'w') as target:
            target.write(secrets.token_hex(32))
        os.chown(path, 1000, 1000)
env = Path('.env.production')
if not env.exists():
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as target:
        target.write('APP_DOMAIN=beresta.invalid\nCORE_VPN_IP=10.77.0.1\n'
                     'NOTES_ALLOW_LIVE_REQUESTS=false\nNOTES_ALLOW_REGISTRATION=false\n'
                     'NOTES_CLOUDRU_MODEL=deepseek-v4-flash\n'
                     'NOTES_CLOUDRU_BASE_URL=https://shared1.multitool.works:4000/v1\n'
                     'NOTES_LIVE_CALL_LIMIT=1\nNOTES_LIVE_USER_CALL_LIMIT=1\n')
PY
