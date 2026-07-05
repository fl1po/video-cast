import threading
import time
import json
import queue
import re
import os
import socket
import mimetypes
from urllib.parse import urlparse, parse_qs

from flask import Flask, render_template, request, jsonify, Response, send_file
import pychromecast
import yt_dlp

# Ensure node is on PATH for yt-dlp JS challenge solving
for _node_path in [
    r"C:\Program Files\nodejs",
    os.path.join(os.path.expanduser("~"), ".deno", "bin"),
]:
    if os.path.isdir(_node_path) and _node_path not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _node_path + os.pathsep + os.environ.get("PATH", "")

app = Flask(__name__)

STATE_FILE = os.environ.get("VIDEOCAST_STATE_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "state.json")

# SSE broadcast poll intervals (seconds); overridable for tests
SSE_POLL_PLAYING = float(os.environ.get("VIDEOCAST_SSE_POLL_PLAYING", "2"))
SSE_POLL_IDLE = float(os.environ.get("VIDEOCAST_SSE_POLL_IDLE", "5"))


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


# Global state
chromecasts = []
local_file_path = None  # path to local file being served
cast_device = None
media_title = None
media_thumbnail = None
media_stream_url = None
last_url = ""
last_device_uuid = ""
cached_devices = []
last_position = 0        # last known playback position
intentional_stop = False  # True when user clicks Stop
_casting_in_progress = False  # True while a new cast is being set up

YT_DLP_FORMAT = "22/18/best[height<=720]/best"
YT_DLP_OPTS = {
    "format": YT_DLP_FORMAT,
    "quiet": True,
    "no_warnings": True,
    "cookiesfrombrowser": ("firefox",),
    "js_runtimes": {"node": {}},
}


def get_device_name(device_uuid):
    """Find device friendly name by UUID from cache or live services."""
    for d in cached_devices:
        if d.get("uuid") == device_uuid:
            return d.get("friendly_name")
    for service in chromecasts:
        if str(service.uuid) == device_uuid:
            return service.friendly_name
    return None


def connect_to_device(device_name):
    """Clean up old connection and establish a fresh one. Returns (device, error_msg)."""
    global cast_device
    if cast_device:
        try:
            cast_device.quit_app()
            time.sleep(2)
            cast_device.disconnect(timeout=5)
        except Exception:
            pass
        cast_device = None
        time.sleep(3)

    casts, browser = pychromecast.get_listed_chromecasts(friendly_names=[device_name])
    if not casts:
        pychromecast.discovery.stop_discovery(browser)
        return None, "Could not connect to Chromecast. Is it on?"

    cast_device = casts[0]
    cast_device.wait(timeout=30)
    pychromecast.discovery.stop_discovery(browser)
    register_listeners(cast_device)
    return cast_device, None


def save_state():
    """Persist key state to disk so it survives restarts."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "last_url": last_url,
                "last_device_uuid": last_device_uuid,
                "cached_devices": cached_devices,
                "media_title": media_title,
                "media_thumbnail": media_thumbnail,
                "media_stream_url": media_stream_url,
                "last_position": last_position,
            }, f)
    except Exception:
        pass


def load_state():
    """Restore state from disk."""
    global last_url, last_device_uuid, cached_devices, media_title, media_thumbnail, media_stream_url, last_position
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        last_url = s.get("last_url", "")
        last_device_uuid = s.get("last_device_uuid", "")
        cached_devices = s.get("cached_devices", [])
        media_title = s.get("media_title")
        media_thumbnail = s.get("media_thumbnail")
        media_stream_url = s.get("media_stream_url")
        last_position = s.get("last_position", 0)
    except Exception:
        pass


def try_reconnect():
    """Discover available Chromecasts on startup (does NOT connect — avoids putting device in bad state)."""
    global chromecasts, last_device_uuid, cached_devices
    if not last_device_uuid or not cached_devices:
        return
    try:
        services, browser = pychromecast.discovery.discover_chromecasts()
        time.sleep(5)
        pychromecast.discovery.stop_discovery(browser)
        chromecasts = services

        # Update cached_devices to only include available devices
        available = []
        for service in services:
            available.append({
                "uuid": str(service.uuid),
                "friendly_name": service.friendly_name,
                "host": service.host,
                "port": service.port,
                "model_name": service.model_name,
            })
        cached_devices = available

        # Check if last device is still on the network
        found = any(str(s.uuid) == last_device_uuid for s in services)
        if not found:
            last_device_uuid = ""

        save_state()
    except Exception:
        pass


load_state()

# SSE: list of subscriber queues
sse_subscribers = []
sse_lock = threading.Lock()

# Event that fires on any pychromecast status change
status_changed = threading.Event()


def build_status():
    """Build the current status dict."""
    cc = cast_device
    if not cc:
        return {"state": "IDLE", "connected": False}
    try:
        mc = cc.media_controller
        ms = mc.status
        return {
            "connected": True,
            "state": ms.player_state or "UNKNOWN",
            "title": media_title or ms.title or "",
            "thumbnail": media_thumbnail or "",
            "stream_url": media_stream_url if media_stream_url and "m3u8" not in (media_stream_url or "") else "",
            "current_time": ms.adjusted_current_time or ms.current_time or 0,
            "duration": ms.duration or 0,
            "volume": cc.status.volume_level if cc.status else 0,
            "muted": cc.status.volume_muted if cc.status else False,
        }
    except Exception:
        return {"state": "IDLE", "connected": False}


def broadcast_status():
    """Push current status to all SSE subscribers."""
    data = build_status()
    msg = f"data: {json.dumps(data)}\n\n"
    with sse_lock:
        dead = []
        for q in sse_subscribers:
            try:
                # Drop old messages if client is slow
                while not q.empty():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            sse_subscribers.remove(q)


_resume_lock = threading.Lock()
_last_resume_attempt = 0


def extract_stream_url(url):
    """Re-extract a fresh stream URL from the original video URL using yt-dlp."""
    try:
        with yt_dlp.YoutubeDL(dict(YT_DLP_OPTS)) as ydl:
            info = ydl.extract_info(url, download=False)
            info = ydl.sanitize_info(info)
        protocol = info.get("protocol", "")
        mime = "application/x-mpegURL" if "m3u8" in protocol else "video/mp4"
        return info.get("url", ""), info.get("title", "Unknown"), info.get("thumbnail", ""), mime
    except Exception:
        return None, None, None, None


def auto_resume():
    """Re-cast the last stream and seek to saved position after TV wakes."""
    global cast_device, intentional_stop, _last_resume_attempt, media_stream_url, media_title, media_thumbnail, _casting_in_progress
    if not _resume_lock.acquire(blocking=False):
        return  # another resume already in progress
    _casting_in_progress = True
    try:
        # Cooldown: don't retry within 30 seconds
        now = time.time()
        if now - _last_resume_attempt < 30:
            return
        _last_resume_attempt = now

        # Wait a moment for the device to fully wake
        time.sleep(5)
        cc = cast_device
        if not cc or not last_url:
            return
        # Check if still idle (wasn't manually restarted)
        try:
            state = cc.media_controller.status.player_state
            if state in ("PLAYING", "BUFFERING", "PAUSED"):
                return  # already recovered on its own
        except Exception:
            pass

        # Re-extract fresh stream URL (old one likely expired)
        stream_url, title, thumb, mime = extract_stream_url(last_url)
        if not stream_url:
            intentional_stop = True
            return

        media_stream_url = stream_url
        media_title = title
        media_thumbnail = thumb

        # Re-cast
        mc = cc.media_controller
        mc.play_media(
            stream_url,
            mime or "video/mp4",
            title=title or "",
            thumb=thumb or "",
        )
        mc.block_until_active(timeout=30)
        # Seek to last position
        if last_position > 0:
            mc.seek(last_position)
        status_changed.set()
        save_state()
    except Exception:
        # Stop retrying on failure
        intentional_stop = True
    finally:
        _casting_in_progress = False
        _resume_lock.release()


class StatusListener:
    """Listener for pychromecast cast device status changes."""
    def new_cast_status(self, status):
        status_changed.set()


class MediaStatusListener:
    """Listener for pychromecast media status changes."""
    def new_media_status(self, status):
        global last_position, intentional_stop
        # Track position while playing
        if status.player_state in ("PLAYING", "PAUSED", "BUFFERING"):
            pos = status.adjusted_current_time or status.current_time
            if pos:
                last_position = pos
                intentional_stop = False
        # Auto-resume if cast went idle unexpectedly (TV sleep/wake)
        # But NOT if video ended naturally (position near duration)
        elif status.player_state == "IDLE" and not intentional_stop and not _casting_in_progress and last_url:
            duration = status.duration or 0
            if duration > 0 and last_position > 0 and (duration - last_position) < 5:
                intentional_stop = True  # video finished naturally
            else:
                threading.Thread(target=auto_resume, daemon=True).start()
        status_changed.set()


def register_listeners(cc):
    """Register status listeners on a cast device."""
    cc.register_status_listener(StatusListener())
    cc.media_controller.register_status_listener(MediaStatusListener())


def sse_broadcaster():
    """Background thread: pushes status to SSE clients.
    Wakes on pychromecast events or periodically for time updates."""
    while True:
        # Poll faster during playback (2s), slower when idle (5s)
        cc = cast_device
        if cc:
            try:
                state = cc.media_controller.status.player_state
                timeout = SSE_POLL_PLAYING if state == "PLAYING" else SSE_POLL_IDLE
            except Exception:
                timeout = SSE_POLL_IDLE
        else:
            timeout = SSE_POLL_IDLE
        status_changed.wait(timeout=timeout)
        status_changed.clear()
        broadcast_status()


# Start the SSE broadcaster thread
_broadcaster = threading.Thread(target=sse_broadcaster, daemon=True)
_broadcaster.start()

# Try reconnecting to last Chromecast in background (don't block startup)
threading.Thread(target=try_reconnect, daemon=True).start()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/media")
def serve_media():
    """Serve the current local file for Chromecast to stream."""
    if not local_file_path or not os.path.isfile(local_file_path):
        return jsonify({"error": "No file"}), 404
    mime = mimetypes.guess_type(local_file_path)[0] or "video/mp4"
    return send_file(local_file_path, mimetype=mime, conditional=True)


@app.route("/api/state")
def state():
    """Return last used URL and device for syncing across clients."""
    status = build_status()
    return jsonify({
        "url": last_url,
        "device_uuid": last_device_uuid,
        "devices": cached_devices,
        "playback": status,
    })


@app.route("/api/events")
def events():
    """SSE endpoint — streams status updates to the client."""
    def stream():
        q = queue.Queue(maxsize=5)
        with sse_lock:
            sse_subscribers.append(q)
        try:
            # Send current status immediately on connect
            data = build_status()
            yield f"data: {json.dumps(data)}\n\n"
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
            with sse_lock:
                if q in sse_subscribers:
                    sse_subscribers.remove(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/scan")
def scan():
    """Discover Chromecast devices on the network."""
    global chromecasts, cached_devices
    try:
        services, browser = pychromecast.discovery.discover_chromecasts()
        time.sleep(5)
        pychromecast.discovery.stop_discovery(browser)

        chromecasts_found = []
        for service in services:
            chromecasts_found.append({
                "uuid": str(service.uuid),
                "friendly_name": service.friendly_name,
                "host": service.host,
                "port": service.port,
                "model_name": service.model_name,
            })
        chromecasts = services
        cached_devices = chromecasts_found
        save_state()
        return jsonify({"devices": chromecasts_found})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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


@app.route("/api/cast", methods=["POST"])
def cast():
    """Extract stream URL via yt-dlp and cast to selected Chromecast."""
    global cast_device, media_title, media_thumbnail, media_stream_url, last_url, last_device_uuid, intentional_stop, last_position, _casting_in_progress

    data = request.json
    url = data.get("url", "").strip()
    device_uuid = data.get("device_uuid", "").strip()

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not device_uuid:
        return jsonify({"error": "No device selected"}), 400

    last_url = url
    last_device_uuid = device_uuid

    device_name = get_device_name(device_uuid)
    if not device_name:
        return jsonify({"error": "Device not found. Try scanning again."}), 404

    # Direct stream URLs (m3u/m3u8/mp4) — skip yt-dlp
    url_lower = url.split("?")[0].lower()
    if url_lower.endswith((".m3u", ".m3u8", ".mp4", ".mkv", ".avi", ".webm")):
        stream_url = url
        video_title = url.split("/")[-1].split("?")[0] or "Stream"
        thumbnail_url = ""
        stream_mime = "application/x-mpegURL" if url_lower.endswith((".m3u", ".m3u8")) else "video/mp4"
    else:
        # Extract stream URL with yt-dlp Python API
        try:
            with yt_dlp.YoutubeDL(dict(YT_DLP_OPTS)) as ydl:
                info = ydl.extract_info(url, download=False)
                info = ydl.sanitize_info(info)
            video_title = info.get("title", "Unknown")
            stream_url = info.get("url", "")
            thumbnail_url = info.get("thumbnail", "")
            protocol = info.get("protocol", "")
            stream_mime = "application/x-mpegURL" if "m3u8" in protocol else "video/mp4"

            if not stream_url:
                return jsonify({"error": "yt-dlp returned no stream URL"}), 500

        except yt_dlp.utils.DownloadError as e:
            return jsonify({"error": f"yt-dlp: {e}"}), 500
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    try:
        _casting_in_progress = True
        cc, err = connect_to_device(device_name)
        if err:
            _casting_in_progress = False
            return jsonify({"error": err}), 500

        intentional_stop = False
        last_position = 0
        media_title = video_title
        media_thumbnail = thumbnail_url
        media_stream_url = stream_url

        mc = cc.media_controller
        mc.play_media(stream_url, stream_mime, title=video_title, thumb=thumbnail_url)
        mc.block_until_active(timeout=30)
        _casting_in_progress = False

        # Seek to timestamp if present in URL
        start_time = parse_timestamp(url)

        def seek_when_ready(controller, pos):
            for _ in range(60):
                try:
                    state = controller.status.player_state
                    if state in ("PLAYING", "BUFFERING", "PAUSED"):
                        controller.seek(pos)
                        status_changed.set()
                        return
                except Exception:
                    pass
                time.sleep(0.5)

        if start_time > 0:
            threading.Thread(target=seek_when_ready, args=(mc, start_time), daemon=True).start()

        # Trigger an immediate SSE push
        status_changed.set()
        save_state()

        return jsonify({
            "status": "casting",
            "title": video_title,
            "thumbnail": thumbnail_url,
            "stream_url": stream_url if stream_mime == "video/mp4" else "",
            "start_time": start_time,
        })
    except Exception as e:
        _casting_in_progress = False
        return jsonify({"error": str(e)}), 500


UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


@app.route("/api/cast_file", methods=["POST"])
def cast_file():
    """Upload a local file and cast it to Chromecast."""
    global cast_device, media_title, media_thumbnail, media_stream_url, last_url, last_device_uuid, local_file_path, intentional_stop, last_position, _casting_in_progress

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    device_uuid = request.form.get("device_uuid", "").strip()

    if not file.filename:
        return jsonify({"error": "No file selected"}), 400
    if not device_uuid:
        return jsonify({"error": "No device selected"}), 400

    # Save uploaded file
    filepath = os.path.join(UPLOAD_DIR, file.filename)
    file.save(filepath)

    # M3U/M3U8 playlists — read the stream URL from the file
    if file.filename.lower().endswith((".m3u", ".m3u8")):
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        if not lines:
            return jsonify({"error": "No stream URL found in playlist file"}), 400
        stream_url = lines[0]
        video_title = file.filename
        thumbnail_url = ""
        mime = "application/x-mpegURL"
    else:
        local_file_path = filepath
        video_title = file.filename
        thumbnail_url = ""
        lan_ip = get_lan_ip()
        stream_url = f"http://{lan_ip}:5000/api/media"
        mime = mimetypes.guess_type(filepath)[0] or "video/mp4"

    last_url = file.filename
    last_device_uuid = device_uuid

    device_name = get_device_name(device_uuid)
    if not device_name:
        return jsonify({"error": "Device not found. Try scanning again."}), 404

    try:
        _casting_in_progress = True
        cc, err = connect_to_device(device_name)
        if err:
            _casting_in_progress = False
            return jsonify({"error": err}), 500

        intentional_stop = False
        last_position = 0
        media_title = video_title
        media_thumbnail = thumbnail_url
        media_stream_url = stream_url

        mc = cc.media_controller
        mc.play_media(stream_url, mime, title=video_title)
        mc.block_until_active(timeout=30)
        _casting_in_progress = False

        status_changed.set()
        save_state()

        return jsonify({
            "status": "casting",
            "title": video_title,
            "stream_url": stream_url,
        })
    except Exception as e:
        _casting_in_progress = False
        return jsonify({"error": str(e)}), 500


@app.route("/api/play", methods=["POST"])
def play():
    cc = cast_device
    if not cc:
        return jsonify({"error": "No active cast"}), 400
    # See /api/pause: broadcast happens when the device confirms, not now.
    cc.media_controller.play()
    cc.media_controller.update_status()
    return jsonify({"status": "playing"})


@app.route("/api/pause", methods=["POST"])
def pause():
    cc = cast_device
    if not cc:
        return jsonify({"error": "No active cast"}), 400
    # No status_changed.set() here: pychromecast's cached status is still the
    # pre-command state, so broadcasting now would echo stale data to clients.
    # The media status listener fires (and broadcasts) when the device confirms;
    # update_status (GET_STATUS) prompts that reply from devices that are slow
    # to push it on their own.
    cc.media_controller.pause()
    cc.media_controller.update_status()
    return jsonify({"status": "paused"})


@app.route("/api/stop", methods=["POST"])
def stop():
    global cast_device, media_title, media_thumbnail, media_stream_url, intentional_stop, last_position
    cc = cast_device
    if not cc:
        return jsonify({"error": "No active cast"}), 400
    intentional_stop = True
    last_position = 0
    cc.media_controller.stop()
    media_title = None
    media_thumbnail = None
    media_stream_url = None
    status_changed.set()
    save_state()
    return jsonify({"status": "stopped"})


@app.route("/api/volume", methods=["POST"])
def volume():
    cc = cast_device
    if not cc:
        return jsonify({"error": "No active cast"}), 400
    data = request.json
    level = data.get("level")
    if level is None:
        return jsonify({"error": "No volume level provided"}), 400
    level = max(0.0, min(1.0, float(level)))
    # See /api/pause: the cast status listener broadcasts once the device
    # reports the new volume.
    cc.set_volume(level)
    return jsonify({"volume": level})


@app.route("/api/seek", methods=["POST"])
def seek():
    cc = cast_device
    if not cc:
        return jsonify({"error": "No active cast"}), 400
    data = request.json
    position = data.get("position")
    if position is None:
        return jsonify({"error": "No position provided"}), 400
    mc = cc.media_controller
    try:
        was_playing = mc.status and mc.status.player_state in ("PLAYING", "BUFFERING")
    except Exception:
        was_playing = False
    mc.seek(float(position))
    # Some Chromecasts pause after seek — resume playback, but only if it was
    # playing before the seek (a seek while paused must stay paused)
    time.sleep(0.3)
    try:
        if was_playing and mc.status and mc.status.player_state in ("PAUSED", "BUFFERING"):
            mc.play()
    except Exception:
        pass
    # See /api/pause: broadcast happens when the device confirms, not now.
    mc.update_status()
    return jsonify({"position": position})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
