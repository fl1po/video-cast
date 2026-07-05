"""A real receiver keeps reporting IDLE for a while after play_media (the
cast endpoints return before playback starts). That transient IDLE must not
trigger auto_resume — only an IDLE outside the post-cast grace window is an
unexpected stop worth recovering from."""
import time

import app as videocast


def _report_idle(device):
    device.media_controller.status.player_state = "IDLE"
    device.media_controller.status.current_time = 0
    device.media_controller.status.adjusted_current_time = 0
    for listener in device.media_controller.listeners:
        listener.new_media_status(device.media_controller.status)


def test_transient_idle_right_after_cast_does_not_trigger_auto_resume(device, monkeypatch):
    calls = []
    monkeypatch.setattr(videocast, "auto_resume", lambda: calls.append("resume"))
    monkeypatch.setattr(videocast, "last_url", "https://example.com/video")
    monkeypatch.setattr(videocast, "last_position", 100)

    monkeypatch.setattr(videocast, "last_cast_at", time.time())
    _report_idle(device)
    time.sleep(0.3)  # auto_resume runs on a thread
    assert calls == [], "IDLE within the post-cast grace is the receiver still loading"


def test_idle_outside_the_grace_still_triggers_auto_resume(device, monkeypatch):
    calls = []
    monkeypatch.setattr(videocast, "auto_resume", lambda: calls.append("resume"))
    monkeypatch.setattr(videocast, "last_url", "https://example.com/video")
    monkeypatch.setattr(videocast, "last_position", 100)

    monkeypatch.setattr(videocast, "last_cast_at", time.time() - 60)
    _report_idle(device)
    time.sleep(0.3)
    assert calls == ["resume"], "a genuine unexpected IDLE must still recover"
