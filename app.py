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
import mimetypes
import os
import queue
import re
import socket
import threading
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs

from flask import Flask, render_template, request, jsonify, Response, send_file
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

YT_DLP_FORMAT = "22/18/best[height<=720]/best"
YT_DLP_OPTS = {
    "format": YT_DLP_FORMAT,
    "quiet": True,
    "no_warnings": True,
    "cookiesfrombrowser": ("firefox",),
    "js_runtimes": {"node": {}},
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
    )


def create_app(state_file=None, platform=None, resolve_source=None,
               poll_playing=None, poll_idle=None, start_workers=True):
    state_file = state_file or os.environ.get("VIDEOCAST_STATE_FILE") \
        or os.path.join(BASE_DIR, "state.json")
    if poll_playing is None:
        poll_playing = float(os.environ.get("VIDEOCAST_SSE_POLL_PLAYING", "2"))
    if poll_idle is None:
        poll_idle = float(os.environ.get("VIDEOCAST_SSE_POLL_IDLE", "5"))
    if platform is None:
        from castplatform import PychromecastPlatform
        platform = PychromecastPlatform()
    resolve_source = resolve_source or resolve_with_ytdlp

    store = StateStore(state_file)
    registry = DeviceRegistry(platform, store)
    session = CastSession(platform, resolve_source, store)
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
                source = resolve_source(url)
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
            "stream_url": source.stream_url if source.mime == "video/mp4" else "",
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
            "stream_url": source.stream_url,
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


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=5000, debug=False, threaded=True)
