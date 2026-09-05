import importlib

import pytest

from app import config


@pytest.fixture
def set_transcribe_concurrency(monkeypatch):
    """Set TRANSCRIBE_CONCURRENCY and re-run config.py's parsing of it, then
    restore both the env var and the module attribute once the test is done."""

    def _set(raw_value):
        if raw_value is None:
            monkeypatch.delenv("TRANSCRIBE_CONCURRENCY", raising=False)
        else:
            monkeypatch.setenv("TRANSCRIBE_CONCURRENCY", raw_value)
        importlib.reload(config)

    yield _set
    monkeypatch.undo()
    importlib.reload(config)


@pytest.mark.parametrize("raw_value", ["0", "-1", "-5"])
def test_transcribe_concurrency_clamped_to_one(set_transcribe_concurrency, raw_value):
    # A Semaphore(0) or Semaphore(negative) would block every transcription
    # forever (see app/pipeline.py's _transcribe_semaphore) — this is the fix
    # for that.
    set_transcribe_concurrency(raw_value)
    assert config.TRANSCRIBE_CONCURRENCY == 1


def test_transcribe_concurrency_default_is_one(set_transcribe_concurrency):
    set_transcribe_concurrency(None)
    assert config.TRANSCRIBE_CONCURRENCY == 1


def test_transcribe_concurrency_respects_valid_values(set_transcribe_concurrency):
    set_transcribe_concurrency("4")
    assert config.TRANSCRIBE_CONCURRENCY == 4
