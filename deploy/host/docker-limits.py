#!/usr/bin/env python3
"""Preserve daemon settings; bound new container logs and regenerable build cache."""
import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--build-cache", choices=["512MB", "2GB"], required=True)
args = parser.parse_args()
if os.geteuid() != 0:
    raise SystemExit("Run as root")
path = Path("/etc/docker/daemon.json")
path.parent.mkdir(mode=0o755, exist_ok=True)
settings = json.loads(path.read_text()) if path.exists() else {}
backup = path.with_name("daemon.json.beresta-before")
if path.exists() and not backup.exists():
    shutil.copyfile(path, backup)
    backup.chmod(0o600)
settings["log-driver"] = "json-file"
settings["log-opts"] = {"max-size": "5m", "max-file": "2"}
gc = settings.setdefault("builder", {}).setdefault("gc", {})
gc.update(enabled=True, defaultKeepStorage=args.build_cache)
fd, temporary = tempfile.mkstemp(dir=path.parent)
try:
    with os.fdopen(fd, "w") as target:
        json.dump(settings, target, indent=2)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    subprocess.run(["dockerd", "--validate", "--config-file", temporary], check=True)
    os.replace(temporary, path)
finally:
    Path(temporary).unlink(missing_ok=True)
print("Docker limits saved. Restart Docker after active builds finish.")
