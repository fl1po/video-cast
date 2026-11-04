"""/api/cast and /api/cast_file at the HTTP interface: both cast through
the Cast Session and answer with the same response shape. Only yt-dlp
(extract) and the device (platform) are faked."""
import io
import os

import pytest

import app as videocast
from conftest import FakeDevice
from mediasource import ResolveError
from store import StateStore
from test_session import StubPlatform

RESPONSE_KEYS = {"status", "title", "thumbnail", "stream_url", "start_time"}
INFO = {"url": "https://cdn.example/page.m3u8", "protocol": "m3u8_native",
        "title": "A Page", "thumbnail": "https://cdn.example/t.jpg",
        "formats": [{"protocol": "https", "vcodec": "avc1.64001f", "height": 720,
                     "url": "https://cdn.example/720.mp4"}]}


def extract(url):
    if "missing" in url:
        raise ResolveError("yt-dlp: Video unavailable")
    return INFO


@pytest.fixture
def setup(tmp_path):
    device = FakeDevice()
    uploads = tmp_path / "uploads"
    state_file = str(tmp_path / "state.json")
    # a previously scanned device, as the Device Registry would have cached it
    StateStore(state_file).update(cached_devices=[{"uuid": "tv-1", "friendly_name": "TV"}])
    app = videocast.create_app(
        state_file=state_file, platform=StubPlatform(device),
        extract=extract, upload_dir=str(uploads), start_workers=False)
    return app.test_client(), device, uploads


def test_cast_a_page_url(setup):
    client, device, _ = setup
    resp = client.post("/api/cast", json={
        "url": "https://www.youtube.com/watch?v=1&t=90", "device_uuid": "tv-1"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == RESPONSE_KEYS
    assert body == {"status": "casting", "title": "A Page",
                    "thumbnail": "https://cdn.example/t.jpg",
                    "stream_url": "https://cdn.example/720.mp4", "start_time": 90}
    assert ("play_media", "https://cdn.example/page.m3u8", "application/x-mpegURL") in device.commands


def test_cast_an_uploaded_file(setup):
    client, device, uploads = setup
    resp = client.post("/api/cast_file", data={
        "device_uuid": "tv-1", "file": (io.BytesIO(b"video"), "movie.mp4")})
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == RESPONSE_KEYS, "both cast routes answer with one shape"
    assert body["title"] == "movie.mp4"
    assert os.listdir(uploads) == ["movie.mp4"]
    assert any(c[0] == "play_media" and c[1].endswith(":5000/api/media")
               for c in device.commands if isinstance(c, tuple))


def test_an_upload_for_an_unknown_device_is_not_saved(setup):
    client, device, uploads = setup
    resp = client.post("/api/cast_file", data={
        "device_uuid": "nope", "file": (io.BytesIO(b"video"), "movie.mp4")})
    assert resp.status_code == 404
    assert os.listdir(uploads) == []


def test_a_resolve_error_reaches_the_user(setup):
    client, device, _ = setup
    resp = client.post("/api/cast", json={
        "url": "https://www.youtube.com/watch?v=missing", "device_uuid": "tv-1"})
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "yt-dlp: Video unavailable"}
    assert device.commands == []


def test_the_url_box_prefill_follows_the_last_typed_url(setup):
    client, device, _ = setup
    client.post("/api/cast_file", data={
        "device_uuid": "tv-1", "file": (io.BytesIO(b"video"), "movie.mp4")})
    assert client.get("/api/state").get_json()["url"] == ""
    client.post("/api/cast", json={
        "url": "https://www.youtube.com/watch?v=missing", "device_uuid": "tv-1"})
    assert client.get("/api/state").get_json()["url"] == "https://www.youtube.com/watch?v=missing"
