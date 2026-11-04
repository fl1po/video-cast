"""Auto-resume re-resolves the Resume Ref — the Media Ref of the last cast
that actually started. For an uploaded file that means the local file, not
yt-dlp on its filename."""
import io

import pytest

from conftest import FakeDevice
from mediasource import MediaRef, MediaSourceResolver, ResolveError
from session import CastSession
from store import StateStore
from test_session import FakeClock, StubPlatform


def no_extract(url):
    raise ResolveError(f"yt-dlp: no such video {url!r}")


def make(tmp_path, device):
    uploads = tmp_path / "uploads"
    uploads.mkdir(exist_ok=True)
    resolver = MediaSourceResolver(extract=no_extract, upload_dir=str(uploads),
                                   device_host=lambda: "10.0.0.5:5000")
    clock = FakeClock()
    session = CastSession(StubPlatform(device), resolver,
                          StateStore(str(tmp_path / "state.json")),
                          now=clock.now, sleep=lambda s: None, spawn=lambda fn: fn())
    return session, resolver, clock, uploads


def test_upload_resumes_from_the_local_file(tmp_path):
    device = FakeDevice()
    session, resolver, clock, uploads = make(tmp_path, device)
    ref = resolver.save_upload(io.BytesIO(b"video bytes"), "movie.mp4")

    session.cast(ref, "TV")
    device.confirm(player_state="PLAYING", current_time=100.0)
    clock.advance(20)  # leave the post-cast idle grace
    device.confirm(player_state="IDLE", current_time=0)

    played = ("play_media", "http://10.0.0.5:5000/api/media", "video/mp4")
    assert device.commands.count(played) == 2, "the resume re-casts the local file"
    assert ("seek", 100.0) in device.commands
    assert session.media_file_path() == str(uploads / "movie.mp4")


def restart(tmp_path, device):
    """A fresh Cast Session over the same state.json and upload dir."""
    session, _, clock, _ = make(tmp_path, device)
    session.attach_device(device)
    return session, clock


def test_upload_resumes_after_an_app_restart(tmp_path):
    device = FakeDevice()
    session, resolver, clock, uploads = make(tmp_path, device)
    session.cast(resolver.save_upload(io.BytesIO(b"v"), "movie.mp4"), "TV")
    device.confirm(player_state="PLAYING", current_time=100.0)
    session.persist()

    device = FakeDevice()
    session, clock = restart(tmp_path, device)
    clock.advance(20)
    device.confirm(player_state="IDLE", current_time=0)

    assert ("play_media", "http://10.0.0.5:5000/api/media", "video/mp4") in device.commands
    assert session.media_file_path() == str(uploads / "movie.mp4"), \
        "/api/media must serve the file again after the restart"
    assert session.last_url == "", "an upload never pre-fills the URL box"


def test_a_failed_cast_does_not_replace_the_resume_ref(tmp_path):
    device = FakeDevice()
    session, resolver, clock, _ = make(tmp_path, device)
    good = resolver.save_upload(io.BytesIO(b"v"), "movie.mp4")
    session.cast(good, "TV")

    with pytest.raises(ResolveError):
        session.cast(MediaRef.url("https://www.youtube.com/watch?v=typo"), "TV")

    assert session.resume_ref == good
    assert session.last_url == "https://www.youtube.com/watch?v=typo", \
        "the failed URL still pre-fills the box"


def test_a_missing_upload_on_resume_logs_and_stops(tmp_path, caplog):
    device = FakeDevice()
    session, resolver, clock, uploads = make(tmp_path, device)
    session.cast(resolver.save_upload(io.BytesIO(b"v"), "movie.mp4"), "TV")
    device.confirm(player_state="PLAYING", current_time=100.0)
    (uploads / "movie.mp4").unlink()

    clock.advance(20)
    device.confirm(player_state="IDLE", current_time=0)

    assert session.intentional_stop
    assert "gone: movie.mp4" in caplog.text


@pytest.mark.parametrize("last_url, file_exists, expected_ref, expected_prefill", [
    ("https://www.youtube.com/watch?v=1", False,
     MediaRef.url("https://www.youtube.com/watch?v=1"), "https://www.youtube.com/watch?v=1"),
    ("movie.mp4", True, MediaRef.upload("movie.mp4"), ""),
    ("movie.mp4", False, None, ""),
])
def test_state_from_before_the_resume_ref_migrates(tmp_path, last_url, file_exists,
                                                    expected_ref, expected_prefill):
    StateStore(str(tmp_path / "state.json")).update(last_url=last_url, last_position=42)
    (tmp_path / "uploads").mkdir()
    if file_exists:
        (tmp_path / "uploads" / "movie.mp4").write_bytes(b"v")

    session, _ = restart(tmp_path, FakeDevice())

    assert session.resume_ref == expected_ref
    assert session.last_url == expected_prefill


def test_a_cleared_resume_ref_is_not_re_derived_from_last_url(tmp_path):
    StateStore(str(tmp_path / "state.json")).update(
        last_url="https://www.youtube.com/watch?v=typo", resume_ref=None)
    session, _ = restart(tmp_path, FakeDevice())
    assert session.resume_ref is None
