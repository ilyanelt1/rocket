from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

import scripts.db_backup as db_backup
from scripts.db_backup import restore, verify


def test_restore_requires_explicit_confirmation(tmp_path: Path):
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"not-a-real-dump")

    with pytest.raises(ValueError, match="pass --yes"):
        restore(archive, confirmed=False)


def test_restore_rejects_missing_archive_even_when_confirmed(tmp_path: Path):
    missing = tmp_path / "missing.dump"

    with pytest.raises(FileNotFoundError, match="not found or empty"):
        restore(missing, confirmed=True)


def test_verify_falls_back_to_archive_listing_without_createdb(
    tmp_path: Path, monkeypatch
):
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"custom archive")
    monkeypatch.setattr(
        db_backup, "database_settings", lambda: ("checkstock", "limited")
    )
    calls = []

    def fake_compose_exec(*args, **kwargs):
        calls.append(args)
        if args[0] == "createdb":
            raise subprocess.CalledProcessError(
                1, args, stderr=b"ERROR: permission denied to create database"
            )
        return SimpleNamespace(stdout=b"; 1259 2200 TABLE public products limited\n")

    monkeypatch.setattr(db_backup, "compose_exec", fake_compose_exec)

    verify(archive)

    assert calls[0][0] == "createdb"
    assert calls[1][0:2] == ("pg_restore", "--list")
    assert all(call[0] != "dropdb" for call in calls)


def test_restore_creates_exactly_one_safety_backup(tmp_path: Path, monkeypatch):
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"custom archive")
    safety_backups = []
    monkeypatch.setattr(
        db_backup, "database_settings", lambda: ("checkstock", "user")
    )
    monkeypatch.setattr(
        db_backup, "backup", lambda destination: safety_backups.append(destination)
    )
    monkeypatch.setattr(db_backup, "compose_exec", lambda *args, **kwargs: None)

    restore(archive, confirmed=True)

    assert len(safety_backups) == 1
    assert safety_backups[0].name.startswith("pre_restore_")
