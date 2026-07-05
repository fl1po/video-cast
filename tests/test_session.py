"""Unit tests at the Cast Session interface — no server, no threads, no
sleeps: now/sleep/spawn are injected and the device is a fake at the
platform seam. This is the resume policy, grace, and persistence coverage
that used to be impossible (auto_resume could only be stubbed out)."""
import pytest

from conftest import FakeDevice
from session import CastSession, MediaSource, NoActiveCast
from store import StateStore


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class StubPlatform:
    def __init__(self, device):
        self.device = device

    def discover(self, wait_s=0):
        return []

    def connect(self, friendly_name, timeout=30):
        return self.device


SOURCE = MediaSource(stream_url="http://cdn/video.mp4", title="A Video",
                     thumbnail="http://cdn/thumb.jpg")


def make_session(tmp_path, device, resolve=None, clock=None):
    clock = clock or FakeClock()
    store = StateStore(str(tmp_path / "state.json"))
    session = CastSession(
        StubPlatform(device), resolve or (lambda url: SOURCE), store,
        now=clock.now, sleep=lambda s: None, spawn=lambda fn: fn())
    return session, clock


def cast(session, device, url="https://example.com/watch?v=1"):
    session.note_attempt(url)
    session.start(SOURCE, "TV")


def test_start_casts_and_persists(tmp_path):
    device = FakeDevice()
    session, clock = make_session(tmp_path, device)
    cast(session, device)

    assert ("play_media", "http://cdn/video.mp4", "video/mp4") in device.commands
    # A fresh store proves the round-trip through state.json
    reread = StateStore(str(tmp_path / "state.json"))
    assert reread.get("last_url") == "https://example.com/watch?v=1"
    assert reread.get("media_title") == "A Video"


def test_session_restores_from_the_store(tmp_path):
    device = FakeDevice()
    session, clock = make_session(tmp_path, device)
    cast(session, device)
    device.confirm(player_state="PLAYING", current_time=42.0)
    session.persist()

    restored, _ = make_session(tmp_path, FakeDevice())
    assert restored.last_url == "https://example.com/watch?v=1"
    assert restored.last_position == 42.0
    assert restored.media_title == "A Video"


def test_unexpected_idle_resumes_at_last_position(tmp_path):
    device = FakeDevice()
    resolved = []
    fresh = MediaSource(stream_url="http://cdn/fresh.mp4", title="A Video")

    def resolve(url):
        resolved.append(url)
        return fresh

    session, clock = make_session(tmp_path, device, resolve=resolve)
    cast(session, device)

    device.confirm(player_state="PLAYING", current_time=100.0)  # position tracked
    clock.advance(20)  # leave the post-cast idle grace
    device.confirm(player_state="IDLE", current_time=0)

    assert resolved == ["https://example.com/watch?v=1"], "resume re-resolves the original URL"
    assert ("play_media", "http://cdn/fresh.mp4", "video/mp4") in device.commands
    assert ("seek", 100.0) in device.commands, "resume seeks back to the saved position"


def test_transient_idle_within_the_grace_does_not_resume(tmp_path):
    device = FakeDevice()
    resolved = []
    session, clock = make_session(
        tmp_path, device, resolve=lambda url: resolved.append(url) or SOURCE)
    cast(session, device)

    device.confirm(player_state="IDLE", current_time=0)  # still within the grace

    assert resolved == []


def test_idle_near_the_end_is_a_natural_finish_not_a_crash(tmp_path):
    device = FakeDevice()
    resolved = []
    session, clock = make_session(
        tmp_path, device, resolve=lambda url: resolved.append(url) or SOURCE)
    cast(session, device)

    device.confirm(player_state="PLAYING", current_time=598.0)  # duration is 600
    clock.advance(20)
    device.confirm(player_state="IDLE", current_time=0)

    assert resolved == []
    assert session.intentional_stop


def test_resume_cooldown_limits_attempts(tmp_path):
    device = FakeDevice()
    resolved = []
    fresh = MediaSource(stream_url="http://cdn/fresh.mp4")

    def resolve(url):
        resolved.append(url)
        return fresh

    session, clock = make_session(tmp_path, device, resolve=resolve)
    cast(session, device)

    device.confirm(player_state="PLAYING", current_time=100.0)
    clock.advance(20)
    device.confirm(player_state="IDLE", current_time=0)  # first resume
    clock.advance(20)  # past the grace again, but within the 30s cooldown
    device.confirm(player_state="IDLE", current_time=0)

    assert len(resolved) == 1, "a second attempt within the cooldown is skipped"


def test_failed_resume_stops_retrying(tmp_path):
    device = FakeDevice()
    attempts = []

    def resolve(url):
        attempts.append(url)
        raise RuntimeError("stream gone")

    session, clock = make_session(tmp_path, device, resolve=resolve)
    cast(session, device)

    device.confirm(player_state="PLAYING", current_time=100.0)
    clock.advance(20)
    device.confirm(player_state="IDLE", current_time=0)
    assert session.intentional_stop, "a failed resume must not retry forever"

    clock.advance(60)  # well past grace and cooldown
    device.confirm(player_state="IDLE", current_time=0)
    assert len(attempts) == 1


def test_m3u8_stream_has_no_preview_url(tmp_path):
    device = FakeDevice()
    session, clock = make_session(tmp_path, device)
    session.start(MediaSource(stream_url="http://cdn/live.m3u8", title="Live",
                              mime="application/x-mpegURL"), "TV")

    assert session.status()["stream_url"] == ""


def test_controls_require_an_active_cast(tmp_path):
    session, clock = make_session(tmp_path, None)
    with pytest.raises(NoActiveCast):
        session.play()
    with pytest.raises(NoActiveCast):
        session.stop()
