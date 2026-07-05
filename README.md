# YT Chromecast Controller

Cast videos from YouTube and other video sites or local files to your Chromecast — ad-free — with a clean web remote.

Uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) to extract direct stream URLs (bypassing ads) and [pychromecast](https://github.com/home-assistant-libs/pychromecast) for device control.

> **Note:** Live streams (Twitch, YouTube Live, etc.) and DRM-protected content are not supported. This works best with on-demand videos that yt-dlp can extract a direct MP4 URL from.

## Setup

```
pip install -r requirements.txt
python app.py
```

> **Note:** `yt-dlp[default]` (not plain `yt-dlp`) is required — it includes `yt-dlp-ejs`, which solves YouTube's JS challenge. Without it most formats are unavailable and casting YouTube URLs fails with "Requested format is not available". A JS runtime (Node.js or Deno) must also be installed.

Open `http://localhost:5000` in your browser.

To access from other devices on your network (e.g. phone), use your PC's IP: `http://<your-ip>:5000`

## Usage

1. **Scan** — click the magnifying glass to discover Chromecast devices on your network
2. **Select** a device from the dropdown
3. **Paste a URL** or **pick a local file** (folder icon), then click **Cast**
4. Control playback with the overlay controls or keyboard shortcuts

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| Space / K | Play / Pause |
| Left Arrow | Rewind 5s |
| Right Arrow | Forward 5s |
| J | Rewind 10s |
| L | Forward 10s |
| Up Arrow | Volume up |
| Down Arrow | Volume down |
| M | Mute / Unmute |
| F | Fullscreen |
| Shift+S | Stop |
| 0–9 | Jump to 0%–90% |
| Home / End | Jump to start / end |

## Features

- Supports many sites via yt-dlp (YouTube, Vimeo, Twitter, Reddit, etc. — any site with downloadable video)
- Local file casting (mp4, webm, mkv, avi, mov)
- URL timestamps supported (`?t=120`, `?t=2m30s`)
- YouTube-style overlay controls with idle auto-hide
- Real-time status via Server-Sent Events
- Auto-resume after TV sleep/wake
- State persists across page refreshes and server restarts
- Multi-device sync — control from desktop and phone simultaneously
- Mobile-friendly with touch support and double-tap fullscreen

## Tests

Backend (Flask + SSE broadcast behavior, faked Chromecast):

```
pip install pytest
python -m pytest tests/
```

Frontend sync engine (`static/js/player-sync.js` — reconciles Chromecast status with the local preview):

```
node --test 'tests/*.test.mjs'
```

Manual testing without a real Chromecast — a simulated device that confirms
commands with realistic latency:

```
VIDEOCAST_STATE_FILE=/tmp/sim-state.json python tests/sim_server.py 5058
```

## Auto-Start (Windows)

Double-click `start.vbs` to run the server in the background, or place it in `shell:startup` to launch on login.

## Requirements

- Python 3.7+
- Node.js (or Deno) — used by yt-dlp to solve YouTube's JS challenge
- Chromecast on the same local network
- `flask`, `pychromecast`, `yt-dlp[default]` (see `requirements.txt`)
