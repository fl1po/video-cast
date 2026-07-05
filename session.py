"""Cast Session — the backend's single unit of playback state: what's
playing, on which device, at what position, whether the last stop was
intentional, plus the policies that act on it.

Interface:

    session.note_attempt(url)                # remember the user's last try
    session.start(source, device_name, start_time=0)   # raises on failure
    session.stop() / .play() / .pause() / .seek(pos) / .set_volume(level)
    session.status() -> dict                 # the SSE/status payload
    session.media_file_path()                # local file served at /api/media
    session.attach_device(device)            # internal seam: start() and tests
    session.wake                             # set to the broadcaster's wake()

Everything else — position tracking, the intentional-stop flag, the
Post-Cast Idle Grace, the auto-resume policy, persistence — is
implementation. Control commands never wake the broadcaster (the No-Echo
Rule): the device's own confirmation event does.
"""
import threading
import time
from dataclasses import dataclass


class NoActiveCast(Exception):
    pass


@dataclass
class MediaSource:
    """The resolved thing a cast plays."""
    stream_url: str
    title: str = ""
    thumbnail: str = ""
    mime: str = "video/mp4"
    local_path: str = None  # set when we serve the file ourselves (/api/media)


# The receiver keeps reporting IDLE for a while after play_media (the cast
# endpoint returns before playback starts) — within this window an IDLE
# status means "still loading", not "stopped" (mirrors the client grace).
IDLE_GRACE_S = 15


class CastSession:
    def __init__(self, platform, resolve_source, store, *,
                 idle_grace_s=IDLE_GRACE_S, resume_cooldown_s=30,
                 resume_settle_s=5, now=time.time, sleep=time.sleep,
                 spawn=None):
        self._platform = platform
        self._resolve_source = resolve_source
        self._store = store
        self._idle_grace_s = idle_grace_s
        self._resume_cooldown_s = resume_cooldown_s
        self._resume_settle_s = resume_settle_s
        self._now = now
        self._sleep = sleep
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.wake = lambda: None  # wired to the broadcaster by create_app

        self._lock = threading.RLock()
        self._device = None
        self._casting = False  # a new cast is being set up
        self._local_file_path = None
        self._resume_lock = threading.Lock()
        self._last_resume_attempt = 0.0

        self.intentional_stop = False
        self.last_cast_at = 0.0
        self.last_url = store.get("last_url", "")
        self.last_position = store.get("last_position", 0)
        self.media_title = store.get("media_title")
        self.media_thumbnail = store.get("media_thumbnail")
        self.media_stream_url = store.get("media_stream_url")

    # ── interface ────────────────────────────────────────────────────────

    def note_attempt(self, url):
        """Remember what the user last tried to cast (prefills the UI and
        feeds auto-resume), even if the cast then fails."""
        self.last_url = url

    def status(self):
        device = self._device
        if not device:
            return {"state": "IDLE", "connected": False}
        try:
            with self._lock:
                ms = device.media_status
                stream_url = self.media_stream_url
                return {
                    "connected": True,
                    "state": ms.player_state or "UNKNOWN",
                    "title": self.media_title or ms.title or "",
                    "thumbnail": self.media_thumbnail or "",
                    # m3u8 streams have no browser-playable preview URL
                    "stream_url": stream_url if stream_url and "m3u8" not in stream_url else "",
                    "current_time": ms.adjusted_current_time or ms.current_time or 0,
                    "duration": ms.duration or 0,
                    "volume": device.volume_level,
                    "muted": device.volume_muted,
                }
        except Exception:
            return {"state": "IDLE", "connected": False}

    def start(self, source, device_name, start_time=0):
        """Connect to the device and cast a Media Source. Raises on failure."""
        self._casting = True
        try:
            self.attach_device(None)  # old connection must be gone before reconnecting
            device = self._platform.connect(device_name)
            self.attach_device(device)

            with self._lock:
                self.intentional_stop = False
                self.last_position = 0
                self.media_title = source.title
                self.media_thumbnail = source.thumbnail
                self.media_stream_url = source.stream_url
                self._local_file_path = source.local_path
                self.last_cast_at = self._now()

            device.play_media(source.stream_url, source.mime,
                              title=source.title or "", thumb=source.thumbnail or "")
            device.block_until_active(timeout=30)

            if start_time > 0:
                self._spawn(lambda: self._seek_when_ready(device, start_time))

            self.wake()
            self.persist()
        finally:
            self._casting = False

    def stop(self):
        device = self._require_device()
        with self._lock:
            self.intentional_stop = True
            self.last_position = 0
            self.media_title = None
            self.media_thumbnail = None
            self.media_stream_url = None
        device.stop()
        self.wake()
        self.persist()

    def play(self):
        device = self._require_device()
        # No wake() here: the device's cached status is still the pre-command
        # state, so broadcasting now would echo stale data (the No-Echo Rule).
        # The confirmation event broadcasts; update_status (GET_STATUS)
        # prompts that reply from devices that are slow to push it.
        device.play()
        device.update_status()

    def pause(self):
        device = self._require_device()
        # See play(): broadcast happens when the device confirms, not now.
        device.pause()
        device.update_status()

    def seek(self, position):
        device = self._require_device()
        try:
            ms = device.media_status
            was_playing = ms and ms.player_state in ("PLAYING", "BUFFERING")
        except Exception:
            was_playing = False
        device.seek(float(position))
        # Some Chromecasts pause after seek — resume playback, but only if it
        # was playing before the seek (a seek while paused must stay paused)
        self._sleep(0.3)
        try:
            ms = device.media_status
            if was_playing and ms and ms.player_state in ("PAUSED", "BUFFERING"):
                device.play()
        except Exception:
            pass
        # See play(): broadcast happens when the device confirms, not now.
        device.update_status()

    def set_volume(self, level):
        device = self._require_device()
        # See play(): the cast status listener broadcasts once the device
        # reports the new volume.
        device.set_volume(max(0.0, min(1.0, float(level))))

    def media_file_path(self):
        return self._local_file_path

    def attach_device(self, device):
        """Adopt a connected Device (or None to detach). Internal seam —
        start() uses it; tests use it to inject fakes."""
        old, self._device = self._device, None
        if old is not None:
            old.disconnect()
        if device is not None:
            device.on_media_status(self._on_media_status)
            device.on_cast_status(self._on_cast_status)
            self._device = device

    def persist(self):
        self._store.update(
            last_url=self.last_url,
            media_title=self.media_title,
            media_thumbnail=self.media_thumbnail,
            media_stream_url=self.media_stream_url,
            last_position=self.last_position,
        )

    # ── implementation ───────────────────────────────────────────────────

    def _require_device(self):
        device = self._device
        if not device:
            raise NoActiveCast()
        return device

    def _on_media_status(self, status):
        with self._lock:
            if status.player_state in ("PLAYING", "PAUSED", "BUFFERING"):
                pos = status.adjusted_current_time or status.current_time
                if pos:
                    self.last_position = pos
                    self.intentional_stop = False
            # Auto-resume if the cast went idle unexpectedly (TV sleep/wake).
            # But NOT if the video ended naturally (position near duration)
            # and not while a fresh cast's receiver is still loading
            # (transient IDLE within the Post-Cast Idle Grace).
            elif (status.player_state == "IDLE" and not self.intentional_stop
                  and not self._casting and self.last_url
                  and self._now() - self.last_cast_at > self._idle_grace_s):
                duration = status.duration or 0
                if duration > 0 and self.last_position > 0 and (duration - self.last_position) < 5:
                    self.intentional_stop = True  # video finished naturally
                else:
                    self._spawn(self._auto_resume)
        self.wake()

    def _on_cast_status(self, status):
        self.wake()

    def _auto_resume(self):
        """Re-cast the last stream and seek to the saved position after the
        TV wakes."""
        if not self._resume_lock.acquire(blocking=False):
            return  # another resume already in progress
        self._casting = True
        try:
            now = self._now()
            if now - self._last_resume_attempt < self._resume_cooldown_s:
                return
            self._last_resume_attempt = now

            self._sleep(self._resume_settle_s)  # let the device fully wake
            device = self._device
            if not device or not self.last_url:
                return
            try:
                if device.media_status.player_state in ("PLAYING", "BUFFERING", "PAUSED"):
                    return  # already recovered on its own
            except Exception:
                pass

            # Re-resolve: the old stream URL has likely expired
            source = self._resolve_source(self.last_url)
            with self._lock:
                self.media_stream_url = source.stream_url
                self.media_title = source.title
                self.media_thumbnail = source.thumbnail
                self.last_cast_at = self._now()

            device.play_media(source.stream_url, source.mime or "video/mp4",
                              title=source.title or "", thumb=source.thumbnail or "")
            device.block_until_active(timeout=30)
            if self.last_position > 0:
                device.seek(self.last_position)
            self.wake()
            self.persist()
        except Exception:
            self.intentional_stop = True  # stop retrying on failure
        finally:
            self._casting = False
            self._resume_lock.release()

    def _seek_when_ready(self, device, position):
        """A fresh cast ignores seeks until the receiver is active — retry
        until it takes (URL timestamps like ?t=90)."""
        for _ in range(60):
            try:
                if device.media_status.player_state in ("PLAYING", "BUFFERING", "PAUSED"):
                    device.seek(position)
                    self.wake()
                    return
            except Exception:
                pass
            self._sleep(0.5)
