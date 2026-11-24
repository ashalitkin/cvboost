from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cvboost.models import Job
from cvboost.storage.db import Store, connect, migrate
from tests.fixture_profile import OUTREACH_YAML, PROFILE_YAML, resume_dir

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def PUBLIC_RESOLVER(host: str) -> list[str]:  # noqa: N802 - used like a constant
    """Fake DNS for tests: every host resolves to one public address."""
    return ["93.184.215.14"]


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytest-socket does not block getaddrinfo; real DNS in tests is a bug."""

    def boom(*args, **kwargs):
        raise RuntimeError("real DNS lookup in a test: pass a fake resolver to HttpClient")

    monkeypatch.setattr("cvboost.http.socket.getaddrinfo", boom)


_PROMPTS: dict = {}


@pytest.fixture(autouse=True)
def _profile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI and Settings.from_env find the fictional profile, resume dir and outreach links, not
    the (personal, absent from the public export) config/profile.yaml, config/resume, ..."""
    monkeypatch.setenv("CVBOOST_PROFILE", str(PROFILE_YAML))
    monkeypatch.setenv("CVBOOST_RESUME_DIR", str(resume_dir()))
    monkeypatch.setenv("CVBOOST_OUTREACH", str(OUTREACH_YAML))
    # never the real .env of whoever runs the tests
    monkeypatch.setenv("CVBOOST_ENV_FILE", str(Path(__file__).parent / "no-such.env"))
    cfg = Path(__file__).parent / "fixtures" / "config"  # frozen copies of rules / llm / worker
    monkeypatch.setenv("CVBOOST_RULES", str(cfg / "rules.yaml"))
    monkeypatch.setenv("CVBOOST_LLM_CONFIG", str(cfg / "llm.yaml"))
    monkeypatch.setenv("CVBOOST_WORKER_CONFIG", str(cfg / "worker.yaml"))


@pytest.fixture(autouse=True)
def _prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prompt registry is built from the fictional profile (tests/fixtures/profile): the
    personal config/profile.yaml and config/prompts are not in the public export. The owner's real
    prompts are checked in tests/owner (its conftest loads them)."""
    from cvboost.llm.prompts import registry

    root = Path(__file__).parent.parent
    if "ps" not in _PROMPTS:
        _PROMPTS["ps"] = registry.load_from_paths(
            PROFILE_YAML, root / "tests" / "fixtures" / "config" / "rules.yaml"
        )
    registry.install(_PROMPTS["ps"])
    yield
    registry.reset()


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def store(tmp_path: Path) -> Store:
    conn = connect(tmp_path / "test.sqlite")
    migrate(conn)
    return Store(conn)


def make_job(source_id: str = "1", **kw) -> Job:
    data = dict(
        source="fake",
        source_id=source_id,
        url=f"https://example.com/jobs/{source_id}",
        company="Acme",
        title="Data Engineer",
        description_text="Build pipelines",
        posted_at=NOW - timedelta(days=1),
        fetched_at=NOW,
    )
    data.update(kw)
    return Job(**data)
