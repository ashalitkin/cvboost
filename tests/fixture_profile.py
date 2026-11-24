"""The fictional profile "Jordan Example" (tests/fixtures/profile): what every general test uses
instead of the live config/profile.yaml, config/prompts and config/resume (which are personal and
not part of the public export). Tests of the owner's real config live in tests/owner."""

from __future__ import annotations

import atexit
import base64
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / "tests" / "fixtures" / "profile"
PROFILE_YAML = PROFILE_DIR / "profile.yaml"
OUTREACH_YAML = PROFILE_DIR / "outreach.yaml"
RESUME_SRC = PROFILE_DIR / "resume"

# 16x16 grey JPEG: the resume renderer needs a photo; a binary file is not committed.
_PHOTO = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERXRTc4"
    "UG1RV19iZ2hnPk1xeXBkeFxlZ2P/2wBDARESEhgVGC8aGi9jQjhCY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2Nj"
    "Y2NjY2NjY2NjY2NjY2NjY2NjY2P/wAARCAAQABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcI"
    "CQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRol"
    "JicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ip"
    "qrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAA"
    "AAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLR"
    "ChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaX"
    "mJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEA"
    "PwBtFFFdIj//2Q=="
)

_DIR: Path | None = None


def resume_dir() -> Path:
    """The fictional resume dir (profile.md, gaps.yaml, resume.css) plus a generated photo.jpg,
    in a temp directory shared by the whole test session."""
    global _DIR
    if _DIR is None:
        _DIR = Path(tempfile.mkdtemp(prefix="cvboost-resume-"))
        atexit.register(shutil.rmtree, _DIR, True)
        for f in RESUME_SRC.iterdir():
            shutil.copy(f, _DIR / f.name)
        (_DIR / "photo.jpg").write_bytes(_PHOTO)
    return _DIR
