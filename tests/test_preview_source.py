"""The Device and the Preview no longer share a URL: the Device gets the
best muxed format (usually HLS, which browsers can't play), while
the resolver chooses a separate progressive URL for the muted Preview.
MediaSource keeps the old behavior for simple sources — a progressive mp4
previews as itself, bare HLS has no preview."""
from mediasource import MediaRef, MediaSource, MediaSourceResolver


YT_FORMATS = [  # trimmed shape of a real YouTube listing
    {"format_id": "251", "protocol": "https", "vcodec": "none",
     "acodec": "opus", "url": "http://cdn/audio"},
    {"format_id": "96", "protocol": "m3u8_native", "vcodec": "avc1.640028",
     "acodec": "mp4a.40.2", "height": 1080, "url": "http://cdn/hls1080"},
    {"format_id": "137", "protocol": "https", "vcodec": "avc1.640028",
     "acodec": "none", "height": 1080, "url": "http://cdn/1080mp4"},
    {"format_id": "398", "protocol": "https", "vcodec": "av01.0.05M.08",
     "acodec": "none", "height": 720, "url": "http://cdn/720av1"},
    {"format_id": "247", "protocol": "https", "vcodec": "vp9",
     "acodec": "none", "height": 720, "url": "http://cdn/720vp9"},
    {"format_id": "136", "protocol": "https", "vcodec": "avc1.64001f",
     "acodec": "none", "height": 720, "url": "http://cdn/720mp4"},
    {"format_id": "18", "protocol": "https", "vcodec": "avc1.42001E",
     "acodec": "mp4a.40.2", "height": 360, "url": "http://cdn/360mp4"},
]


def preview_for(formats):
    """The Preview URL a YouTube page resolves to, given its format listing."""
    info = {"url": "http://cdn/hls1080", "protocol": "m3u8_native", "formats": formats}
    resolver = MediaSourceResolver(extract=lambda url: info, upload_dir="unused",
                                   device_host=lambda: "10.0.0.5:5000")
    return resolver.resolve(MediaRef.url("https://www.youtube.com/watch?v=1")).preview_url


def test_preview_picks_h264_720_over_bigger_or_fancier_codecs():
    assert preview_for(YT_FORMATS) == "http://cdn/720mp4"


def test_preview_requires_a_progressive_video_format():
    hls_only = [f for f in YT_FORMATS if f["protocol"] != "https"]
    audio_only = [f for f in YT_FORMATS if f["vcodec"] == "none"]
    assert preview_for(hls_only) is None
    assert preview_for(audio_only) is None
    assert preview_for([]) is None


def test_progressive_source_previews_as_itself():
    source = MediaSource(stream_url="http://cdn/video.mp4")
    assert source.preview_url == "http://cdn/video.mp4"


def test_bare_hls_source_has_no_preview():
    source = MediaSource(stream_url="http://cdn/live.m3u8",
                         mime="application/x-mpegURL")
    assert source.preview_url is None


def test_resolver_preview_wins_over_the_default():
    source = MediaSource(stream_url="http://cdn/hls.m3u8",
                         mime="application/x-mpegURL",
                         preview_url="http://cdn/720.mp4")
    assert source.preview_url == "http://cdn/720.mp4"
