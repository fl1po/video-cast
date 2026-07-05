"""Test scaffolding for the video-cast backend.

The Cast Platform is the seam — the device is faked at the Device contract
(see castplatform.py's docstring). Everything else (Flask routes, the Status
Broadcaster thread, session wiring) is real: tests talk HTTP to a live
server and read the actual /api/events stream.
"""
import json
import os
import queue
import tempfile
import threading

import http.client

import pytest

import app as videocast


class NullPlatform:
    """No devices on the network; tests attach fakes directly."""

    def discover(self, wait_s=0):
        return []

    def connect(self, friendly_name, timeout=30):
        raise RuntimeError("no real devices in tests")


class FakeMediaStatus:
    def __init__(self):
        self.player_state = "PLAYING"
        self.current_time = 100.0
        self.adjusted_current_time = 100.0
        self.duration = 600.0
        self.title = "Test Video"


class FakeDevice:
    """Stands in for a connected Device at the platform seam: records
    commands and only fires status events when the test *confirms* them —
    exactly how a real Chromecast behaves."""

    def __init__(self):
        self.media_status = FakeMediaStatus()
        self.volume_level = 0.5
        self.volume_muted = False
        self.commands = []
        self._media_cbs = []
        self._cast_cbs = []

    def on_media_status(self, cb):
        self._media_cbs.append(cb)

    def on_cast_status(self, cb):
        self._cast_cbs.append(cb)

    def play_media(self, url, mime, title="", thumb=""):
        self.commands.append(("play_media", url, mime))

    def block_until_active(self, timeout=None):
        pass

    def play(self):
        self.commands.append("play")

    def pause(self):
        self.commands.append("pause")

    def stop(self):
        self.commands.append("stop")

    def seek(self, position):
        self.commands.append(("seek", position))

    def update_status(self):
        # Real device answers GET_STATUS with its post-command state; tests
        # deliver that reply explicitly via confirm().
        self.commands.append("update_status")

    def set_volume(self, level):
        self.commands.append(("set_volume", level))

    def disconnect(self):
        self.commands.append("disconnect")

    def confirm(self, **changes):
        """Simulate the device reporting a media status change back."""
        for key, value in changes.items():
            setattr(self.media_status, key, value)
        if "current_time" in changes:
            self.media_status.adjusted_current_time = changes["current_time"]
        for cb in self._media_cbs:
            cb(self.media_status)

    def confirm_volume(self, level):
        self.volume_level = level
        for cb in self._cast_cbs:
            cb(self)


class SSEClient:
    """Reads /api/events from the live server into a queue."""

    def __init__(self, port):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        self.conn.request("GET", "/api/events")
        self.resp = self.conn.getresponse()
        self.messages = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for raw in self.resp:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data: "):
                    self.messages.put(json.loads(line[6:]))
        except Exception:
            pass

    def next(self, timeout=5):
        return self.messages.get(timeout=timeout)

    def assert_quiet(self, seconds):
        try:
            msg = self.messages.get(timeout=seconds)
        except queue.Empty:
            return
        raise AssertionError(f"expected no SSE broadcast, got: {msg}")

    def close(self):
        try:
            self.conn.sock.close()
        except Exception:
            pass
        self.conn.close()


@pytest.fixture(scope="session")
def flask_app():
    # Scratch state file + long poll intervals: broadcasts in tests come
    # from wake(), never from the periodic timer.
    state_file = os.path.join(tempfile.mkdtemp(), "state.json")
    return videocast.create_app(state_file=state_file, platform=NullPlatform(),
                                poll_playing=60, poll_idle=60)


@pytest.fixture(scope="session")
def server_port(flask_app):
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, flask_app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_port
    server.shutdown()


@pytest.fixture
def cast_session(flask_app):
    return flask_app.videocast.session


@pytest.fixture
def device(cast_session):
    fake = FakeDevice()
    cast_session.attach_device(fake)
    cast_session.intentional_stop = False
    yield fake
    cast_session.attach_device(None)


@pytest.fixture
def sse(server_port, device):
    client = SSEClient(server_port)
    client.next()  # initial on-connect status
    client.assert_quiet(0.2)  # drain any straggler from a previous test
    yield client
    client.close()


@pytest.fixture
def http_api(server_port):
    def post(path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", server_port, timeout=10)
        payload = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request("POST", path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data
    return post
