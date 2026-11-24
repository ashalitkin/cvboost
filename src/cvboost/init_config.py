"""`cvboost init`: create your own config/ from the shipped config.example/ (public version).

Never overwrites: if config/ already holds anything, it stops and says so. Also creates a
.env from .env.example when there is none (mode 600) and a plain grey placeholder
config/resume/photo.jpg (the CV layout needs a photo file; replace it with yours).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

EXAMPLE = "config.example"
TARGET = "config"
PHOTO = Path("resume") / "photo.jpg"


def run_init(root: Path = Path(".")) -> tuple[int, list[str]]:
    """(exit code, message lines)."""
    src, dst = root / EXAMPLE, root / TARGET
    if not src.is_dir():
        return 2, [
            f"{EXAMPLE}/ not found here: run this in the CVBoost folder "
            "(the private development checkout keeps its own config/ and needs no init)."
        ]
    env, env_example = root / ".env", root / ".env.example"
    for p in (dst, env):
        if p.is_symlink():  # also a dangling one: never write through it
            return 2, [f"{p} is a symbolic link: remove it first, init will not write through it."]
    if dst.exists() and (not dst.is_dir() or any(dst.iterdir())):
        return 1, [
            f"{TARGET}/ already exists and is not empty: nothing was changed.",
            f"To start over: mv {TARGET} {TARGET}.backup, then run init again.",
        ]
    shutil.copytree(src, dst, dirs_exist_ok=True)
    lines = [f"created {TARGET}/ from {EXAMPLE}/ (the example person is 'Jordan Example')"]
    photo = dst / PHOTO
    if not photo.exists():
        try:
            from PIL import Image

            photo.parent.mkdir(parents=True, exist_ok=True)

            Image.new("RGB", (400, 400), (200, 200, 200)).save(photo)
            lines.append(
                f"created a grey placeholder {TARGET}/{PHOTO.as_posix()} (use your own photo)"
            )
        except ImportError:
            lines.append(
                f"WARNING: put a square JPEG at {TARGET}/{PHOTO.as_posix()} (CV layout needs it)"
            )
    if not env.exists() and env_example.is_file():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(env, flags, 0o600)
        except OSError as exc:
            lines.append(f"WARNING: .env was not created ({type(exc).__name__}); copy it by hand")
        else:
            with os.fdopen(fd, "wb") as fh:
                fh.write(env_example.read_bytes())
            lines.append(
                "created .env from .env.example (edit it yourself; never paste secrets into a chat)"
            )
    lines.append(
        "next: run /setup in Claude Code, or edit config/ by hand, then `uv run cvboost doctor`"
    )
    return 0, lines
