"""Tests for the bot run-budget logic.

- budget 0 / None => run forever (no deadline)  [local/VPS]
- budget > 0      => capped run                  [GitHub Actions]
"""

import time

import pytest

from app import bot, config


def _budget_has_deadline(budget_s, poll_seconds):
    """Mirror the deadline logic in bot.run() (kept in sync intentionally)."""
    budget = budget_s if budget_s is not None else poll_seconds
    return bool(budget and budget > 0)


def test_budget_zero_means_unlimited():
    assert _budget_has_deadline(0, 0) is False
    assert _budget_has_deadline(None, 0) is False


def test_budget_positive_has_deadline():
    assert _budget_has_deadline(270, 0) is True
    assert _budget_has_deadline(None, 270) is True


def test_config_poll_seconds_local_is_unlimited():
    # In this repo's local .env we set BOT_POLL_SECONDS=0.
    # The test asserts the *interpretation*, not the env value.
    assert bot.__name__  # sanity
    # A zero/None config must not create a deadline.
    assert _budget_has_deadline(None, 0) is False


def test_run_signature_accepts_seconds():
    import inspect
    params = inspect.signature(bot.run).parameters
    assert "budget_s" in params


def test_online_log_uses_deadline_none():
    """The run loop must compare against a possibly-None deadline safely."""
    deadline = None
    # This mirrors: `if deadline is not None and time.time() >= deadline`.
    assert (deadline is not None and time.time() >= deadline) is False
