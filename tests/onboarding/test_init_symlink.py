"""`cvboost init` never writes through a symbolic link and creates .env safely (SEC-22)."""

from __future__ import annotations

import os

import pytest

from cvboost.init_config import run_init


@pytest.fixture
def root(tmp_path):
    (tmp_path / "config.example").mkdir()
    (tmp_path / "config.example" / "a.yaml").write_text("x: 1\n")
    (tmp_path / ".env.example").write_text("A=1\n")
    return tmp_path


def test_creates_config_and_private_env(root):
    code, _ = run_init(root)
    assert code == 0
    assert (root / "config" / "a.yaml").read_text() == "x: 1\n"
    assert (root / ".env").read_text() == "A=1\n"
    assert (os.stat(root / ".env").st_mode & 0o777) == 0o600


def test_dangling_env_symlink_is_refused(root, tmp_path_factory):
    target = tmp_path_factory.mktemp("elsewhere") / "stolen"
    (root / ".env").symlink_to(target)
    code, lines = run_init(root)
    assert code == 2 and "symbolic link" in lines[0]
    assert not target.exists() and not (root / "config").exists()


def test_env_symlink_to_existing_file_is_refused(root, tmp_path_factory):
    target = tmp_path_factory.mktemp("elsewhere") / "real"
    target.write_text("keep")
    (root / ".env").symlink_to(target)
    assert run_init(root)[0] == 2
    assert target.read_text() == "keep"


def test_config_symlink_is_refused_even_dangling(root, tmp_path_factory):
    target = tmp_path_factory.mktemp("elsewhere") / "cfg"
    (root / "config").symlink_to(target)
    code, _ = run_init(root)
    assert code == 2 and not target.exists()


def test_config_symlink_to_empty_dir_is_refused(root, tmp_path_factory):
    target = tmp_path_factory.mktemp("elsewhere")
    (root / "config").symlink_to(target)
    assert run_init(root)[0] == 2
    assert list(target.iterdir()) == []
