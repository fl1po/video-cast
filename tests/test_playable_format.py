"""The Device only plays what its decoder supports, and only if the LOAD
announces the stream correctly.

Two ways a cast dies with the receiver reporting LOAD_FAILED /
idleReason=ERROR *after* it has already fetched the manifest and first
segment — so the relay looks healthy and the log stays empty:

  * the rendition is taller than the receiver will decode (measured: the
    Living Room TV refuses 1080p at any frame rate, plays 720p at 25 and
    50fps);
  * the LOAD announces streamType LIVE for a VOD source (pychromecast 14
    changed play_media's default from BUFFERED to LIVE).
"""
import yt_dlp

from app import YT_DLP_FORMAT, MAX_CAST_HEIGHT


def select(formats):
    """Run the app's real format selector over a yt-dlp format listing."""
    ydl = yt_dlp.YoutubeDL({"quiet": True, "simulate": True})
    selector = ydl.build_format_selector(YT_DLP_FORMAT)
    picked = list(selector({"formats": formats, "incomplete_formats": False,
                            "has_merged_format": False}))
    assert picked, "selector matched nothing"
    return picked[0]


def fmt(format_id, height, fps, vcodec, protocol="m3u8_native"):
    # tbr matters: "best" ranks on bitrate, so the fixtures need realistic
    # ones or the selector picks by tie-break instead of by quality.
    return {"format_id": format_id, "height": height, "fps": fps,
            "tbr": round(height * fps * 0.12), "vcodec": vcodec,
            "acodec": "mp4a.40.2", "protocol": protocol,
            "url": f"https://cdn.example/{format_id}.m3u8", "ext": "mp4"}


# A 50fps upload: its 1080p rendition is level 4.2.
HIGH_FRAMERATE = [
    fmt("93", 360, 25, "avc1.4D401E"),
    fmt("94", 480, 25, "avc1.4D401E"),
    fmt("300", 720, 50, "avc1.640020"),   # level 3.2 — plays
    fmt("301", 1080, 50, "avc1.64002A"),  # level 4.2 — LOAD_FAILED
]

# A 25fps upload: its 1080p rendition is level 4.0, and fails just the same.
NORMAL_FRAMERATE = [
    fmt("93", 360, 25, "avc1.4D401E"),
    fmt("94", 480, 25, "avc1.4D401E"),
    fmt("95", 720, 25, "avc1.64001F"),    # level 3.1 — plays
    fmt("96", 1080, 25, "avc1.640028"),   # level 4.0 — LOAD_FAILED
]


def test_1080p_is_never_selected_at_a_high_frame_rate():
    assert select(HIGH_FRAMERATE)["format_id"] == "300"


def test_1080p_is_never_selected_at_a_normal_frame_rate():
    # the level-4.0 rendition fails too, so fps is not the discriminator
    assert select(NORMAL_FRAMERATE)["format_id"] == "95"


def test_nothing_above_the_cap_is_ever_selected():
    for listing in (HIGH_FRAMERATE, NORMAL_FRAMERATE):
        assert select(listing)["height"] <= MAX_CAST_HEIGHT


def test_falls_back_when_no_hls_is_available():
    progressive = [fmt("18", 360, 30, "avc1.42001E", protocol="https")]
    assert select(progressive)["format_id"] == "18"


class FakeMediaController:
    def __init__(self, calls):
        self.kwargs = None
        self._calls = calls

    def play_media(self, url, content_type, **kwargs):
        self.kwargs = kwargs
        self._calls.append("load")


class FakeCast:
    def __init__(self, app_id=None):
        self.calls = []
        self.app_id = app_id
        self.media_controller = FakeMediaController(self.calls)

    def start_app(self, app_id, force_launch=False, timeout=None):
        self.calls.append(("start_app", app_id))
        self.app_id = app_id


def test_load_announces_a_buffered_stream_not_live():
    from castplatform import PychromecastDevice
    cc = FakeCast()
    PychromecastDevice(cc).play_media("http://host/x.m3u8",
                                      "application/x-mpegURL", title="t")
    assert cc.media_controller.kwargs["stream_type"] == "BUFFERED", (
        "pychromecast 14 defaults to LIVE, which drops duration and seek")


def test_load_launches_the_media_receiver_over_a_foreign_app():
    # pychromecast's default media_controller has app_must_match=False: if
    # the running app (Spotify, YouTube...) exposes the media namespace, the
    # LOAD goes to that app, which silently ignores it — nothing on the TV.
    from castplatform import PychromecastDevice
    from pychromecast.config import APP_MEDIA_RECEIVER
    cc = FakeCast(app_id="AndroidNativeApp")
    PychromecastDevice(cc).play_media("http://host/x.m3u8",
                                      "application/x-mpegURL", title="t")
    assert cc.calls == [("start_app", APP_MEDIA_RECEIVER), "load"]


def test_load_reuses_a_running_media_receiver():
    from castplatform import PychromecastDevice
    from pychromecast.config import APP_MEDIA_RECEIVER
    cc = FakeCast(app_id=APP_MEDIA_RECEIVER)
    PychromecastDevice(cc).play_media("http://host/x.mp4", "video/mp4")
    assert cc.calls == ["load"]

