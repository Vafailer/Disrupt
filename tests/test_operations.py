"""Backup security and recovery guardrails; real recovery is exercised in Docker CI."""
import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("beresta_ops", Path(__file__).parents[1] / "deploy/ops/beresta_ops.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


@pytest.mark.parametrize("name,kind", [("../escape", tarfile.REGTYPE), ("/escape", tarfile.REGTYPE),
    ("link", tarfile.SYMTYPE), ("link", tarfile.LNKTYPE), ("pipe", tarfile.FIFOTYPE)])
def test_unsafe_archives_rejected(tmp_path, name, kind):
    path = tmp_path / "bad.tar"
    with tarfile.open(path, "w") as archive:
        item = tarfile.TarInfo(name)
        item.type = kind
        item.linkname = "/etc/passwd"
        archive.addfile(item)
    with pytest.raises(ops.Failure, match="unsafe_archive"):
        ops.validate_tar(path)


def test_duplicate_normalized_path_rejected(tmp_path):
    path = tmp_path / "bad.tar"
    with tarfile.open(path, "w") as archive:
        for name in ("./original", "original"):
            archive.addfile(tarfile.TarInfo(name))
    with pytest.raises(ops.Failure, match="duplicate"):
        ops.validate_tar(path)


def test_private_directory_required(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(ops.Failure, match="private"):
        ops.private_dir(tmp_path)


def test_restore_refuses_existing_target_before_decrypt(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(ops, "run", run)
    with pytest.raises(ops.Failure, match="already_exists"):
        ops.restore(SimpleNamespace(project="beresta-restore-check"))
    assert calls == [["docker", "volume", "inspect", "beresta-restore-check_restore-db"]]


@pytest.mark.parametrize("project", ["production", "beresta", "beresta-restore-", "beresta-restore-../prod"])
def test_restore_refuses_production_names(project):
    with pytest.raises(ops.Failure, match="new_beresta"):
        ops.restore(SimpleNamespace(project=project))


def test_backup_restarts_original_writers_after_dump_failure(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    args = SimpleNamespace(maintenance=True, project="test", compose=["compose.yaml"],
                           env_file=None, telegram=False, destination=tmp_path, recipient="public")
    snapshots = iter([{"db": {"State": "running"}, "api": {"State": "running"}, "worker": {"State": "exited"}}, {}])
    monkeypatch.setattr(ops, "states", lambda _: next(snapshots))
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if "pg_dump" in command:
            raise ops.Failure("dump_failed")
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(ops, "run", run)
    with pytest.raises(ops.Failure, match="dump_failed"):
        ops.backup(args)
    assert calls[-1][-2:] == ["start", "api"]
    assert not (tmp_path / "latest.json").exists()
    assert not list(tmp_path.glob(".working-*"))


def test_manifest_hash_mismatch_rejected(tmp_path):
    bundle = tmp_path / "bundle.tar"
    manifest = {"version": 1, "telegram": False, "files": {"database.dump": "wrong", "audio.tar": "wrong"}}
    with tarfile.open(bundle, "w") as archive:
        for name, data in {"manifest.json": json.dumps(manifest).encode(), "database.dump": b"data", "audio.tar": b"tar"}.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    output = tmp_path / "out"
    output.mkdir()
    with pytest.raises(ops.Failure, match="checksum"):
        ops.unpack_bundle(bundle, output)


def test_backup_cannot_silently_omit_telegram_state(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    args = SimpleNamespace(maintenance=True, project="test", compose=["compose.yaml"],
                           env_file=None, telegram=False, destination=tmp_path, recipient="public")
    monkeypatch.setattr(ops, "states", lambda _: {"db": {"State": "running"}, "telegram": {"State": "exited"}})
    with pytest.raises(ops.Failure, match="telegram_backup_flag_required"):
        ops.backup(args)


def test_maintenance_must_be_explicit():
    with pytest.raises(ops.Failure, match="maintenance"):
        ops.backup(SimpleNamespace(maintenance=False))


def test_monitor_reports_stalled_delivery_and_old_backup(tmp_path, monkeypatch, capsys):
    args = SimpleNamespace(project="test", compose=["compose.yaml"], env_file=None,
                           telegram=True, backup_dir=tmp_path)
    monkeypatch.setattr(ops, "states", lambda _: {name: {"State": "running"} for name in ("db", "api", "worker", "scheduler", "telegram")})
    monkeypatch.setattr(ops.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * 1024**3, used=1, total=20 * 1024**3))
    def run(command, **kwargs):
        output = json.dumps({"unknown_deliveries": 1, "late_deliveries": 2}).encode() if "psql" in command else b""
        return subprocess.CompletedProcess(command, 0, output)
    monkeypatch.setattr(ops, "run", run)
    assert ops.monitor(args) == 1
    data = json.loads(capsys.readouterr().out)
    assert set(data["alerts"]) == {"unknown_deliveries", "late_deliveries", "backup_missing_or_old"}


def test_operator_locks_exclude_overlapping_backups(tmp_path):
    with ops.lock(tmp_path / "lock"):
        with pytest.raises(ops.Failure, match="already_running"):
            with ops.lock(tmp_path / "lock"):
                pytest.fail("second lock acquired")
