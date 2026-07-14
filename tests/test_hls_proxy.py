"""googlevideo URLs are IP-bound to the resolving network and carry no CORS
headers, so neither the cast receiver (HLS via XHR) nor a remote/VPN'd
browser can fetch them directly. Everything routes through /api/relay:
manifests get every URI rewritten, other media streams through."""
from app import parse_byte_range, rewrite_hls_manifest, route_via_relay
from session import MediaSource


BASE = "https://cdn.example/path/index.m3u8?sig=abc"


def proxy(url):
    return f"PROXY({url})"


def test_segment_lines_are_proxied_and_tags_left_alone():
    manifest = "\n".join([
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXTINF:3.0,",
        "https://cdn.example/seg1.ts",
        "#EXTINF:7.0,",
        "https://cdn.example/seg2.ts",
        "#EXT-X-ENDLIST",
    ])
    out = rewrite_hls_manifest(manifest, BASE, proxy).splitlines()
    assert out[0] == "#EXTM3U"
    assert out[1] == "#EXT-X-VERSION:3"
    assert out[3] == "PROXY(https://cdn.example/seg1.ts)"
    assert out[5] == "PROXY(https://cdn.example/seg2.ts)"
    assert out[6] == "#EXT-X-ENDLIST"


def test_relative_uris_resolve_against_the_manifest_url():
    manifest = "#EXTM3U\n#EXTINF:3.0,\nseg1.ts"
    out = rewrite_hls_manifest(manifest, BASE, proxy).splitlines()
    assert out[2] == "PROXY(https://cdn.example/path/seg1.ts)"


def test_uri_attributes_are_proxied():
    manifest = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXT-X-KEY:METHOD=AES-128,URI="https://cdn.example/key"'
    out = rewrite_hls_manifest(manifest, BASE, proxy).splitlines()
    assert out[1] == '#EXT-X-MAP:URI="PROXY(https://cdn.example/path/init.mp4)"'
    assert out[2] == '#EXT-X-KEY:METHOD=AES-128,URI="PROXY(https://cdn.example/key)"'


def test_byte_range_parsing():
    assert parse_byte_range("bytes=0-") == (0, None)
    assert parse_byte_range("bytes=500-999") == (500, 999)
    assert parse_byte_range("bytes=123456789-") == (123456789, None)
    assert parse_byte_range(None) is None
    assert parse_byte_range("") is None
    assert parse_byte_range("bytes=-500") is None, "suffix ranges fall back to full"
    assert parse_byte_range("bytes=0-1,5-6") is None, "multipart falls back to full"


def test_googlevideo_source_routes_both_sides_through_the_relay():
    source = route_via_relay(MediaSource(
        stream_url="https://manifest.googlevideo.com/x.m3u8",
        mime="application/x-mpegURL",
        preview_url="https://rr1.googlevideo.com/videoplayback?itag=136"))
    # the Device needs an absolute URL, the Preview a page-relative one
    assert source.stream_url.startswith("http://")
    assert "/api/relay?u=https%3A%2F%2Fmanifest.googlevideo.com" in source.stream_url
    assert source.preview_url.startswith("/api/relay?u=https%3A%2F%2Frr1.googlevideo.com")


def test_other_sources_are_left_alone():
    source = route_via_relay(MediaSource(stream_url="https://iptv.example/live.m3u8",
                                         mime="application/x-mpegURL"))
    assert source.stream_url == "https://iptv.example/live.m3u8"
    assert source.preview_url is None

    mp4 = route_via_relay(MediaSource(stream_url="https://cdn.example/v.mp4"))
    assert mp4.stream_url == mp4.preview_url == "https://cdn.example/v.mp4"
