"""A real receiver keeps reporting IDLE for a while after play_media (the
cast endpoints return before playback starts). That transient IDLE must not
trigger the auto-resume policy — only an IDLE outside the Post-Cast Idle
Grace is an unexpected stop worth recovering from."""
import time


def test_transient_idle_right_after_cast_does_not_trigger_auto_resume(cast_session, device, monkeypatch):
    calls = []
    monkeypatch.setattr(cast_session, "_auto_resume", lambda: calls.append("resume"))
    monkeypatch.setattr(cast_session, "last_url", "https://example.com/video")
    monkeypatch.setattr(cast_session, "last_position", 100)

    monkeypatch.setattr(cast_session, "last_cast_at", time.time())
    device.confirm(player_state="IDLE", current_time=0)
    time.sleep(0.3)  # auto-resume runs on a thread
    assert calls == [], "IDLE within the post-cast grace is the receiver still loading"


def test_idle_outside_the_grace_still_triggers_auto_resume(cast_session, device, monkeypatch):
    calls = []
    monkeypatch.setattr(cast_session, "_auto_resume", lambda: calls.append("resume"))
    monkeypatch.setattr(cast_session, "last_url", "https://example.com/video")
    monkeypatch.setattr(cast_session, "last_position", 100)

    monkeypatch.setattr(cast_session, "last_cast_at", time.time() - 60)
    device.confirm(player_state="IDLE", current_time=0)
    time.sleep(0.3)
    assert calls == ["resume"], "a genuine unexpected IDLE must still recover"
