"""#70: a profile.yaml with an invalid language switches the LLM steps off, collecting goes on."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from cvboost.config import Settings
from cvboost.llm.prompts import registry
from cvboost.worker import run_worker
from tests.worker.helpers import FakeConnector, make_deps

FIXTURE = Path(__file__).parent.parent / "fixtures" / "profile"


def test_invalid_language_collects_but_skips_llm(tmp_path, monkeypatch, caplog):
    cfg = tmp_path / "cfg"
    shutil.copytree(FIXTURE, cfg)
    with (cfg / "profile.yaml").open("a") as f:
        f.write("explain_language: xx\n")
    monkeypatch.setenv("CVBOOST_PROFILE", str(cfg / "profile.yaml"))
    monkeypatch.setenv("CVBOOST_RESUME_DIR", str(cfg / "resume"))
    registry.reset()
    s = Settings(
        db_path=tmp_path / "data" / "w.sqlite",
        log_level="INFO",
        rules_path=Path(__file__).parent.parent / "fixtures" / "config" / "rules.yaml",
        llm_config_path=tmp_path / "llm.yaml",
        worker_config_path=tmp_path / "worker.yaml",
        profile_path=cfg / "profile.yaml",
        resume_dir=cfg / "resume",
    )
    c = FakeConnector(ids=["1"])
    llm_used: list[str] = []

    class Boom:
        def __getattr__(self, name):
            llm_used.append(name)
            raise AssertionError("the LLM client must not be used")

    def factory(settings, stop):
        return make_deps(settings.data_dir, [c], llm_client=Boom())

    with caplog.at_level(logging.WARNING):
        assert run_worker(s, once=True, deps_factory=factory) == 0
    assert c.fetches == 1 and llm_used == []
    assert not registry.is_loaded()
    assert "explain_language" in (registry.error() or "")
    assert "LLM steps are off" in caplog.text
