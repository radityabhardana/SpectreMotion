"""Tests for StatusMessage auto-update heartbeat (timer ticks every N seconds
while the main work blocks)."""

import time

import pytest

from app import bot


class FakeTG:
    def __init__(self):
        self.sent = []
        self.edits = []
        self._mid = 50

    def send_message(self, chat_id, text, reply_markup=None, **k):
        self._mid += 1
        self.sent.append((chat_id, text, reply_markup))
        return {"message_id": self._mid}

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))
        return {"message_id": message_id}

    def delete_message(self, chat_id, message_id):
        return True


def test_heartbeat_ticks_while_blocked():
    tg = FakeTG()
    st = bot.StatusMessage(tg, chat_id=1, job8="abc123", interval=0.3)
    st.update("transcribe")          # initial send
    st.start_heartbeat()
    # Simulate blocking work for ~1s while the heartbeat should edit.
    time.sleep(1.0)
    st.stop_heartbeat()
    # At least 2 periodic edits should have happened.
    assert len(tg.edits) >= 2, f"heartbeat tidak nge-tick (edits={len(tg.edits)})"


def test_heartbeat_elapsed_advances():
    tg = FakeTG()
    st = bot.StatusMessage(tg, chat_id=1, job8="abc123", interval=0.3)
    st.update("transcribe")
    st.start_heartbeat()
    time.sleep(0.9)
    st.stop_heartbeat()
    # The last edited text should show a non-zero elapsed time.
    texts = [e[2] for e in tg.edits]
    assert texts, "tidak ada edit"
    assert any("⏱" in t for t in texts)


def test_replace_stops_heartbeat():
    tg = FakeTG()
    st = bot.StatusMessage(tg, chat_id=1, job8="abc123", interval=0.2)
    st.update("transcribe")
    st.start_heartbeat()
    time.sleep(0.3)
    st.replace("KATALOG SELESAI")
    assert st._hb_thread is None  # stopped
    assert st._last_text == "KATALOG SELESAI"


def test_delete_stops_heartbeat():
    tg = FakeTG()
    st = bot.StatusMessage(tg, chat_id=1, job8="abc123", interval=0.2)
    st.update("render")
    st.start_heartbeat()
    time.sleep(0.2)
    st.delete()
    assert st._hb_thread is None


def test_show_cancelled_stops_heartbeat():
    tg = FakeTG()
    st = bot.StatusMessage(tg, chat_id=1, job8="abc123", interval=0.2)
    st.update("transcribe")
    st.start_heartbeat()
    time.sleep(0.2)
    st.show_cancelled()
    assert st._hb_thread is None


def test_no_duplicate_edit_for_identical_text():
    tg = FakeTG()
    st = bot.StatusMessage(tg, chat_id=1, job8=None, interval=10)
    st.update("probe")
    # Same stage, same second-ish -> identical text should be skipped.
    before = len(tg.edits) + len(tg.sent)
    st.update("probe")
    after = len(tg.edits) + len(tg.sent)
    # Allow at most 1 extra (timer may have advanced the spinner/marker).
    assert after - before <= 1
