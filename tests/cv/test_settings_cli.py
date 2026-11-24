from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cvboost import cli
from cvboost.config import Settings
from cvboost.cv import kit as K
from cvboost.llm.config import LLMConfig
from tests.cv.helpers import ROOT, queue_request
from tests.fixture_profile import resume_dir


def test_kits_dir_defaults_next_to_the_db(monkeypatch):
    monkeypatch.delenv("CVBOOST_KITS_DIR", raising=False)
    monkeypatch.setenv("CVBOOST_DB", "/data/cvboost.sqlite")
    assert Settings.from_env().kits_dir == Path("/data/kits")
    monkeypatch.setenv("CVBOOST_KITS_DIR", "/elsewhere/kits")
    assert Settings.from_env().kits_dir == Path("/elsewhere/kits")


def test_panel_url_must_be_http(monkeypatch):
    monkeypatch.setenv("CVBOOST_PANEL_URL", "https://panel.example.com/")
    assert Settings.from_env().panel_url == "https://panel.example.com"
    monkeypatch.setenv("CVBOOST_PANEL_URL", "javascript:alert(1)")
    with pytest.raises(ValueError, match="CVBOOST_PANEL_URL"):
        Settings.from_env()
    monkeypatch.delenv("CVBOOST_PANEL_URL")
    assert Settings.from_env().panel_url is None


def test_shipped_llm_yaml_has_the_cv_section():
    cfg = LLMConfig.model_validate(
        yaml.safe_load((ROOT / "tests" / "fixtures" / "config" / "llm.yaml").read_text())
    )
    assert cfg.cv.enabled  # re-enabled after #12
    assert cfg.cv_alias == "opus" and cfg.cv.max_attempts == 2
    assert cfg.cv.timeout_s == 300 and cfg.cv.max_budget_usd_per_call == 1.5
    assert cfg.daily_call_caps["opus"] >= 10  # CV calls


def test_cv_model_role_must_exist():
    with pytest.raises(ValueError, match="cv.model_role"):
        LLMConfig.model_validate({"cv": {"model_role": "nope"}})


def test_check_profile_command_passes_on_the_fictional_profile(capsys):
    assert cli.main(["cv", "check-profile"]) == 0
    assert "NOT COVERED" not in capsys.readouterr().out


def test_check_profile_fails_on_an_unguarded_gap(tmp_path, capsys, monkeypatch):
    rd = tmp_path / "r"
    rd.mkdir()
    src = resume_dir()
    (rd / "profile.md").write_text(
        (src / "profile.md")
        .read_text()
        .replace("## Applications log", "- Kubernetes: never.\n\n## Applications log")
    )
    (rd / "gaps.yaml").write_text((src / "gaps.yaml").read_text())
    monkeypatch.setenv("CVBOOST_RESUME_DIR", str(rd))
    assert cli.main(["cv", "check-profile"]) == 1
    assert "Kubernetes" in capsys.readouterr().err


def test_cv_run_dry_run_never_calls_claude(store, tmp_path, monkeypatch, capsys):
    queue_request(store)
    monkeypatch.setenv("CVBOOST_DB", str(tmp_path / "test.sqlite"))  # the fixture's database
    monkeypatch.setenv(
        "CVBOOST_LLM_CONFIG", str(ROOT / "tests" / "fixtures" / "config" / "llm.yaml")
    )
    assert cli.main(["cv", "run", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "prompt_version: cv-v1-" in out and "job input" in out
    assert cli.main(["cv", "list"]) == 0
    assert "queued" in capsys.readouterr().out


def test_kit_refuses_bad_ids_and_unknown_files(tmp_path):
    with pytest.raises(K.KitError):
        K.kit_dir(tmp_path / "kits", 0)
    with pytest.raises(K.KitError):
        K.write_kit(tmp_path / "kits", 1, {"../x": "y"})
    with pytest.raises(ValueError):
        K.kit_dir(tmp_path / "kits", "../etc")  # type: ignore[arg-type]


def test_kits_dir_symlink_escape_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "kits"
    link.symlink_to(outside)
    with pytest.raises(K.KitError):
        K.kit_dir(link, 5)
    assert list(outside.iterdir()) == []


def test_kit_lock_is_exclusive(tmp_path):
    h = K.kit_lock(tmp_path / "kits", 3)
    with pytest.raises(K.KitBusy):
        K.kit_lock(tmp_path / "kits", 3)
    h.release()
    K.kit_lock(tmp_path / "kits", 3).release()


def test_kit_wait_gives_up_after_the_timeout(tmp_path):
    h = K.kit_lock(tmp_path / "kits", 4)
    ticks = iter([0.0, 1.0, 5.0, 25.0])
    with pytest.raises(K.KitBusy):
        K.kit_lock(tmp_path / "kits", 4, wait_s=20, sleep=lambda s: None, clock=lambda: next(ticks))
    h.release()


def test_regeneration_clears_the_previous_generation_first(tmp_path):
    kits = tmp_path / "kits"
    K.write_kit(kits, 9, {"resume.md": "one", "resume.pdf": b"%PDF one"})
    K.write_kit(kits, 9, {"resume.md": "two", "resume.pdf": b"%PDF two"})
    K.write_kit(kits, 9, {"resume.md": "three"})  # this time without a PDF
    d = kits / "9"
    assert (d / "prev" / "resume.md").read_text() == "two"
    assert (d / "prev" / "resume.pdf").read_bytes() == b"%PDF two"
    K.write_kit(kits, 9, {"resume.md": "four"})
    assert (d / "prev" / "resume.md").read_text() == "three"
    assert not (d / "prev" / "resume.pdf").exists()  # nothing of the older generation lingers


def test_profile_is_mounted_not_baked_into_the_image():
    ignore = (ROOT / ".dockerignore").read_text().split()
    assert "config/resume" in ignore
    c = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for name in ("app", "worker"):
        svc = c["services"][name]
        assert "./config:/app/config:ro" in svc["volumes"]
        assert svc["environment"]["CVBOOST_RESUME_DIR"] == "/app/config/resume"
