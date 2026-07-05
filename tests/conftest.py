"""Test scaffolding for the video-cast backend.

The Chromecast is the external boundary — it's faked. Everything else
(Flask routes, SSE broadcaster thread, listener wiring) is real: tests talk
HTTP to a live server and read the actual /api/events stream.
"""
import json
import os
import queue
import tempfile
import threading

import http.client

import pytest

# Isolate state + disable periodic SSE ticks BEFORE the app module loads
# (it reads state and starts its broadcaster thread at import time).
os.environ["VIDEOCAST_STATE_FILE"] = os.path.join(tempfile.mkdtemp(), "state.json")
os.environ["VIDEOCAST_SSE_POLL_PLAYING"] = "60"
os.environ["VIDEOCAST_SSE_POLL_IDLE"] = "60"

import app as videocast  # noqa: E402


class FakeMediaStatus:
    def __init__(self):
        self.player_state = "PLAYING"
        self.current_time = 100.0
        self.adjusted_current_time = 100.0
        self.duration = 600.0
        self.title = "Test Video"


class FakeMediaController:
    """Stands in for pychromecast's MediaController: records commands and
    only changes its cached status when the test *confirms* them — exactly
    how a real Chromecast behaves."""

    def __init__(self):
        self.status = FakeMediaStatus()
        self.listeners = []
        self.commands = []

    def register_status_listener(self, listener):
        self.listeners.append(listener)

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

    def confirm(self, **changes):
        """Simulate the device reporting a status change back."""
        for key, value in changes.items():
            setattr(self.status, key, value)
        if "current_time" in changes:
            self.status.adjusted_current_time = changes["current_time"]
        for listener in self.listeners:
            listener.new_media_status(self.status)


class FakeCastStatus:
    def __init__(self):
        self.volume_level = 0.5
        self.volume_muted = False


class FakeCastDevice:
    def __init__(self):
        self.media_controller = FakeMediaController()
        self.status = FakeCastStatus()
        self.cast_listeners = []
        self.commands = []

    def register_status_listener(self, listener):
        self.cast_listeners.append(listener)

    def set_volume(self, level):
        self.commands.append(("set_volume", level))

    def confirm_volume(self, level):
        self.status.volume_level = level
        for listener in self.cast_listeners:
            listener.new_cast_status(self.status)


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
def server_port():
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, videocast.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_port
    server.shutdown()


@pytest.fixture
def device():
    fake = FakeCastDevice()
    videocast.cast_device = fake
    videocast.register_listeners(fake)
    videocast.intentional_stop = False
    yield fake
    videocast.cast_device = None


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
