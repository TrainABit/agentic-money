from __future__ import annotations

import re
from pathlib import Path

_JOB_ID = re.compile(r"\Ajob_[a-z0-9]{4,64}\Z")


def validate_job_id(value: object) -> str:
    """Return a canonical job id or reject it at a trust boundary."""
    if not isinstance(value, str) or not _JOB_ID.fullmatch(value):
        raise ValueError("invalid job id")
    return value


def safe_child(root: Path, name: object, *, label: str = "path component") -> Path:
    """Resolve one untrusted child while keeping it beneath *root*."""
    if not isinstance(name, str) or not name or name in {".", ".."}:
        raise ValueError(f"invalid {label}")
    if Path(name).is_absolute() or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError(f"invalid {label}")

    resolved_root = Path(root).resolve()
    resolved_child = (resolved_root / name).resolve()
    try:
        resolved_child.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes its root") from exc
    return resolved_child


def job_child(root: Path, job_id: object) -> Path:
    return safe_child(root, validate_job_id(job_id), label="job id")
