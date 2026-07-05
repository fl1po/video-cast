"""Run the app against a simulated Chromecast — no real device needed.

The fake device confirms commands after a short delay (like a real receiver)
and its playback position advances in real time, so the full UI including
preview sync can be exercised in a browser.

Usage:
    VIDEOCAST_STATE_FILE=/tmp/sim-state.json python tests/sim_server.py [port]

Then open http://127.0.0.1:<port>/ and cast the sample URL shown on startup.
"""
import os
import sys
import threading
import time

if not os.environ.get("VIDEOCAST_STATE_FILE"):
    print("Refusing to run without VIDEOCAST_STATE_FILE (would clobber real state.json)")
    sys.exit(1)

# Start clean: leftover state would trigger try_reconnect's real network
# discovery, which wipes the seeded fake device from cached_devices.
if os.path.exists(os.environ["VIDEOCAST_STATE_FILE"]):
    os.remove(os.environ["VIDEOCAST_STATE_FILE"])

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app as videocast  # noqa: E402

CONFIRM_DELAY = float(os.environ.get("SIM_CONFIRM_DELAY", "0.4"))
FAKE_UUID = "00000000-0000-0000-0000-00000000fake"


class SimMediaStatus:
    def __init__(self):
        self.player_state = "IDLE"
        # Matches tests/sample.mp4 so the preview and "device" agree
        self.duration = float(os.environ.get("SIM_DURATION", "10"))
        self.title = ""
        self._base_time = 0.0
        self._base_at = time.time()

    @property
    def current_time(self):
        if self.player_state == "PLAYING":
            elapsed = self._base_time + (time.time() - self._base_at)
            if os.environ.get("SIM_LOOP"):
                return elapsed % self.duration  # endless playback for slow test drivers
            return min(self.duration, elapsed)
        return self._base_time

    @property
    def adjusted_current_time(self):
        return self.current_time

    def set_position(self, seconds):
        self._base_time = seconds
        self._base_at = time.time()


class SimMediaController:
    def __init__(self):
        self.status = SimMediaStatus()
        self.listeners = []

    def register_status_listener(self, listener):
        self.listeners.append(listener)

    def _notify(self):
        for listener in self.listeners:
            listener.new_media_status(self.status)

    def _later(self, fn):
        threading.Timer(CONFIRM_DELAY, fn).start()

    def play_media(self, url, mime, title="", thumb=""):
        self.status.title = title
        self.status.player_state = "BUFFERING"
        self.status.set_position(0)

        def start():
            self.status.player_state = "PLAYING"
            self.status.set_position(0)
            self._notify()
        threading.Timer(CONFIRM_DELAY * 3, start).start()

    def block_until_active(self, timeout=None):
        time.sleep(0.1)

    def play(self):
        def confirm():
            self.status.player_state = "PLAYING"
            self.status.set_position(self.status.current_time)
            self._notify()
        self._later(confirm)

    def pause(self):
        def confirm():
            self.status.set_position(self.status.current_time)
            self.status.player_state = "PAUSED"
            self._notify()
        self._later(confirm)

    def seek(self, position):
        def confirm():
            self.status.set_position(position)
            self._notify()
        self._later(confirm)

    def stop(self):
        def confirm():
            self.status.player_state = "IDLE"
            self.status.set_position(0)
            self._notify()
        self._later(confirm)

    def update_status(self):
        self._later(self._notify)


class SimCastStatus:
    volume_level = 0.5
    volume_muted = False


class SimCastDevice:
    def __init__(self):
        self.media_controller = SimMediaController()
        self.status = SimCastStatus()
        self.cast_listeners = []

    def register_status_listener(self, listener):
        self.cast_listeners.append(listener)

    def set_volume(self, level):
        def confirm():
            self.status.volume_level = level
            for listener in self.cast_listeners:
                listener.new_cast_status(self.status)
        threading.Timer(CONFIRM_DELAY, confirm).start()

    def quit_app(self):
        pass

    def disconnect(self, timeout=None):
        pass


def fake_connect(device_name):
    device = SimCastDevice()
    videocast.cast_device = device
    videocast.register_listeners(device)
    return device, None


videocast.connect_to_device = fake_connect
videocast.cached_devices = [{
    "uuid": FAKE_UUID, "friendly_name": "Simulated TV",
    "host": "127.0.0.1", "port": 8009, "model_name": "sim",
}]
videocast.last_device_uuid = FAKE_UUID


@videocast.app.route("/sample.mp4")
def sample_mp4():
    from flask import send_file
    return send_file(os.path.join(os.path.dirname(__file__), "sample.mp4"),
                     mimetype="video/mp4", conditional=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5058
    print(f"Sim ready — cast this URL: http://127.0.0.1:{port}/sample.mp4")
    videocast.app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
