"""The Media Source Resolver turns a Media Ref into a Media Source. Only the
network call (extract) is faked; classification, upload storage, m3u
reading, start time and error messages are the real rules."""
import io
import os

import pytest

from mediasource import MediaRef, MediaSourceResolver, ResolveError

HLS = "application/x-mpegURL"
PAGE_INFO = {"url": "https://cdn.example/page.m3u8", "protocol": "m3u8_native",
             "title": "A Page", "thumbnail": "https://cdn.example/t.jpg", "formats": []}


@pytest.fixture
def uploads(tmp_path):
    path = tmp_path / "uploads"
    path.mkdir()
    return path


@pytest.fixture
def extracted():
    return []


@pytest.fixture
def resolver(uploads, extracted):
    def extract(url):
        extracted.append(url)
        return PAGE_INFO
    return MediaSourceResolver(extract=extract, upload_dir=str(uploads),
                               device_host=lambda: "10.0.0.5:5000")


@pytest.mark.parametrize("url, mime, title", [
    ("https://cdn.example/v.mp4", "video/mp4", "v.mp4"),
    ("https://cdn.example/v.MKV?sig=1", "video/mp4", "v.MKV"),
    ("https://cdn.example/v.webm", "video/mp4", "v.webm"),
    ("https://iptv.example/live.m3u8?token=x", HLS, "live.m3u8"),
    ("https://iptv.example/list.m3u", HLS, "list.m3u"),
])
def test_direct_stream_urls_skip_extraction(resolver, extracted, url, mime, title):
    source = resolver.resolve(MediaRef.url(url))
    assert (source.stream_url, source.mime, source.title) == (url, mime, title)
    assert extracted == []


def test_page_urls_are_extracted(resolver, extracted):
    source = resolver.resolve(MediaRef.url("https://www.youtube.com/watch?v=1"))
    assert extracted == ["https://www.youtube.com/watch?v=1"]
    assert (source.stream_url, source.mime, source.title, source.thumbnail) == (
        "https://cdn.example/page.m3u8", HLS, "A Page", "https://cdn.example/t.jpg")


@pytest.mark.parametrize("url, seconds", [
    ("https://www.youtube.com/watch?v=1&t=90", 90),
    ("https://youtu.be/1?t=1h2m30s", 3750),
    ("https://www.youtube.com/watch?v=1&start=45", 45),
    ("https://cdn.example/v.mp4?t=12", 12),
    ("https://www.youtube.com/watch?v=1", 0),
])
def test_start_time_comes_from_the_url(resolver, url, seconds):
    assert resolver.resolve(MediaRef.url(url)).start_time == seconds


def test_an_upload_resolves_to_the_local_file(resolver, uploads):
    ref = resolver.save_upload(io.BytesIO(b"bytes"), "movie.mkv")
    source = resolver.resolve(ref)
    assert ref == MediaRef.upload("movie.mkv")
    assert source.stream_url == "http://10.0.0.5:5000/api/media"
    assert source.local_path == str(uploads / "movie.mkv")
    assert source.title == "movie.mkv"
    assert source.start_time == 0


@pytest.mark.parametrize("filename, stored", [
    ("../../app.py", "app.py"),
    (r"..\..\evil.mp4", "evil.mp4"),
    (r"C:\Users\me\Videos\clip.mp4", "clip.mp4"),
    ("Фильм 2024.mp4", "Фильм 2024.mp4"),  # non-ASCII names survive
])
def test_uploads_can_only_land_in_the_upload_dir(resolver, uploads, filename, stored):
    ref = resolver.save_upload(io.BytesIO(b"bytes"), filename)
    assert ref.value == stored
    assert os.listdir(uploads) == [stored]


@pytest.mark.parametrize("filename", ["", "..", "../"])
def test_an_unusable_upload_name_is_refused(resolver, filename):
    with pytest.raises(ResolveError, match="Invalid file name"):
        resolver.save_upload(io.BytesIO(b"bytes"), filename)


def test_an_uploaded_playlist_becomes_a_url_ref(resolver, uploads):
    playlist = b"#EXTM3U\n#EXTINF:-1,Channel\n\nhttps://iptv.example/live.m3u8\nhttps://iptv.example/other.m3u8\n"
    ref = resolver.save_upload(io.BytesIO(playlist), "channels.m3u")
    assert ref == MediaRef.url("https://iptv.example/live.m3u8")
    assert os.listdir(uploads) == [], "the playlist file is not kept"


def test_an_empty_playlist_is_refused(resolver):
    with pytest.raises(ResolveError, match="No stream URL found"):
        resolver.save_upload(io.BytesIO(b"#EXTM3U\n# nothing\n"), "empty.m3u8")


def test_a_missing_upload_is_a_resolve_error(resolver):
    with pytest.raises(ResolveError, match="gone: movie.mp4"):
        resolver.resolve(MediaRef.upload("movie.mp4"))


def test_extractor_failures_become_resolve_errors(uploads):
    def extract(url):
        raise RuntimeError("HTTP Error 403")
    resolver = MediaSourceResolver(extract, str(uploads), lambda: "h:1")
    with pytest.raises(ResolveError, match="HTTP Error 403"):
        resolver.resolve(MediaRef.url("https://www.youtube.com/watch?v=1"))


def test_an_extraction_without_a_stream_url_is_a_resolve_error(uploads):
    resolver = MediaSourceResolver(lambda url: {"title": "x"}, str(uploads), lambda: "h:1")
    with pytest.raises(ResolveError, match="no stream URL"):
        resolver.resolve(MediaRef.url("https://www.youtube.com/watch?v=1"))


def test_legacy_last_url_maps_to_a_ref(resolver, uploads):
    (uploads / "movie.mp4").write_bytes(b"bytes")
    assert resolver.legacy_ref("https://www.youtube.com/watch?v=1") == \
        MediaRef.url("https://www.youtube.com/watch?v=1")
    assert resolver.legacy_ref("movie.mp4") == MediaRef.upload("movie.mp4")
    assert resolver.legacy_ref("deleted.mp4") is None
    assert resolver.legacy_ref("") is None
