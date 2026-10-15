import pytest


@pytest.fixture(autouse=True)
def _isolated_trials_log(tmp_path, monkeypatch):
    """Harness runs in tests log their trials to a temporary file, never to research/trials.jsonl."""
    monkeypatch.setenv("RESEARCH_TRIALS_LOG", str(tmp_path / "trials.jsonl"))
