#!/usr/bin/env python3
"""Operator CLI. Never loads application settings, model keys or user content into logs."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath


class Failure(Exception):
    pass


def run(args, *, output=None, source=None, timeout=300, check=True):
    try:
        result = subprocess.run(args, stdin=source, stdout=output or subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise Failure("command_unavailable_or_timeout") from None
    if check and result.returncode:
        raise Failure("command_failed")
    return result


def compose(args):
    command = ["docker", "compose", "--project-name", args.project]
    if args.env_file:
        command += ["--env-file", str(Path(args.env_file).resolve())]
    for filename in args.compose:
        command += ["-f", str(Path(filename).resolve())]
    if args.telegram:
        command += ["--profile", "telegram"]
    return command


def states(command):
    data = run(command + ["ps", "--all", "--format", "json"]).stdout.decode()
    # Compose versions emit either a JSON array or newline-delimited objects.
    rows = json.loads(data) if data.strip().startswith("[") else [json.loads(line) for line in data.splitlines() if line]
    return {row["Service"]: row for row in rows if not row.get("Name", "").endswith("-run")}


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def private_dir(path):
    path = Path(path).absolute()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise Failure("directory_must_be_private")
    return path


@contextmanager
def lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure("operation_already_running") from None
        yield
    finally:
        os.close(fd)


def atomic_json(path, data):
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as target:
            json.dump(data, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temp).unlink(missing_ok=True)


def validate_tar(path):
    """Validate before container extraction. No links, special files or traversal."""
    seen = set()
    with tarfile.open(path, "r:") as archive:
        for item in archive:
            name = PurePosixPath(item.name)
            if name.is_absolute() or ".." in name.parts or (not item.isfile() and not item.isdir()):
                raise Failure("unsafe_archive")
            if str(name) in seen:
                raise Failure("duplicate_archive_member")
            seen.add(str(name))
            if item.size < 0:
                raise Failure("unsafe_archive")


class RemoteBot:
    """One remote Compose bot, controlled through a preconfigured Docker SSH context."""
    def __init__(self, context, project):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", context or "") or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", project or ""):
            raise Failure("invalid_remote_target")
        self.command = ["docker", "--context", context]
        endpoint = json.loads(run(["docker", "context", "inspect", context]).stdout)[0]["Endpoints"]["docker"]["Host"]
        if not endpoint.startswith("ssh://"):
            raise Failure("remote_context_requires_ssh")
        ids = run(self.command + ["ps", "-aq", "--filter", "label=com.docker.compose.project=" + project,
                                  "--filter", "label=com.docker.compose.service=telegram",
                                  "--filter", "label=com.docker.compose.oneoff=False"]).stdout.decode().split()
        if len(ids) != 1:
            raise Failure("remote_bot_must_be_single_container")
        self.container = ids[0]
        data = self.inspect()
        mounts = [m for m in data["Mounts"] if m["Destination"] == "/app/data" and m["Type"] == "volume"]
        if len(mounts) != 1:
            raise Failure("remote_bot_requires_state_volume")
        self.volume, self.image = mounts[0]["Name"], data["Image"]
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", self.volume) or not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image):
            raise Failure("invalid_remote_storage")
        if data["State"].get("Restarting") or data["State"].get("Paused"):
            raise Failure("remote_bot_unstable")
        self.was_running = data["State"]["Running"]

    def inspect(self):
        return json.loads(run(self.command + ["inspect", self.container]).stdout)[0]

    def stop(self):
        if self.was_running:
            run(self.command + ["stop", "--time", "30", self.container], timeout=60)
        self.assert_stopped()

    def assert_stopped(self):
        if self.inspect()["State"]["Running"]:
            raise Failure("remote_bot_not_stopped")

    def archive(self, target):
        self.assert_stopped()
        run(self.command + ["run", "--rm", "--network", "none", "--read-only", "--user", "1000:1000",
                            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                            "--mount", "type=volume,src=" + self.volume + ",dst=/state,readonly",
                            "--entrypoint", "tar", self.image, "--hard-dereference", "-C", "/state", "-cf", "-", "."], output=target)
        self.assert_stopped()

    def restart(self):
        if self.was_running:
            run(self.command + ["start", self.container])


def backup(args):
    if not args.maintenance:
        raise Failure("backup_requires_maintenance_flag")
    context, project = getattr(args, "bot_context", None), getattr(args, "bot_project", None)
    if bool(context) != bool(project) or (context and args.telegram):
        raise Failure("choose_local_or_remote_bot")
    destination = private_dir(args.destination)
    with lock(destination / ".backup.lock"):
        remote = RemoteBot(context, project) if context else None
        try:
            if remote:
                remote.stop()
            return _backup(args, remote)
        finally:
            if remote:
                try:
                    remote.restart()
                except Failure:
                    raise Failure("remote_restart_failed_operator_action_required") from None


def _backup(args, remote=None):
    if not args.maintenance:
        raise Failure("backup_requires_maintenance_flag")
    command = compose(args)
    destination = private_dir(args.destination)
    with tempfile.TemporaryDirectory(dir=destination, prefix=".working-") as directory:
        work = Path(directory)
        current = states(command)
        services = [s for s in ("telegram", "scheduler", "worker", "api") if current.get(s, {}).get("State") == "running"]
        if current.get("db", {}).get("State") != "running":
            raise Failure("database_not_running")
        if "telegram" in current and not args.telegram:
            raise Failure("telegram_backup_flag_required")
        if args.telegram and "telegram" not in current:
            raise Failure("telegram_state_not_found")
        # Fail before maintenance if encryption recipient/tool is invalid.
        with (work / "probe").open("wb") as target:
            run(["age", "-r", args.recipient], source=subprocess.DEVNULL, output=target)
        restart_error = False
        try:
            if services:
                run(command + ["stop", "--timeout", "30", *services])
            stopped = states(command)
            if any(stopped.get(s, {}).get("State") == "running" for s in services):
                raise Failure("writers_not_stopped")
            with (work / "database.dump").open("wb") as target:
                run(command + ["exec", "-T", "db", "pg_dump", "-U", "notes", "-d", "notes", "-Fc"], output=target)
            with (work / "audio.tar").open("wb") as target:
                run(command + ["run", "--rm", "--no-deps", "-T", "--entrypoint", "tar", "api",
                               "--hard-dereference", "-C", "/app/data/audio", "-cf", "-", "."], output=target)
            names = ["database.dump", "audio.tar"]
            if remote:
                with (work / "telegram.tar").open("wb") as target:
                    remote.archive(target)
                names.append("telegram.tar")
            elif args.telegram:
                with (work / "telegram.tar").open("wb") as target:
                    run(command + ["run", "--rm", "--no-deps", "-T", "--entrypoint", "tar", "telegram",
                                   "--hard-dereference", "-C", "/app/data", "-cf", "-", "."], output=target)
                names.append("telegram.tar")
            for name in names[1:]:
                validate_tar(work / name)
            metadata = {"version": 1, "created_at": datetime.now(UTC).isoformat(), "project": args.project,
                        "telegram": bool(args.telegram or remote), "files": {name: digest(work / name) for name in names}}
            (work / "manifest.json").write_text(json.dumps(metadata))
            with tarfile.open(work / "bundle.tar", "w") as bundle:
                for name in [*names, "manifest.json"]:
                    bundle.add(work / name, arcname=name)
        finally:
            # Start only services that were running. Never start an intentionally disabled bot.
            if services:
                try:
                    restart_error = bool(run(command + ["start", *services], check=False).returncode)
                except Failure:
                    restart_error = True
        name = datetime.now(UTC).strftime("beresta-%Y%m%dT%H%M%S-") + os.urandom(4).hex() + ".tar.age"
        encrypted = work / name
        run(["age", "-r", args.recipient, "-o", str(encrypted), str(work / "bundle.tar")], timeout=1800)
        os.chmod(encrypted, 0o600)
        with encrypted.open("rb") as source:
            os.fsync(source.fileno())
        checksum = digest(encrypted)
        os.rename(encrypted, destination / name)
        atomic_json(destination / "latest.json", {"file": name, "sha256": checksum, "created_at": metadata["created_at"]})
        print(json.dumps({"backup": name, "sha256": checksum, "writers_restarted": not restart_error}))
        if restart_error:
            raise Failure("backup_saved_but_service_restart_failed")


def unpack_bundle(bundle, directory):
    allowed = {"manifest.json", "database.dump", "audio.tar", "telegram.tar"}
    with tarfile.open(bundle, "r:") as archive:
        seen = set()
        for item in archive:
            if item.name not in allowed or not item.isfile() or item.name in seen:
                raise Failure("unsafe_bundle")
            if item.name == "manifest.json" and item.size > 65536:
                raise Failure("invalid_manifest")
            seen.add(item.name)
            with archive.extractfile(item) as source, (directory / item.name).open("xb") as target:
                shutil.copyfileobj(source, target)
    metadata = json.loads((directory / "manifest.json").read_text())
    expected = {"database.dump", "audio.tar"} | ({"telegram.tar"} if metadata.get("telegram") is True else set())
    if metadata.get("version") != 1 or set(metadata.get("files", {})) != expected or seen != expected | {"manifest.json"}:
        raise Failure("invalid_manifest")
    for name, checksum in metadata["files"].items():
        if digest(directory / name) != checksum:
            raise Failure("backup_checksum_mismatch")
        if name.endswith(".tar"):
            validate_tar(directory / name)
    return metadata


def restore(args):
    if not re.fullmatch(r"beresta-restore-[a-z0-9-]{1,40}", args.project):
        raise Failure("restore_requires_new_beresta_restore_project")
    command = ["docker", "compose", "-p", args.project, "-f", str(Path(__file__).with_name("restore.compose.yaml"))]
    # Fixed compose file, fixed volume names, no external volumes or production services.
    for volume in ("restore-db", "restore-audio", "restore-state"):
        if run(["docker", "volume", "inspect", args.project + "_" + volume], check=False).returncode == 0:
            raise Failure("restore_target_already_exists")
    destination = private_dir(args.work_dir)
    with lock(destination / ".restore.lock"), tempfile.TemporaryDirectory(dir=destination) as temp:
        work = Path(temp)
        # age can read an identity from the SSH stdin pipe without storing it on this host.
        identity = "-" if args.identity == "-" else str(Path(args.identity).resolve())
        run(["age", "-d", "-i", identity, "-o", str(work / "bundle.tar"), str(Path(args.bundle).resolve())], timeout=1800)
        metadata = unpack_bundle(work / "bundle.tar", work)
        run(command + ["up", "-d", "--wait", "db"])
        try:
            with (work / "database.dump").open("rb") as source:
                run(command + ["exec", "-T", "db", "pg_restore", "-U", "notes", "-d", "notes",
                               "--no-owner", "--no-privileges", "--single-transaction", "--exit-on-error"], source=source)
            for name, mount in [("audio.tar", "/audio"), ("telegram.tar", "/state")]:
                if name not in metadata["files"]:
                    continue
                with (work / name).open("rb") as source:
                    run(command + ["run", "--rm", "--no-deps", "-T", "files", "--no-same-owner", "-xf", "-", "-C", mount], source=source)
            run(command + ["run", "--rm", "--no-deps", "-T", "--entrypoint", "sh", "files", "-c",
                           "chown -R 1000:1000 /audio /state && find /audio /state -type d -exec chmod 700 {} + && find /audio /state -type f -exec chmod 600 {} +"])
            # Compare every referenced original with its DB digest, not just tar integrity.
            rows = run(command + ["exec", "-T", "db", "psql", "-XAt", "-U", "notes", "-d", "notes", "-c",
                                  "SELECT coalesce(audio_key,'missing') || ' ' || coalesce(audio_sha256,'missing') FROM captures WHERE input_kind='audio'"]).stdout.decode().splitlines()
            for row in rows:
                parts = row.split(" ")
                if len(parts) != 2:
                    raise Failure("invalid_audio_metadata")
                key, checksum = parts
                if not re.fullmatch(r"[0-9a-f-]{36}\.audio", key) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                    raise Failure("invalid_audio_metadata")
                actual = run(command + ["run", "--rm", "--no-deps", "-T", "--entrypoint", "sha256sum", "files", "/audio/" + key]).stdout.decode().split()[0]
                if actual != checksum:
                    raise Failure("referenced_original_missing_or_changed")
            print(json.dumps({"restored_project": args.project, "audio_verified": len(rows), "applications_started": False}))
        except Exception:
            run(command + ["stop", "db"], check=False)
            raise


MONITOR_SQL = """SELECT json_build_object(
 'old_queued_jobs',(SELECT count(*) FROM jobs WHERE status='queued' AND created_at < extract(epoch from now())-300),
 'expired_jobs',(SELECT count(*) FROM jobs WHERE status='running' AND lease_until < extract(epoch from now())),
 'late_deliveries',(SELECT count(*) FROM outbox WHERE (status='pending' AND created_at < extract(epoch from now())-60)
   OR (status='retryable' AND retry_at < extract(epoch from now())-60)),
 'expired_deliveries',(SELECT count(*) FROM outbox WHERE status IN ('leased','authorized') AND lease_until < extract(epoch from now())),
 'unknown_deliveries',(SELECT count(*) FROM outbox WHERE status='unknown'))"""


def monitor(args):
    command, alerts = compose(args), []
    context, project = getattr(args, "bot_context", None), getattr(args, "bot_project", None)
    if bool(context) != bool(project) or (context and args.telegram):
        raise Failure("choose_local_or_remote_bot")
    if context:
        try:
            if not RemoteBot(context, project).was_running:
                alerts.append("remote_bot_not_running")
        except (Failure, ValueError, KeyError, TypeError):
            alerts.append("remote_bot_unavailable")
    current = states(command)
    required = ["db", "api", "worker", "scheduler"] + (["telegram"] if args.telegram else [])
    for service in required:
        row = current.get(service, {})
        if row.get("State") != "running" or row.get("Health") == "unhealthy":
            alerts.append(service + "_not_healthy")
    probe = "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5)); assert r['status']=='ok'"
    if run(command + ["exec", "-T", "api", "python", "-c", probe], check=False, timeout=15).returncode:
        alerts.append("api_or_database_unavailable")
    counters = json.loads(run(command + ["exec", "-T", "db", "psql", "-XAt", "-U", "notes", "-d", "notes", "-c", MONITOR_SQL]).stdout)
    alerts.extend(name for name, count in counters.items() if count)
    disk = shutil.disk_usage(args.backup_dir)
    if disk.free < 2 * 1024**3 or disk.used / disk.total > 0.85:
        alerts.append("low_disk_space")
    try:
        marker = json.loads((Path(args.backup_dir) / "latest.json").read_text())
        age = time.time() - datetime.fromisoformat(marker["created_at"]).timestamp()
        filename = marker["file"]
        if Path(filename).name != filename or not (Path(args.backup_dir) / filename).is_file() or not 0 <= age <= 26 * 3600:
            alerts.append("backup_missing_or_old")
    except (OSError, ValueError, KeyError):
        alerts.append("backup_missing_or_old")
    print(json.dumps({"ok": not alerts, "alerts": alerts, "counters": counters}))
    return int(bool(alerts))


def main():
    parser = argparse.ArgumentParser(description="Beresta backup, isolated recovery and read-only health checks")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "monitor"):
        sub = commands.add_parser(name)
        sub.add_argument("--project", required=True)
        sub.add_argument("--compose", action="append", required=True)
        sub.add_argument("--env-file")
        sub.add_argument("--telegram", action="store_true")
        sub.add_argument("--bot-context", help="Docker SSH context for the separate bot VPS")
        sub.add_argument("--bot-project", help="Compose project on the bot VPS")
        if name == "backup":
            sub.add_argument("--destination", required=True)
            sub.add_argument("--recipient", required=True)
            sub.add_argument("--maintenance", action="store_true")
        else:
            sub.add_argument("--backup-dir", required=True)
    sub = commands.add_parser("restore")
    sub.add_argument("--project", required=True)
    sub.add_argument("--bundle", required=True)
    sub.add_argument("--identity", required=True)
    sub.add_argument("--work-dir", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        return {"backup": backup, "restore": restore, "monitor": monitor}[args.command](args) or 0
    except (Failure, OSError, ValueError, tarfile.TarError, KeyError, TypeError) as error:
        # Never print subprocess stderr, database errors, archive member names, or secrets.
        print(json.dumps({"ok": False, "error": str(error) if isinstance(error, Failure) else "operations_failed", "action": args.command}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
