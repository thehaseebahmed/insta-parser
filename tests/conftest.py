import pytest
from fastapi.testclient import TestClient

from app import main as main_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient wired to an isolated WORK_DIR, with clean job state per test.

    Uses `with TestClient(...)` so the app's lifespan (and, transitively, any
    asyncio.Task spawned by /process) keeps running across the multiple
    requests a single test makes, not just for the duration of one call.
    """
    monkeypatch.setattr(main_module.config, "WORK_DIR", str(tmp_path))
    monkeypatch.setattr(main_module.config, "KEEP_FILES", False)
    main_module._jobs.clear()
    with TestClient(main_module.app) as test_client:
        yield test_client
    main_module._jobs.clear()
