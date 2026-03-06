import threading
import time
import json
import subprocess
import sys
import queue

import os

from flask import Flask, render_template, request, jsonify, Response
import pychromecast

app = Flask(__name__)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# Global state
chromecasts = []
cast_device = None
media_title = None
media_thumbnail = None
media_stream_url = None
last_url = ""
last_device_uuid = ""
cached_devices = []


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
            }, f)
    except Exception:
        pass


def load_state():
    """Restore state from disk."""
    global last_url, last_device_uuid, cached_devices, media_title, media_thumbnail
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        last_url = s.get("last_url", "")
        last_device_uuid = s.get("last_device_uuid", "")
        cached_devices = s.get("cached_devices", [])
        media_title = s.get("media_title")
        media_thumbnail = s.get("media_thumbnail")
    except Exception:
        pass


def try_reconnect():
    """Try to reconnect to the last used Chromecast on startup."""
    global cast_device
    if not last_device_uuid or not cached_devices:
        return
    # Find friendly name for the last device
    name = None
    for d in cached_devices:
        if d.get("uuid") == last_device_uuid:
            name = d.get("friendly_name")
            break
    if not name:
        return
    try:
        casts, browser = pychromecast.get_listed_chromecasts(friendly_names=[name])
        if casts:
            cast_device = casts[0]
            cast_device.wait(timeout=10)
            register_listeners(cast_device)
            # Check if it's actually playing something
            ms = cast_device.media_controller.status
            if ms.player_state in (None, "UNKNOWN", "IDLE"):
                pass  # connected but nothing playing — that's fine
        pychromecast.discovery.stop_discovery(browser)
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
            "stream_url": media_stream_url or "",
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


class StatusListener:
    """Listener for pychromecast cast device status changes."""
    def new_cast_status(self, status):
        status_changed.set()


class MediaStatusListener:
    """Listener for pychromecast media status changes."""
    def new_media_status(self, status):
        status_changed.set()


def register_listeners(cc):
    """Register status listeners on a cast device."""
    cc.register_status_listener(StatusListener())
    cc.media_controller.register_status_listener(MediaStatusListener())


def sse_broadcaster():
    """Background thread: pushes status to SSE clients.
    Wakes on pychromecast events or every 1s for time updates."""
    while True:
        # Wait up to 1s — wakes early if pychromecast fires an event
        status_changed.wait(timeout=1)
        status_changed.clear()
        broadcast_status()


# Start the SSE broadcaster thread
_broadcaster = threading.Thread(target=sse_broadcaster, daemon=True)
_broadcaster.start()

# Try reconnecting to last Chromecast on startup
try_reconnect()


@app.route("/")
def index():
    return render_template("index.html")


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
        except GeneratorExit:
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
        # Give discovery a moment to find devices
        time.sleep(3)
        services, browser = pychromecast.discovery.discover_chromecasts()
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


@app.route("/api/cast", methods=["POST"])
def cast():
    """Extract stream URL via yt-dlp and cast to selected Chromecast."""
    global cast_device, media_title, media_thumbnail, media_stream_url, last_url, last_device_uuid

    data = request.json
    url = data.get("url", "").strip()
    device_uuid = data.get("device_uuid", "").strip()

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not device_uuid:
        return jsonify({"error": "No device selected"}), 400

    last_url = url
    last_device_uuid = device_uuid

    # Find the selected device
    target_service = None
    for service in chromecasts:
        if str(service.uuid) == device_uuid:
            target_service = service
            break

    if not target_service:
        return jsonify({"error": "Device not found. Try scanning again."}), 404

    # Extract stream URL with yt-dlp
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "yt_dlp",
                "--no-warnings",
                "-f", "best[ext=mp4]/best",
                "--get-url",
                "--get-title",
                "--get-thumbnail",
                url,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return jsonify({"error": f"yt-dlp failed: {result.stderr.strip()}"}), 500

        lines = result.stdout.strip().split("\n")
        if len(lines) < 1:
            return jsonify({"error": "yt-dlp returned no output"}), 500

        video_title = lines[0] if len(lines) >= 1 else "Unknown"
        stream_url = lines[1] if len(lines) >= 2 else lines[0]
        thumbnail_url = lines[2] if len(lines) >= 3 else ""

        # If only one line, it's probably the URL
        if len(lines) == 1:
            stream_url = lines[0]
            video_title = "Unknown"
            thumbnail_url = ""

    except subprocess.TimeoutExpired:
        return jsonify({"error": "yt-dlp timed out"}), 504
    except FileNotFoundError:
        return jsonify({"error": "yt-dlp not found. Install with: pip install yt-dlp"}), 500

    # Connect to Chromecast (fresh connection to wake from sleep)
    try:
        # Disconnect stale connection if any
        if cast_device:
            try:
                cast_device.disconnect(timeout=5)
            except Exception:
                pass
            cast_device = None

        casts, browser = pychromecast.get_listed_chromecasts(
            friendly_names=[target_service.friendly_name]
        )
        if not casts:
            pychromecast.discovery.stop_discovery(browser)
            return jsonify({"error": "Could not connect to Chromecast"}), 500

        cast_device = casts[0]
        cast_device.wait(timeout=30)
        pychromecast.discovery.stop_discovery(browser)

        # Register listeners for SSE push
        register_listeners(cast_device)

        # Wake the device — quit any existing app so it's in a clean state
        if cast_device.app_id:
            cast_device.quit_app()
            time.sleep(2)

        media_title = video_title
        media_thumbnail = thumbnail_url
        media_stream_url = stream_url

        # Cast the stream
        mc = cast_device.media_controller
        mc.play_media(
            stream_url,
            "video/mp4",
            title=video_title,
            thumb=thumbnail_url,
        )
        mc.block_until_active(timeout=30)

        # Trigger an immediate SSE push
        status_changed.set()
        save_state()

        return jsonify({
            "status": "casting",
            "title": video_title,
            "thumbnail": thumbnail_url,
            "stream_url": stream_url,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/play", methods=["POST"])
def play():
    cc = cast_device
    if not cc:
        return jsonify({"error": "No active cast"}), 400
    cc.media_controller.play()
    status_changed.set()
    return jsonify({"status": "playing"})


@app.route("/api/pause", methods=["POST"])
def pause():
    cc = cast_device
    if not cc:
        return jsonify({"error": "No active cast"}), 400
    cc.media_controller.pause()
    status_changed.set()
    return jsonify({"status": "paused"})


@app.route("/api/stop", methods=["POST"])
def stop():
    global cast_device, media_title, media_thumbnail, media_stream_url
    cc = cast_device
    if not cc:
        return jsonify({"error": "No active cast"}), 400
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
    cc.set_volume(level)
    status_changed.set()
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
    cc.media_controller.seek(float(position))
    status_changed.set()
    return jsonify({"position": position})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
