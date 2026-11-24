from __future__ import annotations

import pytest

from cvboost import connectors
from tests.conftest import NOW


@pytest.fixture(autouse=True)
def _sync_sources(store):
    """jobs.source is a foreign key: the e-mail sub-sources must exist, as in the worker."""
    store.sync_sources(connectors.REGISTRY, NOW)
