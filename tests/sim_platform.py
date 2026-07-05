"""Simulated Cast Platform — the second adapter at the platform seam
(prod: castplatform.PychromecastPlatform).

Devices confirm commands after a short delay (like a real receiver) and the
playback position advances in real time, so the full UI including preview
sync can be exercised in a browser (see sim_server.py).

Knobs (env, read at construction): SIM_CONFIRM_DELAY, SIM_LOAD_DELAY,
SIM_DURATION, SIM_LOOP.
"""
import os
import threading
import time

FAKE_UUID = "00000000-0000-0000-0000-00000000fake"


class SimMediaStatus:
    def __init__(self, duration, loop):
        self.player_state = "IDLE"
        # Default matches tests/sample.mp4 so the preview and "device" agree
        self.duration = duration
        self.title = ""
        self._loop = loop
        self._base_time = 0.0
        self._base_at = time.time()

    @property
    def current_time(self):
        if self.player_state == "PLAYING":
            elapsed = self._base_time + (time.time() - self._base_at)
            if self._loop:
                return elapsed % self.duration  # endless playback for slow test drivers
            return min(self.duration, elapsed)
        return self._base_time

    @property
    def adjusted_current_time(self):
        return self.current_time

    def set_position(self, seconds):
        self._base_time = seconds
        self._base_at = time.time()


class SimDevice:
    def __init__(self, confirm_delay, load_delay, duration, loop):
        self._confirm_delay = confirm_delay
        self._load_delay = load_delay
        self.media_status = SimMediaStatus(duration, loop)
        self.volume_level = 0.5
        self.volume_muted = False
        self._media_cbs = []
        self._cast_cbs = []

    def on_media_status(self, cb):
        self._media_cbs.append(cb)

    def on_cast_status(self, cb):
        self._cast_cbs.append(cb)

    def _notify(self):
        for cb in self._media_cbs:
            cb(self.media_status)

    def _later(self, fn):
        threading.Timer(self._confirm_delay, fn).start()

    def play_media(self, url, mime, title="", thumb=""):
        st = self.media_status
        st.title = title
        st.set_position(0)

        def start():
            st.player_state = "PLAYING"
            st.set_position(0)
            self._notify()

        if self._load_delay > 0:
            # Like a real receiver: still IDLE when the cast endpoint returns,
            # and it pushes (still-IDLE) status events while loading.
            st.player_state = "IDLE"
            threading.Timer(self._load_delay / 2, self._notify).start()
            threading.Timer(self._load_delay, start).start()
        else:
            st.player_state = "BUFFERING"
            threading.Timer(self._confirm_delay * 3, start).start()

    def block_until_active(self, timeout=None):
        time.sleep(0.1)

    def play(self):
        def confirm():
            self.media_status.player_state = "PLAYING"
            self.media_status.set_position(self.media_status.current_time)
            self._notify()
        self._later(confirm)

    def pause(self):
        def confirm():
            self.media_status.set_position(self.media_status.current_time)
            self.media_status.player_state = "PAUSED"
            self._notify()
        self._later(confirm)

    def seek(self, position):
        def confirm():
            self.media_status.set_position(position)
            self._notify()
        self._later(confirm)

    def stop(self):
        def confirm():
            self.media_status.player_state = "IDLE"
            self.media_status.set_position(0)
            self._notify()
        self._later(confirm)

    def update_status(self):
        self._later(self._notify)

    def set_volume(self, level):
        def confirm():
            self.volume_level = level
            for cb in self._cast_cbs:
                cb(self)
        self._later(confirm)

    def disconnect(self):
        pass


class SimPlatform:
    def __init__(self, confirm_delay=None, load_delay=None, duration=None, loop=None):
        env = os.environ.get
        self._confirm_delay = float(env("SIM_CONFIRM_DELAY", "0.4")) if confirm_delay is None else confirm_delay
        self._load_delay = float(env("SIM_LOAD_DELAY", "2.0")) if load_delay is None else load_delay
        self._duration = float(env("SIM_DURATION", "10")) if duration is None else duration
        self._loop = bool(env("SIM_LOOP")) if loop is None else loop
        self.device = None  # most recently connected

    def discover(self, wait_s=0):
        return [{"uuid": FAKE_UUID, "friendly_name": "Simulated TV",
                 "host": "127.0.0.1", "port": 8009, "model_name": "sim"}]

    def connect(self, friendly_name, timeout=30):
        self.device = SimDevice(self._confirm_delay, self._load_delay,
                                self._duration, self._loop)
        return self.device
