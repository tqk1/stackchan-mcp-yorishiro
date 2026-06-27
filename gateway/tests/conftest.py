"""Pytest configuration."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_activity_log(monkeypatch, tmp_path):
    """Redirect the activity-feed JSONL to a per-test temp path.

    The heartbeat / proactive / switchbot / presence call sites append to
    :mod:`stackchan_mcp.activity_log`, whose default path is the real
    ``~/.stackchan/activity_log.jsonl``. Without this isolation, any test
    that exercises those paths would pollute the user's live state dir.
    Tests that need a specific path / disabled state override this env.
    """
    monkeypatch.setenv("STACKCHAN_ACTIVITY_LOG", str(tmp_path / "activity_log.jsonl"))


# Use asyncio mode for all async tests
def pytest_collection_modifyitems(config, items):
    """Auto-mark all async tests."""
    for item in items:
        if item.get_closest_marker("asyncio") is None:
            if asyncio_test(item):
                item.add_marker(pytest.mark.asyncio)


def asyncio_test(item):
    """Check if test is async."""
    return hasattr(item, "function") and hasattr(item.function, "__wrapped__")
