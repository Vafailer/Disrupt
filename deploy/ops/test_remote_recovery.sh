#!/usr/bin/env bash
# GitHub disposable runner only. One Docker daemon, two projects, real SSH transport.
set -euo pipefail
if [ "${GITHUB_ACTIONS:-}" != true ]; then
  echo 'This fixture is restricted to disposable GitHub Actions runners.' >&2
  exit 1
fi
work=$(mktemp -d)
trap 'docker context rm -f beresta-ci-ssh >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
sudo service ssh start
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
ssh-keygen -q -t ed25519 -N '' -f "$work/key"
cat "$work/key.pub" >> "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
# Trust the runner's host key read locally, not an unverified network scan.
printf '127.0.0.1 ' > "$work/known_hosts"
sudo cat /etc/ssh/ssh_host_ed25519_key.pub >> "$work/known_hosts"
cat >> "$HOME/.ssh/config" <<EOF
Host beresta-ci-bot
  HostName 127.0.0.1
  User $(id -un)
  IdentityFile $work/key
  UserKnownHostsFile $work/known_hosts
  StrictHostKeyChecking yes
  BatchMode yes
  IdentitiesOnly yes
EOF
chmod 600 "$HOME/.ssh/config"
docker context create beresta-ci-ssh --docker host=ssh://beresta-ci-bot >/dev/null
REMOTE_BOT_CONTEXT=beresta-ci-ssh bash deploy/ops/test_recovery.sh
