import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { createPreviewController } = require("../static/js/preview-controller.js");

// Helpers — a full SSE status and a local <video> snapshot.
const activeStatus = (t = 100, extra = {}) => ({
  connected: true, state: "PLAYING", title: "Video", thumbnail: "",
  stream_url: "http://cdn/v.mp4", current_time: t, duration: 600,
  volume: 0.5, muted: false, ...extra,
});
const idleStatus = (extra = {}) => ({ connected: true, state: "IDLE", ...extra });
const local = (t = 100, extra = {}) => ({
  currentTime: t, paused: false, ended: false, seeking: false, readyState: 4, error: null, ...extra,
});
const pausedLocal = (t = 100) => local(t, { paused: true });

// A controller that just cast a video with a preview URL.
function castCtrl(now = 10_000) {
  const ctrl = createPreviewController();
  ctrl.castRequested(now - 100);
  ctrl.castStarted({ stream_url: "http://cdn/v.mp4", title: "Video", start_time: 0 }, now);
  return ctrl;
}

// ── cast lifecycle ─────────────────────────────────────────────────────────

test("castStarted wires the preview: src, autoplay-on-ready, section, title", () => {
  const ctrl = createPreviewController();
  ctrl.castRequested(9_900);
  const act = ctrl.castStarted(
    { stream_url: "http://cdn/v.mp4", title: "Video", thumbnail: "th.jpg", start_time: 90 },
    10_000,
  );
  assert.equal(act.setSrc, "http://cdn/v.mp4");
  assert.equal(act.poster, "th.jpg");
  assert.equal(act.playWhenReady, true);
  assert.equal(act.title, "Video");
  assert.equal(act.showSection, true);
  assert.equal(act.seekTo, 90, "URL timestamp is applied to the preview too");
});

test("cast without a preview URL (m3u8) still arms the post-cast idle grace", () => {
  const ctrl = createPreviewController();
  ctrl.castRequested(9_900);
  const act = ctrl.castStarted({ title: "Live" }, 10_000);
  assert.equal(act.setSrc, undefined);
  const idle = ctrl.handleStatus(idleStatus(), local(0), 12_000);
  assert.ok(!idle.hideSection, "transient IDLE within the grace must not tear down");
});

test("cast failure hides the section only when no earlier stream is loaded", () => {
  const ctrl = createPreviewController();
  ctrl.castRequested(10_000);
  assert.equal(ctrl.castFailed(10_500).hideSection, true);

  const withStream = castCtrl(20_000);
  withStream.castRequested(30_000);
  assert.equal(withStream.castFailed(30_500).hideSection, false,
    "an existing preview survives a failed re-cast");
});

// ── teardown gate (the black-until-refresh bug) ────────────────────────────

test("transient post-cast IDLE does not tear the preview down; expiry does", () => {
  const ctrl = castCtrl(10_000);
  const during = ctrl.handleStatus(idleStatus(), local(0), 12_000);
  assert.ok(!during.clearSrc && !during.hideSection, "receiver still loading");

  const expired = ctrl.handleStatus(idleStatus(), local(0), 26_000);
  assert.equal(expired.clearSrc, true, "cast never started -> real stop");
  assert.equal(expired.hideSection, true);
});

test("IDLE while the cast API call is in flight does not tear down", () => {
  const ctrl = createPreviewController();
  ctrl.castRequested(10_000); // no noteCast yet — only this flag protects us
  const act = ctrl.handleStatus(idleStatus(), local(0), 10_500);
  assert.ok(!act.hideSection && !act.clearSrc);
});

test("a real stop (seen-active cast goes IDLE) tears down everywhere", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(5), local(5), 13_000); // receiver started
  const act = ctrl.handleStatus(idleStatus(), local(5), 14_000);
  assert.equal(act.clearSrc, true);
  assert.equal(act.hideSection, true);
  assert.equal(act.chip, false);
  assert.equal(act.statusText, "Idle");
});

test("disconnect tears down and reports 'Not connected'", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(5), local(5), 13_000);
  const act = ctrl.handleStatus({ connected: false, state: "IDLE" }, local(5), 14_000);
  assert.equal(act.clearSrc, true);
  assert.equal(act.statusText, "Not connected");
});

// ── stream swap ────────────────────────────────────────────────────────────

test("a new stream URL from the server swaps the preview; the same URL does not", () => {
  const ctrl = castCtrl(10_000);
  const same = ctrl.handleStatus(activeStatus(5), local(5), 13_000);
  assert.equal(same.setSrc, undefined, "no reload on every status");

  const swapped = ctrl.handleStatus(
    activeStatus(0, { stream_url: "http://cdn/other.mp4" }), local(5), 14_000);
  assert.equal(swapped.setSrc, "http://cdn/other.mp4");
  assert.equal(swapped.play, true, "server is PLAYING -> start the new preview");
});

test("a window that never cast this session shows status but does not load the stream", () => {
  const ctrl = createPreviewController();
  const act = ctrl.handleStatus(activeStatus(100), pausedLocal(100), 10_000);
  assert.equal(act.setSrc, undefined, "must not compete for the CDN session");
  assert.ok(!act.play, "and must not drive the (empty) element");
  assert.equal(act.showSection, true, "the section still reflects what's playing");
  assert.equal(act.displayState, "PLAYING");
});

// ── user intents ───────────────────────────────────────────────────────────

test("userPlayPause: optimistic state + command; stale echo does not bounce it", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(100), local(100), 13_000);

  const act = ctrl.userPlayPause(13_500);
  assert.equal(act.displayState, "PAUSED");
  assert.equal(act.pause, true);
  assert.deepEqual(act.command, { name: "pause" });

  // Stale SSE echo still says PLAYING — the display must hold the intent.
  const echo = ctrl.handleStatus(activeStatus(100.5), pausedLocal(100), 13_800);
  assert.equal(echo.displayState, "PAUSED");
  assert.ok(!echo.play, "must not un-pause the preview");
});

test("userSeekBy bases on the cast clock and clamps to [0, duration]", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(100), local(97), 13_000); // preview drifted to 97

  const act = ctrl.userSeekBy(10, local(97), 13_000);
  assert.equal(act.command.body.position, 110, "cast clock (100), not the drifted preview (97)");
  assert.equal(act.seekTo, 110);
  assert.equal(act.progress.time, 110);

  assert.equal(ctrl.userSeekBy(9_999, local(97), 13_000).command.body.position, 600);
  assert.equal(ctrl.userSeekBy(-9_999, local(97), 13_000).command.body.position, 0);
});

test("seeking an errored preview still commands the Chromecast, but not the element", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(100), local(100), 13_000);
  const act = ctrl.userSeekTo(300, local(100, { error: {} }), 13_500);
  assert.deepEqual(act.command, { name: "seek", body: { position: 300 } });
  assert.equal(act.seekTo, undefined);
});

test("userStop tears down and later statuses stop driving the element", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(100), local(100), 13_000);

  const act = ctrl.userStop(14_000);
  assert.equal(act.clearSrc, true);
  assert.equal(act.hideSection, true);
  assert.equal(act.chip, false);
  assert.deepEqual(act.command, { name: "stop" });

  // The device may echo PLAYING once more before it confirms the stop.
  const echo = ctrl.handleStatus(activeStatus(101), pausedLocal(0), 14_200);
  assert.ok(!echo.play && !echo.setSrc, "stopped session must not restart the preview");
});

// ── autoplay-blocked fallback ──────────────────────────────────────────────

test("tick steps the paused preview to the extrapolated cast position", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(100), pausedLocal(100), 10_000);
  assert.equal(ctrl.tick(pausedLocal(100), 12_000).seekTo, 102);
});

test("tick is inert while playing, seeking, unready, or with no stream", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(100), pausedLocal(100), 10_000);
  assert.deepEqual(ctrl.tick(local(100), 12_000), {}, "already playing");
  assert.deepEqual(ctrl.tick(local(100, { paused: true, seeking: true }), 12_000), {});
  assert.deepEqual(ctrl.tick(pausedLocal(100).readyState === 0 ? pausedLocal(100) : { ...pausedLocal(100), readyState: 0 }, 12_000), {});

  const noCast = createPreviewController();
  noCast.handleStatus(activeStatus(100), pausedLocal(100), 10_000);
  assert.deepEqual(noCast.tick(pausedLocal(100), 12_000), {}, "never cast this session");
});

test("playRefused shows the chip; playing hides it", () => {
  const ctrl = castCtrl(10_000);
  assert.equal(ctrl.playRefused(11_000).chip, true);
  assert.equal(ctrl.playing(12_000).chip, false);
});

test("a fresh cast withholds autoplay until the device confirms PLAYING, then starts in sync", () => {
  const ctrl = createPreviewController();
  ctrl.castRequested(9_900);
  const started = ctrl.castStarted({ stream_url: "http://cdn/v.mp4", title: "Video", start_time: 0 }, 10_000);
  assert.equal(started.setSrc, "http://cdn/v.mp4", "preview still loads immediately");
  assert.equal(started.playWhenReady, undefined, "but must not autoplay on canplay");

  // The receiver is still connecting/buffering -- must not start the preview
  // even though it has long since finished loading locally.
  const buffering = ctrl.handleStatus(
    activeStatus(0, { state: "BUFFERING" }), pausedLocal(0), 10_500);
  assert.equal(buffering.play, undefined);

  // The device finally confirms PLAYING, already 1.4s in (normal cast
  // startup latency) -- well under DRIFT_THRESHOLD_S(3), the gap that used
  // to slip through uncorrected because playWhenReady had already started
  // the preview from 0 with nothing to reconcile it against.
  const confirmed = ctrl.handleStatus(activeStatus(1.4), pausedLocal(0), 12_800);
  assert.equal(confirmed.seekTo, 1.4, "preview starts from the real position, not 0");
  assert.equal(confirmed.play, true);

  // Once resolved, later statuses go through the normal mirror/snap path.
  const steady = ctrl.handleStatus(activeStatus(2.4), local(2.4), 13_800);
  assert.equal(steady.seekTo, undefined);
  assert.equal(steady.play, undefined);
});

test("playStarted corrects the head start so it doesn't become permanent lag", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(100), pausedLocal(100), 10_000);
  // play() resolved 4s later: playback begins at the queued 100, cast is at 104.
  assert.equal(ctrl.playStarted(local(100), 14_000).seekTo, 104);
  // In-sync start needs no correction.
  assert.equal(ctrl.playStarted(local(104), 14_000).seekTo, undefined);
});

test("a gesture unlocks playback from the live cast position", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(100), pausedLocal(100), 10_000);

  const act = ctrl.gesture(pausedLocal(100), 14_000);
  assert.equal(act.play, true);
  assert.equal(act.seekTo, 104, "buffer from live, not the last stepped frame");

  const noMeta = ctrl.gesture({ ...pausedLocal(100), readyState: 0 }, 14_000);
  assert.equal(noMeta.play, true);
  assert.equal(noMeta.seekTo, undefined, "cannot seek before metadata");
});

test("gestures are inert when nothing needs unlocking", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(100), pausedLocal(100), 10_000);
  ctrl.userPlayPause(11_000); // user chose PAUSED
  assert.deepEqual(ctrl.gesture(pausedLocal(100), 12_000), {});
});

test("visibilityRestored resumes only a preview that should be playing", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(100), local(100), 13_000);
  assert.equal(ctrl.visibilityRestored(pausedLocal(100), 14_000).play, true);
  assert.deepEqual(ctrl.visibilityRestored(local(100), 14_000), {}, "already playing");
});

// ── native <video> events (iOS fullscreen controls) ────────────────────────

test("native pause is forwarded only from fullscreen", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(100), local(100), 13_000);

  assert.deepEqual(ctrl.nativePause({ fullscreen: false }, 14_000), {});

  const act = ctrl.nativePause({ fullscreen: true }, 14_000);
  assert.equal(act.displayState, "PAUSED");
  assert.deepEqual(act.command, { name: "pause" });
});

test("a native pause right after a seek is browser noise, not the user", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(100), local(100), 13_000);
  ctrl.previewSeeking();
  ctrl.previewSeeked(local(120), { fullscreen: false }, 14_000);
  assert.deepEqual(ctrl.nativePause({ fullscreen: true }, 15_000), {});
});

test("programmatic seeks are never echoed back to the Chromecast", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(100), local(100), 13_000);

  ctrl.noteProgrammaticSeek(); // the applier is about to write currentTime
  assert.deepEqual(ctrl.previewSeeked(local(104), { fullscreen: true }, 14_000), {});

  // A genuine user seek via native fullscreen controls IS forwarded.
  const act = ctrl.previewSeeked(local(200), { fullscreen: true }, 17_000);
  assert.deepEqual(act.command, { name: "seek", body: { position: 200 } });
});

test("native play updates the display everywhere, commands only from fullscreen", () => {
  const ctrl = castCtrl(10_000);
  ctrl.handleStatus(activeStatus(100), local(100), 13_000);
  ctrl.userPlayPause(14_000); // now PAUSED

  const inline = ctrl.nativePlay({ fullscreen: false }, 20_000);
  assert.equal(inline.displayState, "PLAYING");
  assert.equal(inline.command, undefined);

  ctrl.userPlayPause(21_000); // back to PAUSED
  const fs = ctrl.nativePlay({ fullscreen: true }, 22_000);
  assert.deepEqual(fs.command, { name: "play" });
});

// ── restore on page load ───────────────────────────────────────────────────

test("restore adopts active playback with the fresh-cast grace", () => {
  const ctrl = createPreviewController();
  const act = ctrl.restore(
    activeStatus(42), { ...pausedLocal(0), readyState: 0 }, 10_000);
  assert.equal(act.setSrc, "http://cdn/v.mp4");
  assert.equal(act.seekWhenReady, 42, "resume the preview at the cast position");
  assert.equal(act.showSection, true);
  assert.equal(act.displayState, "PLAYING", "status is mirrored in the same shot");

  // The fresh-cast grace: a transient PAUSED while the preview loads and
  // seeks to the restored position must not be mirrored onto it.
  const echo = ctrl.handleStatus(
    activeStatus(42, { state: "PAUSED" }), local(42), 11_000);
  assert.ok(!echo.pause, "transient state right after restore is not mirrored");
});

test("restore with idle/disconnected playback is a no-op", () => {
  const ctrl = createPreviewController();
  assert.deepEqual(ctrl.restore({ connected: false, state: "IDLE" }, local(0), 10_000), {});
  assert.deepEqual(ctrl.restore(idleStatus(), local(0), 10_000), {});
});

// ── progress display ───────────────────────────────────────────────────────

test("progress is withheld while the preview element is mid-seek", () => {
  const ctrl = castCtrl(10_000);
  ctrl.previewSeeking();
  const during = ctrl.handleStatus(activeStatus(200), local(100, { seeking: true }), 13_000);
  assert.equal(during.progress, undefined, "don't fight the scrubbing user");

  ctrl.previewSeeked(local(200), { fullscreen: false }, 13_500);
  const after = ctrl.handleStatus(activeStatus(201), local(201), 14_000);
  assert.equal(after.progress.time, 201);
  assert.equal(after.progress.duration, 600);
});
