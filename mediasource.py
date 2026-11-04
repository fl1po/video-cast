"""Media Source Resolver — turns a Media Ref (what the user asked to cast)
into a Media Source (what the Device and the Preview play).

Interface:

    resolver = MediaSourceResolver(extract, upload_dir, device_host)
    resolver.resolve(ref) -> MediaSource          # raises ResolveError
    resolver.save_upload(stream, filename) -> MediaRef   # raises ResolveError
    resolver.legacy_ref(last_url) -> MediaRef | None    # pre-Resume-Ref state.json

    extract(url) -> yt-dlp info dict   # the port: YtDlpExtractor in prod,
                                       # a fake returning info dicts in tests
    device_host() -> "ip:port"         # where the Device reaches this box

Everything else — page vs direct stream classification, preview format
choice, m3u reading, upload storage, relay routing, the ?t= start time — is
implementation. The cast routes and the Cast Session's auto-resume both
resolve through here, so a ref resolves the same way on first cast and on
resume.
"""
import logging
import mimetypes
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

from relay import relay_url

log = logging.getLogger(__name__)


class ResolveError(Exception):
    """A Media Ref could not be resolved. The message is user-facing."""


@dataclass
class MediaSource:
    """The resolved thing a cast plays. stream_url is what the Device plays;
    preview_url is a browser-playable progressive URL for the muted Preview."""
    stream_url: str
    title: str = ""
    thumbnail: str = ""
    mime: str = "video/mp4"
    local_path: str = None  # set when we serve the file ourselves (/api/media)
    preview_url: str = None
    start_time: int = 0  # seconds; from the ref (?t=90), 0 = from the start

    def __post_init__(self):
        # A progressive stream previews as itself; HLS has no browser-playable
        # preview unless the resolver picked a dedicated progressive URL.
        if self.preview_url is None and self.mime == "video/mp4":
            self.preview_url = self.stream_url


@dataclass(frozen=True)
class MediaRef:
    """What the user asked to cast, resolvable again later: a URL (page or
    direct stream) or the name of an uploaded file in the upload dir."""
    kind: str  # "url" | "upload"
    value: str

    @classmethod
    def url(cls, url):
        return cls("url", url.strip())

    @classmethod
    def upload(cls, filename):
        return cls("upload", filename)

    def to_dict(self):
        return {"kind": self.kind, "value": self.value}

    @classmethod
    def from_dict(cls, data):
        try:
            if data["kind"] in ("url", "upload") and data["value"]:
                return cls(data["kind"], data["value"])
        except (KeyError, TypeError):
            pass
        return None


DIRECT_EXTS = (".m3u", ".m3u8", ".mp4", ".mkv", ".avi", ".webm")
PLAYLIST_EXTS = (".m3u", ".m3u8")
HLS_MIME = "application/x-mpegURL"


class MediaSourceResolver:
    def __init__(self, extract, upload_dir, device_host):
        self._extract = extract
        self._upload_dir = upload_dir
        self._device_host = device_host

    def resolve(self, ref):
        if ref.kind == "upload":
            source = self._resolve_upload(ref.value)
        elif ref.kind == "url":
            source = self._resolve_url(ref.value)
        else:
            raise ResolveError(f"Unknown media kind: {ref.kind}")
        return self._route_via_relay(source)

    def save_upload(self, stream, filename):
        """Store an uploaded file and return its ref. An m3u playlist is
        read for the stream it names instead — it becomes a URL ref and the
        file is not kept."""
        name = _safe_filename(filename)
        if name.lower().endswith(PLAYLIST_EXTS):
            text = stream.read().decode("utf-8", errors="replace")
            lines = [l.strip() for l in text.splitlines()
                     if l.strip() and not l.strip().startswith("#")]
            if not lines:
                raise ResolveError("No stream URL found in playlist file")
            return MediaRef.url(lines[0])
        os.makedirs(self._upload_dir, exist_ok=True)
        with open(os.path.join(self._upload_dir, name), "wb") as f:
            while chunk := stream.read(1024 * 1024):
                f.write(chunk)
        return MediaRef.upload(name)

    def legacy_ref(self, last_url):
        """The ref an old state.json meant by last_url: a URL, or — for an
        upload, which stored its bare filename — the uploaded file if it is
        still there. None when nothing resolvable is left."""
        if not last_url:
            return None
        if "://" in last_url:
            return MediaRef.url(last_url)
        try:
            name = _safe_filename(last_url)
        except ResolveError:
            return None
        if os.path.isfile(os.path.join(self._upload_dir, name)):
            return MediaRef.upload(name)
        return None

    # ── implementation ───────────────────────────────────────────────────

    def _resolve_upload(self, name):
        path = os.path.join(self._upload_dir, _safe_filename(name))
        if not os.path.isfile(path):
            raise ResolveError(f"Uploaded file is gone: {name}")
        return MediaSource(
            stream_url=f"http://{self._device_host()}/api/media",
            title=name,
            mime=mimetypes.guess_type(path)[0] or "video/mp4",
            local_path=path,
        )

    def _resolve_url(self, url):
        if not url:
            raise ResolveError("No URL provided")
        path = url.split("?")[0].lower()
        if path.endswith(DIRECT_EXTS):
            source = MediaSource(
                stream_url=url,
                title=url.split("/")[-1].split("?")[0] or "Stream",
                mime=HLS_MIME if path.endswith(PLAYLIST_EXTS) else "video/mp4",
            )
        else:
            source = self._from_page(url)
        source.start_time = _parse_timestamp(url)
        return source

    def _from_page(self, url):
        try:
            info = self._extract(url)
        except ResolveError:
            raise
        except Exception as e:
            raise ResolveError(str(e)) from e
        stream_url = info.get("url", "")
        if not stream_url:
            raise ResolveError("yt-dlp returned no stream URL")
        return MediaSource(
            stream_url=stream_url,
            title=info.get("title", "Unknown"),
            thumbnail=info.get("thumbnail", ""),
            mime=HLS_MIME if "m3u8" in info.get("protocol", "") else "video/mp4",
            preview_url=_pick_preview_url(info.get("formats") or []) or None,
        )

    def _route_via_relay(self, source):
        """googlevideo URLs are IP-bound to this network and carry no CORS
        headers, so neither the Device (HLS via XHR) nor a remote/VPN'd
        browser can fetch them directly — both sides pull through the relay.
        The Device needs an absolute URL; the Preview a page-relative one."""
        if "googlevideo" in source.stream_url and "mpegURL" in source.mime:
            source.stream_url = relay_url(source.stream_url, host=self._device_host())
        if source.preview_url and "googlevideo" in source.preview_url:
            source.preview_url = relay_url(source.preview_url)
        return source


def _safe_filename(filename):
    """The bare name of an uploaded file, so it can only land in the upload
    dir. Unlike werkzeug's secure_filename this keeps non-ASCII names."""
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    if name in ("", ".", ".."):
        raise ResolveError("Invalid file name")
    return name


def _parse_timestamp(url):
    """Start time in seconds from a ?t= / ?start= parameter (90, 1h2m30s)."""
    try:
        qs = parse_qs(urlparse(url).query)
        t = qs.get("t", qs.get("start", [None]))[0]
        if t is None:
            return 0
        if t.isdigit():
            return int(t)
        total = 0
        for val, unit in re.findall(r"(\d+)([hms])", t):
            total += int(val) * {"h": 3600, "m": 60, "s": 1}[unit]
        return total
    except Exception:
        return 0


def _pick_preview_url(formats):
    """Best browser-playable progressive URL for the muted Preview: highest
    resolution up to 720p, h264 preferred (plays everywhere), audio optional
    (the Preview is muted anyway)."""
    candidates = [
        f for f in formats
        if f.get("protocol") in ("https", "http")
        and f.get("vcodec") not in (None, "none")
        and (f.get("height") or 0) <= 720
        and f.get("url")
    ]
    if not candidates:
        return ""
    return max(candidates, key=lambda f: (
        f.get("height") or 0,
        (f.get("vcodec") or "").startswith("avc1"),
        f.get("acodec") not in (None, "none"),
    ))["url"]


# ── the yt-dlp adapter ───────────────────────────────────────────────────

# YouTube has all but retired progressive (video+audio) formats — 720p mp4
# (22) is gone and 360p (18) comes and goes. Muxed HLS is what the Chromecast
# plays natively; the Preview gets its own progressive URL (_pick_preview_url)
# since browsers can't play those manifests.
# The receiver silently refuses renditions above MAX_CAST_HEIGHT: it fetches
# the manifest and the first segment, then answers LOAD_FAILED with
# idleReason=ERROR, leaving the Cast Session at IDLE with duration 0 and
# nothing in the log. Measured on the Living Room TV (Android box, Default
# Media Receiver): 1080p fails at any frame rate — level 4.0 (avc1.640028,
# 1080p25) and level 4.2 (avc1.64002A, 1080p50) alike — while 720p plays at
# both 25 and 50fps (levels 3.1/3.2). The ceiling is per-device, so it is
# overridable rather than baked into the selector.
MAX_CAST_HEIGHT = int(os.environ.get("VIDEOCAST_MAX_HEIGHT", "720"))
YT_DLP_FORMAT = (
    f"best[protocol^=m3u8][height<={MAX_CAST_HEIGHT}]/"
    f"best[height<={MAX_CAST_HEIGHT}]/best"
)

# yt-dlp solves YouTube's JS challenges with node or deno, which a hidden
# Windows launch (start.vbs) may not have on PATH.
JS_RUNTIME_DIRS = [
    r"C:\Program Files\nodejs",
    os.path.join(os.path.expanduser("~"), ".deno", "bin"),
]


class YtDlpExtractor:
    """extract(url) -> info dict, via yt-dlp. Raises ResolveError."""

    def __init__(self, cookies_from_browser="firefox"):
        for path in JS_RUNTIME_DIRS:
            if os.path.isdir(path) and path not in os.environ.get("PATH", ""):
                os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
        self._opts = {
            "format": YT_DLP_FORMAT,
            "quiet": True,
            "js_runtimes": {"node": {}},
            # Error messages end up in UI toasts — no ANSI color codes.
            "color": "never",
            # Without a logger, yt-dlp reports errors by writing to sys.stderr
            # before raising (YoutubeDL.trouble). The app routinely outlives
            # its console (start.vbs, ssh, closed terminal); stderr writes
            # then raise "[WinError 233] No process is on the other end of the
            # pipe", masking the real error. logging handlers swallow stream
            # errors, so this stays safe.
            "logger": logging.getLogger("yt_dlp"),
        }
        if cookies_from_browser:
            self._opts["cookiesfrombrowser"] = (cookies_from_browser,)

    def __call__(self, url):
        import yt_dlp
        try:
            with yt_dlp.YoutubeDL(dict(self._opts)) as ydl:
                return ydl.sanitize_info(ydl.extract_info(url, download=False))
        except yt_dlp.utils.DownloadError as e:
            raise ResolveError(f"yt-dlp: {e}") from e
