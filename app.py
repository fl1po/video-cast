"""video-cast — Flask entry point.

create_app() wires the modules together; importing this module has no side
effects. Components live on app.videocast (session, registry, broadcaster,
platform, store) so tests reach them without monkeypatching globals.

    Cast Session   (session.py)      playback state + policies
    Device Registry(devices.py)      scan / cache / name lookup
    Status Broadcaster (sse.py)      SSE fan-out
    Cast Platform  (castplatform.py) the seam in front of pychromecast
"""
import json
import logging
import logging.handlers
import mimetypes
import os
import queue
import re
import socket
import sys
import threading
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs, urljoin, quote

from flask import Flask, render_template, request, jsonify, Response, send_file
import requests
import yt_dlp

from devices import DeviceRegistry
from session import CastSession, MediaSource, NoActiveCast
from sse import StatusBroadcaster
from store import StateStore

# Ensure node is on PATH for yt-dlp JS challenge solving
for _node_path in [
    r"C:\Program Files\nodejs",
    os.path.join(os.path.expanduser("~"), ".deno", "bin"),
]:
    if os.path.isdir(_node_path) and _node_path not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _node_path + os.pathsep + os.environ.get("PATH", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

# YouTube has all but retired progressive (video+audio) formats — 720p mp4
# (22) is gone and 360p (18) comes and goes. Muxed HLS reaches 1080p and the
# Chromecast plays it natively; the Preview gets its own progressive URL
# (pick_preview_url) since browsers can't play those manifests.
YT_DLP_FORMAT = "best[protocol^=m3u8][height<=1080]/best[height<=720]/best"
YT_DLP_OPTS = {
    "format": YT_DLP_FORMAT,
    "quiet": True,
    "cookiesfrombrowser": ("firefox",),
    "js_runtimes": {"node": {}},
    # Error messages end up in UI toasts — no ANSI color codes.
    "color": "never",
    # Without a logger, yt-dlp reports errors by writing to sys.stderr before
    # raising (YoutubeDL.trouble). The app routinely outlives its console
    # (start.vbs, ssh, closed terminal); stderr writes then raise "[WinError
    # 233] No process is on the other end of the pipe", masking the real
    # error. logging handlers swallow stream errors, so this stays safe.
    "logger": logging.getLogger("yt_dlp"),
}


def get_lan_ip():
    """Get this machine's LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def parse_timestamp(url):
    """Extract start time in seconds from URL timestamp parameter."""
    try:
        qs = parse_qs(urlparse(url).query)
        t = qs.get("t", qs.get("start", [None]))[0]
        if t is None:
            return 0
        # Pure number = seconds
        if t.isdigit():
            return int(t)
        # Format like 1h2m30s, 2m30s, 45s
        total = 0
        for val, unit in re.findall(r'(\d+)([hms])', t):
            if unit == 'h': total += int(val) * 3600
            elif unit == 'm': total += int(val) * 60
            elif unit == 's': total += int(val)
        return total
    except Exception:
        return 0


# Connection reuse for the relay — one TLS handshake per googlevideo
# host instead of one per segment.
_relay_http = requests.Session()

# googlevideo cuts open-ended fetches after a ~2MB burst (anti-scraper
# pacing) but serves bounded Range windows at full speed — same trick as
# yt-dlp's --http-chunk-size. Every upstream fetch stays under this.
RELAY_CHUNK = 8 * 1024 * 1024


def parse_byte_range(header):
    """'bytes=A-B' / 'bytes=A-' -> (A, B or None). Anything else (suffix,
    multipart) -> None; the relay then serves the whole resource."""
    m = re.fullmatch(r"bytes=(\d+)-(\d*)", (header or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)) if m.group(2) else None


def relay_url(upstream_url, host=None):
    """Local /api/relay URL serving an upstream URL from this box. Absolute
    (with host) for the Device; relative for the Preview, which resolves it
    against whatever origin the page was loaded from."""
    prefix = f"http://{host}" if host else ""
    return f"{prefix}/api/relay?u={quote(upstream_url, safe='')}"


def route_via_relay(source):
    """googlevideo URLs are IP-bound to this network and carry no CORS
    headers, so neither the Device (HLS via XHR) nor a remote/VPN'd browser
    can fetch them directly — both sides pull through the relay instead."""
    if "googlevideo" in source.stream_url and "mpegURL" in source.mime:
        source.stream_url = relay_url(source.stream_url, host=f"{get_lan_ip()}:5000")
    if source.preview_url and "googlevideo" in source.preview_url:
        source.preview_url = relay_url(source.preview_url)
    return source


def rewrite_hls_manifest(text, base_url, proxy_url):
    """Route every URI in an m3u8 playlist through proxy_url(absolute_url).
    Covers segment/variant lines and URI="..." attributes (EXT-X-MAP,
    EXT-X-KEY); relative URIs are resolved against the manifest's URL."""
    def sub_uri_attr(match):
        return f'URI="{proxy_url(urljoin(base_url, match.group(1)))}"'

    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            out.append(re.sub(r'URI="([^"]+)"', sub_uri_attr, line))
        else:
            out.append(proxy_url(urljoin(base_url, stripped)))
    return "\n".join(out) + "\n"


def pick_preview_url(formats):
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


def resolve_with_ytdlp(url):
    """Resolve a page URL to a Media Source via yt-dlp. Raises on failure."""
    with yt_dlp.YoutubeDL(dict(YT_DLP_OPTS)) as ydl:
        info = ydl.extract_info(url, download=False)
        info = ydl.sanitize_info(info)
    stream_url = info.get("url", "")
    if not stream_url:
        raise RuntimeError("yt-dlp returned no stream URL")
    protocol = info.get("protocol", "")
    return MediaSource(
        stream_url=stream_url,
        title=info.get("title", "Unknown"),
        thumbnail=info.get("thumbnail", ""),
        mime="application/x-mpegURL" if "m3u8" in protocol else "video/mp4",
        preview_url=pick_preview_url(info.get("formats") or []) or None,
    )


def create_app(state_file=None, platform=None, resolve_source=None,
               poll_playing=None, poll_idle=None, start_workers=True):
    state_file = state_file or os.environ.get("VIDEOCAST_STATE_FILE") \
        or os.path.join(BASE_DIR, "state.json")
    if poll_playing is None:
        # 1s so the UI clock ticks every second — status() reads pychromecast's
        # cached, time-interpolated position; no device traffic per poll.
        poll_playing = float(os.environ.get("VIDEOCAST_SSE_POLL_PLAYING", "1"))
    if poll_idle is None:
        poll_idle = float(os.environ.get("VIDEOCAST_SSE_POLL_IDLE", "5"))
    if platform is None:
        from castplatform import PychromecastPlatform
        platform = PychromecastPlatform()
    resolve_source = resolve_source or resolve_with_ytdlp

    def resolve_for_cast(url):
        return route_via_relay(resolve_source(url))

    store = StateStore(state_file)
    registry = DeviceRegistry(platform, store)
    session = CastSession(platform, resolve_for_cast, store)
    broadcaster = StatusBroadcaster(session.status, poll_playing, poll_idle)
    session.wake = broadcaster.wake

    os.makedirs(UPLOAD_DIR, exist_ok=True)

    app = Flask(__name__)
    app.videocast = SimpleNamespace(session=session, registry=registry,
                                    broadcaster=broadcaster, platform=platform,
                                    store=store)

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/media")
    def serve_media():
        """Serve the current local file for Chromecast to stream."""
        path = session.media_file_path()
        if not path or not os.path.isfile(path):
            return jsonify({"error": "No file"}), 404
        mime = mimetypes.guess_type(path)[0] or "video/mp4"
        return send_file(path, mimetype=mime, conditional=True)

    @app.route("/api/relay", methods=["GET", "OPTIONS"])
    def relay():
        """Serve upstream media from this box: m3u8 manifests come back
        with every URI rewritten to this endpoint (plus the CORS headers
        the cast receiver needs), everything else streams through."""
        cors = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
        }
        if request.method == "OPTIONS":
            return Response(status=204, headers=cors)

        u = request.args.get("u", "")
        if not u.startswith(("http://", "https://")):
            return jsonify({"error": "bad url"}), 400

        rng = parse_byte_range(request.headers.get("Range"))
        start = rng[0] if rng else 0
        asked_end = rng[1] if rng else None  # inclusive, None = to the end

        def window_req(pos):
            win_end = pos + RELAY_CHUNK - 1
            if asked_end is not None:
                win_end = min(win_end, asked_end)
            return _relay_http.get(u, stream=True, timeout=(5, 30),
                                   headers={"Range": f"bytes={pos}-{win_end}"})

        try:
            first = window_req(start)
        except requests.RequestException as e:
            return jsonify({"error": f"upstream: {e}"}), 502
        if first.status_code >= 400:
            return Response(status=first.status_code, headers=cors)

        ctype = first.headers.get("Content-Type", "")
        if "mpegurl" in ctype.lower() or u.split("?")[0].endswith((".m3u8", ".m3u")):
            host = request.host
            body = rewrite_hls_manifest(
                first.text, u, lambda url: relay_url(url, host=host))
            return Response(body, mimetype="application/vnd.apple.mpegurl", headers=cors)

        content_range = first.headers.get("Content-Range", "")
        total = None
        if first.status_code == 206 and "/" in content_range:
            size = content_range.rsplit("/", 1)[1]
            total = int(size) if size.isdigit() else None

        if total is None:
            # Upstream ignored the Range — stream its single response through.
            headers = dict(cors)
            for h in ("Content-Type", "Content-Length", "Accept-Ranges"):
                if h in first.headers:
                    headers[h] = first.headers[h]
            return Response(first.iter_content(64 * 1024),
                            status=first.status_code, headers=headers)

        end = min(asked_end, total - 1) if asked_end is not None else total - 1

        def stream():
            pos, resp, failures = start, first, 0
            while True:
                made_progress = False
                try:
                    for chunk in resp.iter_content(64 * 1024):
                        pos += len(chunk)
                        made_progress = True
                        yield chunk
                    failures = 0
                except requests.RequestException:
                    # googlevideo sometimes resets a window mid-transfer —
                    # resume from the bytes actually received.
                    failures = 0 if made_progress else failures + 1
                    if failures > 3:
                        return
                finally:
                    resp.close()
                if pos > end:
                    return
                try:
                    resp = window_req(pos)
                except requests.RequestException:
                    return
                if resp.status_code != 206:
                    resp.close()
                    return

        headers = dict(cors)
        headers["Content-Type"] = ctype or "application/octet-stream"
        headers["Accept-Ranges"] = "bytes"
        headers["Content-Length"] = str(end - start + 1)
        if rng:
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            return Response(stream(), status=206, headers=headers)
        return Response(stream(), status=200, headers=headers)

    @app.route("/api/state")
    def state():
        """Return last used URL and device for syncing across clients."""
        return jsonify({
            "url": session.last_url,
            "device_uuid": registry.last_uuid,
            "devices": registry.cached(),
            "playback": session.status(),
        })

    @app.route("/api/events")
    def events():
        """SSE endpoint — streams status updates to the client."""
        def stream():
            q = broadcaster.subscribe()
            try:
                # Send current status immediately on connect
                yield f"data: {json.dumps(session.status())}\n\n"
                while True:
                    try:
                        msg = q.get(timeout=30)
                        yield msg
                    except queue.Empty:
                        # Send keepalive comment to prevent timeout
                        yield ": keepalive\n\n"
            except (GeneratorExit, OSError, BrokenPipeError, ConnectionResetError):
                pass
            finally:
                broadcaster.unsubscribe(q)

        return Response(stream(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.route("/api/scan")
    def scan():
        """Discover Chromecast devices on the network."""
        try:
            return jsonify({"devices": registry.scan()})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/cast", methods=["POST"])
    def cast():
        """Resolve a URL to a Media Source and cast it."""
        data = request.json
        url = (data.get("url") or "").strip()
        device_uuid = (data.get("device_uuid") or "").strip()

        if not url:
            return jsonify({"error": "No URL provided"}), 400
        if not device_uuid:
            return jsonify({"error": "No device selected"}), 400

        session.note_attempt(url)
        registry.select(device_uuid)
        device_name = registry.name_for(device_uuid)
        if not device_name:
            return jsonify({"error": "Device not found. Try scanning again."}), 404

        # Direct stream URLs (m3u/m3u8/mp4) — skip yt-dlp
        url_lower = url.split("?")[0].lower()
        if url_lower.endswith((".m3u", ".m3u8", ".mp4", ".mkv", ".avi", ".webm")):
            source = MediaSource(
                stream_url=url,
                title=url.split("/")[-1].split("?")[0] or "Stream",
                mime="application/x-mpegURL" if url_lower.endswith((".m3u", ".m3u8")) else "video/mp4",
            )
        else:
            try:
                source = resolve_for_cast(url)
            except yt_dlp.utils.DownloadError as e:
                return jsonify({"error": f"yt-dlp: {e}"}), 500
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        start_time = parse_timestamp(url)
        try:
            session.start(source, device_name, start_time=start_time)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

        return jsonify({
            "status": "casting",
            "title": source.title,
            "thumbnail": source.thumbnail,
            "stream_url": source.preview_url or "",
            "start_time": start_time,
        })

    @app.route("/api/cast_file", methods=["POST"])
    def cast_file():
        """Upload a local file and cast it."""
        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        file = request.files["file"]
        device_uuid = request.form.get("device_uuid", "").strip()

        if not file.filename:
            return jsonify({"error": "No file selected"}), 400
        if not device_uuid:
            return jsonify({"error": "No device selected"}), 400

        filepath = os.path.join(UPLOAD_DIR, file.filename)
        file.save(filepath)

        # M3U/M3U8 playlists — read the stream URL from the file
        if file.filename.lower().endswith((".m3u", ".m3u8")):
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
            if not lines:
                return jsonify({"error": "No stream URL found in playlist file"}), 400
            source = MediaSource(stream_url=lines[0], title=file.filename,
                                 mime="application/x-mpegURL")
        else:
            source = MediaSource(
                stream_url=f"http://{get_lan_ip()}:5000/api/media",
                title=file.filename,
                mime=mimetypes.guess_type(filepath)[0] or "video/mp4",
                local_path=filepath,
            )

        session.note_attempt(file.filename)
        registry.select(device_uuid)
        device_name = registry.name_for(device_uuid)
        if not device_name:
            return jsonify({"error": "Device not found. Try scanning again."}), 404

        try:
            session.start(source, device_name)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

        return jsonify({
            "status": "casting",
            "title": source.title,
            "stream_url": source.preview_url or "",
        })

    @app.route("/api/play", methods=["POST"])
    def play():
        try:
            session.play()
        except NoActiveCast:
            return jsonify({"error": "No active cast"}), 400
        return jsonify({"status": "playing"})

    @app.route("/api/pause", methods=["POST"])
    def pause():
        try:
            session.pause()
        except NoActiveCast:
            return jsonify({"error": "No active cast"}), 400
        return jsonify({"status": "paused"})

    @app.route("/api/stop", methods=["POST"])
    def stop():
        try:
            session.stop()
        except NoActiveCast:
            return jsonify({"error": "No active cast"}), 400
        return jsonify({"status": "stopped"})

    @app.route("/api/volume", methods=["POST"])
    def volume():
        level = request.json.get("level")
        if level is None:
            return jsonify({"error": "No volume level provided"}), 400
        try:
            session.set_volume(level)
        except NoActiveCast:
            return jsonify({"error": "No active cast"}), 400
        return jsonify({"volume": max(0.0, min(1.0, float(level)))})

    @app.route("/api/seek", methods=["POST"])
    def seek():
        position = request.json.get("position")
        if position is None:
            return jsonify({"error": "No position provided"}), 400
        try:
            session.seek(position)
        except NoActiveCast:
            return jsonify({"error": "No active cast"}), 400
        return jsonify({"position": position})

    if start_workers:
        broadcaster.start()
        # Refresh device availability in background (don't block startup)
        threading.Thread(target=registry.startup_reconnect, daemon=True).start()

    return app


def configure_logging():
    """The app usually runs headless (hidden console, pythonw, dead ssh
    session) — warnings and errors go to a rotating file so failed casts
    stay diagnosable. A console handler is kept for dev runs."""
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(BASE_DIR, "videocast.log"),
        maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    file_handler.setLevel(logging.WARNING)
    handlers = [file_handler]
    if sys.stderr:  # absent under pythonw
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


if __name__ == "__main__":
    configure_logging()
    create_app().run(host="0.0.0.0", port=5000, debug=False, threaded=True)
