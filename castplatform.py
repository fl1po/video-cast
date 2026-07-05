"""Cast Platform — the seam in front of Chromecast reality.

The interface (duck-typed; the simulated platform in tests/sim_platform.py
is the second adapter):

    platform.discover() -> [{"uuid", "friendly_name", "host", "port", "model_name"}]
    platform.connect(friendly_name) -> Device       # raises ConnectError

    device.play_media(url, mime, title="", thumb="")
    device.block_until_active(timeout)
    device.play() / .pause() / .stop() / .seek(pos) / .update_status()
    device.set_volume(level)
    device.media_status      -> obj with player_state, current_time,
                                adjusted_current_time, duration, title
    device.volume_level / device.volume_muted
    device.on_media_status(cb) / device.on_cast_status(cb)
    device.disconnect()

Status events are PUSH and fire only when the device confirms a change —
commands never synthesize events. The No-Echo Rule depends on this.
"""
import time

import pychromecast


class ConnectError(Exception):
    pass


class _MediaListener:
    def __init__(self, cb):
        self._cb = cb

    def new_media_status(self, status):
        self._cb(status)


class _CastListener:
    def __init__(self, cb):
        self._cb = cb

    def new_cast_status(self, status):
        self._cb(status)


class PychromecastDevice:
    """A connected Chromecast, seen through pychromecast."""

    def __init__(self, cc):
        self._cc = cc

    def play_media(self, url, mime, title="", thumb=""):
        self._cc.media_controller.play_media(url, mime, title=title, thumb=thumb)

    def block_until_active(self, timeout=None):
        self._cc.media_controller.block_until_active(timeout=timeout)

    def play(self):
        self._cc.media_controller.play()

    def pause(self):
        self._cc.media_controller.pause()

    def stop(self):
        self._cc.media_controller.stop()

    def seek(self, position):
        self._cc.media_controller.seek(position)

    def update_status(self):
        self._cc.media_controller.update_status()

    def set_volume(self, level):
        self._cc.set_volume(level)

    @property
    def media_status(self):
        return self._cc.media_controller.status

    @property
    def volume_level(self):
        return self._cc.status.volume_level if self._cc.status else 0

    @property
    def volume_muted(self):
        return self._cc.status.volume_muted if self._cc.status else False

    def on_media_status(self, cb):
        self._cc.media_controller.register_status_listener(_MediaListener(cb))

    def on_cast_status(self, cb):
        self._cc.register_status_listener(_CastListener(cb))

    def disconnect(self):
        # A quick reconnect puts the device in a bad state — quit the running
        # app and give the device time to settle before anyone reconnects.
        try:
            self._cc.quit_app()
            time.sleep(2)
            self._cc.disconnect(timeout=5)
        except Exception:
            pass
        time.sleep(3)


class PychromecastPlatform:
    def discover(self, wait_s=5):
        services, browser = pychromecast.discovery.discover_chromecasts()
        time.sleep(wait_s)
        pychromecast.discovery.stop_discovery(browser)
        return [{
            "uuid": str(s.uuid),
            "friendly_name": s.friendly_name,
            "host": s.host,
            "port": s.port,
            "model_name": s.model_name,
        } for s in services]

    def connect(self, friendly_name, timeout=30):
        casts, browser = pychromecast.get_listed_chromecasts(friendly_names=[friendly_name])
        if not casts:
            pychromecast.discovery.stop_discovery(browser)
            raise ConnectError("Could not connect to Chromecast. Is it on?")
        cc = casts[0]
        cc.wait(timeout=timeout)
        pychromecast.discovery.stop_discovery(browser)
        return PychromecastDevice(cc)
