# video-cast

A single-user Flask app that casts videos (YouTube/yt-dlp URLs, direct streams, uploaded files) to a Chromecast, with a muted browser Preview kept in sync with the device over SSE. Deployed on a personal Windows PC; developed on a Mac with no real device.

## Language

**Cast Session**:
The backend's single unit of playback state: what's playing (media identity), on which device, at what position, whether the last stop was intentional, plus the policies that act on it (post-cast idle grace, auto-resume, persistence). There is at most one.
_Avoid_: global state, playback state, app state

**Cast Platform**:
The adapter seam in front of Chromecast reality: `discover()` and `connect(name) → Device`. Two adapters exist: pychromecast (prod) and the simulated platform (tests, sim_server).
_Avoid_: pychromecast wrapper, device layer

**Device**:
A connected Chromecast as seen through the Cast Platform: media commands, volume, status snapshots, and push status events (`on_status`). Status events are pushed, never polled — command confirmations must arrive as device-confirmed events (see No-Echo Rule).
_Avoid_: cast_device, chromecast object

**Device Registry**:
Knows which Chromecasts exist: scan, cached device list (persisted), friendly-name-by-UUID lookup. Does not connect to anything.
_Avoid_: device cache, discovery

**Status Broadcaster**:
Fans the Cast Session's status out to SSE subscribers; wakes on device events (`wake()`) or on its poll interval.
_Avoid_: SSE machinery, event bus

**Media Source**:
The resolved thing a cast plays: `{stream_url, title, thumbnail, mime}`, produced from a page URL (via yt-dlp), a direct stream URL, an uploaded file, or an m3u playlist. The Cast Session receives a `resolve_source(url) → Media Source` callable for auto-resume; the deep resolver is a planned separate module.
_Avoid_: stream info, video info

**Preview**:
The muted local `<video>` element mirroring the Device. Driven by the sync engine's actions; never the source of truth.
_Avoid_: local player, mirror

**Preview Controller**:
`preview-controller.js` — the pure, node-tested state machine owning every Preview decision: lifecycle (load/swap/teardown), autoplay-blocked fallback, native-event forwarding, user intents, display state. The page's single interface: events in, an actions object out (DOM writes and backend commands both); the page only reads snapshots and applies actions.
_Avoid_: page logic, wiring

**Sync Engine**:
`player-sync.js` — reconciles Device status against the Preview and pending user intents. An internal seam of the Preview Controller (separately tested); the page never calls it directly.
_Avoid_: player logic, reconciler

**Post-Cast Idle Grace**:
The window (15s) after `play_media` during which a Device-reported IDLE means "still loading", not "stopped". Exists on both sides of the SSE seam: the Cast Session gates auto-resume with it; the Sync Engine gates Preview teardown with it.
_Avoid_: idle timeout, loading window

**No-Echo Rule**:
Control endpoints never broadcast after sending a command — pychromecast's cached status is still pre-command, so broadcasting would echo stale state. The broadcast happens when the Device confirms (its listener fires); `update_status()` prompts slow devices to confirm.
_Avoid_: (don't paraphrase — this is a load-bearing invariant, name it)

## Flagged ambiguities

- **"casting in progress"** exists as both the server flag (suppresses auto-resume during setup) and the page flag (suppresses Preview teardown while the cast API call is in flight). Same phrase, two different windows on two sides of the seam. When speaking precisely, say *server cast-setup flag* vs *page cast-request flag*. Candidate under consideration: publish `loading` in the status interface and delete the page flag.

## Example dialogue

**Dev**: The Preview went black after I cast — bug in the Sync Engine?

**Expert**: First ask which side dropped it. If the Device reported IDLE within the Post-Cast Idle Grace and the Preview was torn down anyway, the page ignored the engine's `shouldClearOnIdle`. If the Cast Session auto-resumed mid-load, its grace gate failed server-side.

**Dev**: And if pause bounced back to playing for a second?

**Expert**: That's a No-Echo Rule violation — something broadcast before the Device confirmed. Check that no control endpoint sets the wake event directly; the Status Broadcaster should only fan out device-confirmed status.
