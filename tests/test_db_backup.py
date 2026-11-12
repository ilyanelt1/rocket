from pathlib import Path

import pytest

from scripts.db_backup import restore


def test_restore_requires_explicit_confirmation(tmp_path: Path):
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"not-a-real-dump")

    with pytest.raises(ValueError, match="pass --yes"):
        restore(archive, confirmed=False)


def test_restore_rejects_missing_archive_even_when_confirmed(tmp_path: Path):
    missing = tmp_path / "missing.dump"

    with pytest.raises(FileNotFoundError, match="not found or empty"):
        restore(missing, confirmed=True)
