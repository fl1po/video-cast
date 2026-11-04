"""video-cast — Flask entry point.

create_app() wires the modules together; importing this module has no side
effects. Components live on app.videocast (session, registry, broadcaster,
platform, store, resolver) so tests reach them without monkeypatching globals.

    Cast Session   (session.py)      playback state + policies
    Media Source Resolver (mediasource.py) Media Ref -> Media Source
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
import socket
import sys
import threading
from types import SimpleNamespace

from flask import Flask, render_template, request, jsonify, Response, send_file
import requests

from devices import DeviceRegistry
from mediasource import MediaRef, MediaSourceResolver, YtDlpExtractor
from relay import parse_byte_range, relay_url, rewrite_hls_manifest
from session import CastSession, NoActiveCast
from sse import StatusBroadcaster
from store import StateStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")


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


# Connection reuse for the relay — one TLS handshake per googlevideo
# host instead of one per segment.
_relay_http = requests.Session()

# googlevideo cuts open-ended fetches after a ~2MB burst (anti-scraper
# pacing) but serves bounded Range windows at full speed — same trick as
# yt-dlp's --http-chunk-size. Every upstream fetch stays under this.
RELAY_CHUNK = 8 * 1024 * 1024

# The port the Device reaches this box on (see __main__).
PORT = 5000


def create_app(state_file=None, platform=None, extract=None, upload_dir=None,
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
    upload_dir = upload_dir or UPLOAD_DIR
    resolver = MediaSourceResolver(
        extract=extract or YtDlpExtractor(), upload_dir=upload_dir,
        # per call: the LAN IP can change while the app runs
        device_host=lambda: f"{get_lan_ip()}:{PORT}")

    store = StateStore(state_file)
    registry = DeviceRegistry(platform, store)
    session = CastSession(platform, resolver, store)
    broadcaster = StatusBroadcaster(session.status, poll_playing, poll_idle)
    session.wake = broadcaster.wake

    os.makedirs(upload_dir, exist_ok=True)

    app = Flask(__name__)
    app.videocast = SimpleNamespace(session=session, registry=registry,
                                    broadcaster=broadcaster, platform=platform,
                                    store=store, resolver=resolver)

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

    def cast_ref(ref_for, device_uuid):
        """Shared tail of both cast routes: the device is looked up before
        the ref is made (an upload is only saved for a known device)."""
        registry.select(device_uuid)
        device_name = registry.name_for(device_uuid)
        if not device_name:
            return jsonify({"error": "Device not found. Try scanning again."}), 404
        try:
            source = session.cast(ref_for(), device_name)
        except Exception as e:  # ResolveError messages are user-facing
            return jsonify({"error": str(e)}), 500
        return jsonify({
            "status": "casting",
            "title": source.title,
            "thumbnail": source.thumbnail,
            # the Preview's URL, not the Device's (wire name is legacy)
            "stream_url": source.preview_url or "",
            "start_time": source.start_time,
        })

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
        return cast_ref(lambda: MediaRef.url(url), device_uuid)

    @app.route("/api/cast_file", methods=["POST"])
    def cast_file():
        """Upload a local file (or an m3u playlist) and cast it."""
        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        file = request.files["file"]
        device_uuid = request.form.get("device_uuid", "").strip()
        if not file.filename:
            return jsonify({"error": "No file selected"}), 400
        if not device_uuid:
            return jsonify({"error": "No device selected"}), 400
        return cast_ref(lambda: resolver.save_upload(file.stream, file.filename),
                        device_uuid)

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
    create_app().run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
