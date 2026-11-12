import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose"]


def compose_exec(*args, input_data=None, capture_output=False):
    return subprocess.run(
        COMPOSE + ["exec", "-T", "db", *args],
        cwd=ROOT,
        input=input_data,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        check=True,
    )


def database_settings():
    database = compose_exec("printenv", "POSTGRES_DB", capture_output=True)
    username = compose_exec("printenv", "POSTGRES_USER", capture_output=True)
    return database.stdout.decode().strip(), username.stdout.decode().strip()


def backup(destination: Path) -> None:
    database, username = database_settings()
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = compose_exec(
        "pg_dump",
        "-U",
        username,
        "-d",
        database,
        "--format=custom",
        "--no-owner",
        "--no-acl",
        capture_output=True,
    )
    if not result.stdout:
        raise RuntimeError("pg_dump returned an empty archive")
    destination.write_bytes(result.stdout)
    print(f"Backup created: {destination} ({len(result.stdout)} bytes)")


def verify(archive: Path) -> None:
    if not archive.is_file() or archive.stat().st_size == 0:
        raise FileNotFoundError(f"Backup archive not found or empty: {archive}")
    _, username = database_settings()
    temporary_db = f"checkstock_verify_{uuid4().hex[:12]}"
    compose_exec("createdb", "-U", username, temporary_db)
    try:
        compose_exec(
            "pg_restore",
            "--no-owner",
            "--no-acl",
            "--exit-on-error",
            "-U",
            username,
            "-d",
            temporary_db,
            input_data=archive.read_bytes(),
        )
        result = compose_exec(
            "psql",
            "-U",
            username,
            "-d",
            temporary_db,
            "-Atqc",
            "SELECT to_regclass('public.products') IS NOT NULL",
            capture_output=True,
        )
        if result.stdout.decode().strip() != "t":
            raise RuntimeError("Restored archive does not contain public.products")
        print(f"Backup verified successfully: {archive}")
    finally:
        compose_exec("dropdb", "--if-exists", "-U", username, temporary_db)


def restore(archive: Path, confirmed: bool) -> None:
    if not confirmed:
        raise ValueError("Restore is destructive; pass --yes to confirm")
    if not archive.is_file() or archive.stat().st_size == 0:
        raise FileNotFoundError(f"Backup archive not found or empty: {archive}")
    database, username = database_settings()
    compose_exec(
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-acl",
        "--exit-on-error",
        "-U",
        username,
        "-d",
        database,
        input_data=archive.read_bytes(),
    )
    print(f"Database restored from: {archive}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Create, verify, and restore CheckStock PostgreSQL backups."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    backup_parser = commands.add_parser(
        "backup", help="Create a custom-format backup"
    )
    backup_parser.add_argument(
        "--output",
        type=Path,
        help="Output archive path (default: backups/checkstock_<UTC timestamp>.dump)",
    )
    verify_parser = commands.add_parser(
        "verify", help="Restore into a temporary DB and verify"
    )
    verify_parser.add_argument("archive", type=Path)
    restore_parser = commands.add_parser(
        "restore", help="Restore into the configured database"
    )
    restore_parser.add_argument("archive", type=Path)
    restore_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm destructive restore into the configured database",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "backup":
            destination = args.output or (
                ROOT
                / "backups"
                / f"checkstock_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.dump"
            )
            backup(destination)
        elif args.command == "verify":
            verify(args.archive.resolve())
        elif args.command == "restore":
            restore(args.archive.resolve(), args.yes)
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        print(f"Database backup operation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
