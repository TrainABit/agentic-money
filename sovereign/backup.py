"""Cold backups of one engine data directory, and verification of them.

:func:`create_backup` snapshots the SQLite database through the online backup
API — safe while the engine is running and writing — then copies the document
trees that carry operator value (playbooks, invoices, artifacts) and,
optionally, the encrypted secrets bundle. The Fernet master key is never
copied: a backup holding both ``secrets.enc`` and ``master.key`` would be a
plaintext-equivalent wallet, so the key must travel separately.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

from sovereign.config import EngineConfig
from sovereign.fileio import atomic_write_text

__all__ = ["create_backup", "restore_drill", "verify_backup", "MANIFEST_NAME"]

MANIFEST_NAME = "manifest.json"
DB_NAME = "sovereign.db"
_BACKUP_DIRS = ("playbooks", "invoices", "artifacts")
MASTER_KEY_WARNING = (
    "master.key is deliberately NOT in this backup. secrets.enc cannot be "
    "decrypted without it: keep the master key stored separately (offline "
    "medium or password manager), or this backup cannot restore credentials."
)


def _engine_version() -> str:
    try:
        return metadata.version("sovereign")
    except metadata.PackageNotFoundError:
        return "0.1.0"


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _snapshot_db(db_path: Path, target: Path) -> None:
    """Copy the live database with sqlite3's online backup API.

    A dedicated short-lived connection keeps this callable from any process
    (CLI, daemon, tests) while the engine holds its own connection open; the
    backup API copies a consistent snapshot instead of a torn file.
    """
    source = sqlite3.connect(db_path, timeout=30.0)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
            # The copy inherits the live database's WAL header, and merely
            # opening a WAL-flagged file (even read-only) materializes -wal and
            # -shm sidecars beside it. A cold backup must stay one
            # self-contained file, so flip the snapshot to a rollback journal.
            destination.execute("PRAGMA journal_mode=DELETE")
        finally:
            destination.close()
    finally:
        source.close()


def create_backup(
    config: EngineConfig, out_dir: Path, *, include_secrets: bool = True
) -> dict[str, Any]:
    """Write a verifiable backup of the engine's data directory into ``out_dir``.

    ``out_dir`` must be absent or an empty directory (never clobbers), and must
    live outside the data directory so the copy cannot recurse into itself.
    Returns the manifest that was also written to ``manifest.json``.
    """
    paths = config.paths()
    out = Path(out_dir)
    resolved_out = out.resolve()
    resolved_root = paths.root.resolve()
    if resolved_out == resolved_root or resolved_root in resolved_out.parents:
        raise ValueError(f"backup target {out} must live outside the data directory")
    if out.exists():
        if not out.is_dir():
            raise ValueError(f"backup target {out} exists and is not a directory")
        if any(out.iterdir()):
            raise ValueError(f"backup target {out} is not empty; refusing to clobber it")
    else:
        out.mkdir(parents=True)
    if not paths.db.exists():
        raise ValueError(f"no database at {paths.db}; nothing to back up")

    _snapshot_db(paths.db, out / DB_NAME)
    for name in _BACKUP_DIRS:
        source = paths.root / name
        if source.is_dir():
            shutil.copytree(source, out / name)
    if include_secrets and paths.secrets.exists():
        shutil.copy2(paths.secrets, out / paths.secrets.name)

    # The master key must never ride along with the ciphertext it opens. The
    # code above cannot copy it, but a stray file of that name inside a copied
    # tree would be just as dangerous, so scrub and refuse rather than keep it.
    leaked = sorted(p for p in out.rglob(paths.master_key.name) if p.is_file())
    for path in leaked:
        path.unlink()
    if leaked:
        raise RuntimeError(
            "master.key must never enter a backup; offending copies were removed "
            "and the backup was aborted"
        )

    files: dict[str, dict[str, Any]] = {}
    for path in sorted(out.rglob("*")):
        if path.is_file():
            digest, size = _sha256(path)
            files[path.relative_to(out).as_posix()] = {"sha256": digest, "bytes": size}

    manifest: dict[str, Any] = {
        "created_ts": datetime.now(timezone.utc).isoformat(),
        "engine_version": _engine_version(),
        "files": files,
        "include_secrets": bool(include_secrets),
        "master_key_excluded": True,
        "warning": MASTER_KEY_WARNING,
    }
    atomic_write_text(out / MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def verify_backup(out_dir: Path) -> dict[str, Any]:
    """Check a backup directory against its manifest and probe the copied DB.

    Every manifest entry is re-hashed; missing files, hash or size mismatches,
    and data files absent from the manifest all fail verification, as does a
    database that does not pass ``PRAGMA quick_check`` when opened read-only.
    """
    out = Path(out_dir)
    manifest_path = out / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError(f"{manifest_path} is missing; not a backup directory")
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{manifest_path} is not valid JSON: {exc}") from exc
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError(f"{manifest_path} has no files mapping")

    errors: list[str] = []
    checked = 0
    for rel, expected in sorted(files.items()):
        path = out / rel
        if not path.is_file():
            errors.append(f"missing: {rel}")
            continue
        digest, size = _sha256(path)
        checked += 1
        if digest != expected.get("sha256"):
            errors.append(f"sha256 mismatch: {rel}")
        elif size != expected.get("bytes"):
            errors.append(f"size mismatch: {rel}")

    on_disk = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    for rel in sorted(on_disk - set(files) - {MANIFEST_NAME}):
        errors.append(f"unexpected file: {rel}")

    db_path = out / DB_NAME
    if db_path.is_file():
        try:
            connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
            try:
                row = connection.execute("PRAGMA quick_check").fetchone()
                quick_check = str(row[0]) if row else "unknown"
            finally:
                connection.close()
        except sqlite3.Error as exc:
            quick_check = f"error: {exc}"
        if quick_check != "ok":
            errors.append(f"quick_check: {quick_check}")
    else:
        quick_check = "missing"
        if f"missing: {DB_NAME}" not in errors:
            errors.append(f"missing: {DB_NAME}")

    return {
        "ok": not errors,
        "files_checked": checked,
        "quick_check": quick_check,
        "errors": errors,
    }


def restore_drill(config: EngineConfig, work_dir: Path) -> dict[str, Any]:
    """Create a backup, verify it, and probe the snapshot without restoring.

    Writes into ``work_dir/backup``. The live data directory is never
    mutated. A drill is the operator's proof that last night's backup
    procedure still produces a readable, schema-current database.
    """
    backup_dir = Path(work_dir) / "backup"
    manifest = create_backup(config, backup_dir)
    verify = verify_backup(backup_dir)
    probe: dict[str, Any] = {}
    db_path = backup_dir / DB_NAME
    if verify["ok"] and db_path.is_file():
        connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            jobs = (
                int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
                if "jobs" in tables
                else 0
            )
            ledger_rows = (
                int(connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0])
                if "ledger" in tables
                else 0
            )
            probe = {
                "schema_version": version,
                "tables": sorted(tables),
                "jobs": jobs,
                "ledger_rows": ledger_rows,
            }
        finally:
            connection.close()
    return {
        "ok": bool(verify["ok"]),
        "backup": str(backup_dir),
        "manifest": {
            "created_ts": manifest.get("created_ts"),
            "engine_version": manifest.get("engine_version"),
            "include_secrets": manifest.get("include_secrets"),
            "master_key_excluded": manifest.get("master_key_excluded"),
            "file_count": len(manifest.get("files") or {}),
        },
        "verify": verify,
        "probe": probe,
    }
