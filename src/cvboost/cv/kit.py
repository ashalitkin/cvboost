"""The application kit on disk: `{kits_dir}/{canonical_id}/` with private files.

Path safety: the directory name is built from an int only, must resolve inside `kits_dir`, and
a symlink in its place (or at `prev/`) is refused. Files are written to a temp name opened with
O_EXCL|O_NOFOLLOW and moved into place with os.replace. Directories are 0700, files 0600.
The previous generation is kept in `prev/` (one generation).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from cvboost.locks import LockHandle, try_lock

RESUME_MD = "resume.md"
RESUME_PDF = "resume.pdf"
RECRUITER_NOTE = "recruiter_note.md"
FIT_NOTES = "fit_notes.md"
CLAIMS_JSON = "claims.json"
COVER_MD = "cover_letter.md"
COVER_PDF = "cover_letter.pdf"
COVER_FILES = (COVER_MD, COVER_PDF)
KIT_FILES = (RESUME_MD, RESUME_PDF, RECRUITER_NOTE, FIT_NOTES, CLAIMS_JSON, *COVER_FILES)


class KitError(ValueError):
    """An unsafe path or a refused write."""


class KitBusy(RuntimeError):
    """Somebody (the panel or a CLI run) holds this kit's lock."""


def _ensure_dir(path: Path) -> None:
    if path.is_symlink():
        raise KitError(f"{path.name} is a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def kit_dir(kits_dir: Path, canonical_id: int) -> Path:
    """The kit directory for a group (created, 0700). Refuses anything outside `kits_dir`."""
    cid = int(canonical_id)
    if cid <= 0:
        raise KitError("bad canonical id")
    _ensure_dir(kits_dir)
    d = kits_dir / str(cid)
    _ensure_dir(d)
    if not d.resolve().is_relative_to(kits_dir.resolve()):
        raise KitError("kit directory escapes kits_dir")
    return d


def kit_lock(
    kits_dir: Path,
    canonical_id: int,
    *,
    wait_s: float = 0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> LockHandle:
    """Exclusive lock on one kit. Waits up to `wait_s`, then raises KitBusy."""
    _ensure_dir(kits_dir)
    path = kits_dir / f"{int(canonical_id)}.lock"
    deadline = clock() + wait_s
    while True:
        handle = try_lock(path)
        if handle is not None:
            return handle
        if clock() >= deadline:
            raise KitBusy(f"kit {canonical_id} is busy")
        sleep(0.5)


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def write_kit(kits_dir: Path, canonical_id: int, files: dict[str, bytes | str]) -> Path:
    """Replace the kit's files (previous ones go to `prev/`). The caller holds `kit_lock`."""
    unknown = set(files) - set(KIT_FILES)
    if unknown:
        raise KitError(f"unknown kit file {sorted(unknown)[0]!r}")
    d = kit_dir(kits_dir, canonical_id)
    prev = d / "prev"
    # a write that carries no cover letter leaves the existing one alone
    rotated = (
        KIT_FILES
        if any(n in files for n in COVER_FILES)
        else tuple(n for n in KIT_FILES if n not in COVER_FILES)
    )
    existing = [n for n in rotated if (d / n).is_file() and not (d / n).is_symlink()]
    if existing:
        _ensure_dir(prev)
        for name in rotated:  # one generation: nothing of the one before may linger
            (prev / name).unlink(missing_ok=True)
        for name in existing:
            os.replace(d / name, prev / name)
    for name, data in files.items():
        _write_private(d / name, data.encode("utf-8") if isinstance(data, str) else data)
    return d


def write_one(kits_dir: Path, canonical_id: int, name: str, data: bytes | str) -> Path:
    """Overwrite a single file (the owner's edit), without rotating `prev/`."""
    if name not in KIT_FILES:
        raise KitError(f"unknown kit file {name!r}")
    d = kit_dir(kits_dir, canonical_id)
    _write_private(d / name, data.encode("utf-8") if isinstance(data, str) else data)
    return d / name


def retire_pdf(kits_dir: Path, canonical_id: int, name: str = RESUME_PDF) -> None:
    """Move a PDF that no longer matches its markdown to `prev/`: it is no longer downloadable."""
    if name not in (RESUME_PDF, COVER_PDF):
        raise KitError(f"not a pdf of the kit: {name!r}")
    d = kit_dir(kits_dir, canonical_id)
    pdf = d / name
    if pdf.is_file() or pdf.is_symlink():
        prev = d / "prev"
        _ensure_dir(prev)
        os.replace(pdf, prev / name)
