#!/bin/sh
# Preparation only. Never starts the Telegram sender.
set -eu
test "$(id -u)" = 0
ip -4 -o addr show dev beresta | grep -q ' inet 10\.77\.0\.2/'
install -m 755 deploy/host/bot-compose /usr/local/sbin/beresta-bot
install -d -m 700 secrets
python3 - <<'PY'
import os
from pathlib import Path
env = Path('.env.bot')
if not env.exists():
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as target:
        target.write('CORE_VPN_IP=10.77.0.1\nBERESTA_WEB_URL=https://beresta.invalid\n'
                     'BERESTA_BOT_NETWORK_ENABLED=false\n')
PY
# Build does not need Telegram credentials. Create follows after owner input.
beresta-bot build telegram
