"""Run the app against a simulated Chromecast — no real device needed.

The simulated platform (sim_platform.py) is the second adapter at the Cast
Platform seam: devices confirm commands after a short delay (like a real
receiver) and playback position advances in real time, so the full UI
including preview sync can be exercised in a browser.

Usage:
    VIDEOCAST_STATE_FILE=/tmp/sim-state.json python tests/sim_server.py [port]

Then open http://127.0.0.1:<port>/ and cast the sample URL shown on startup.
"""
import os
import sys

if not os.environ.get("VIDEOCAST_STATE_FILE"):
    print("Refusing to run without VIDEOCAST_STATE_FILE (would clobber real state.json)")
    sys.exit(1)

# Start clean: leftover state from a previous run would preselect stale devices
if os.path.exists(os.environ["VIDEOCAST_STATE_FILE"]):
    os.remove(os.environ["VIDEOCAST_STATE_FILE"])

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app import create_app  # noqa: E402
from sim_platform import SimPlatform, FAKE_UUID  # noqa: E402

app = create_app(platform=SimPlatform())
app.videocast.registry.scan()  # seed the simulated device
app.videocast.registry.select(FAKE_UUID)


@app.route("/sample.mp4")
def sample_mp4():
    from flask import send_file
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.mp4"),
                     mimetype="video/mp4", conditional=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5058
    print(f"Sim ready — cast this URL: http://127.0.0.1:{port}/sample.mp4")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
