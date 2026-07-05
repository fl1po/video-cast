import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { createPlayerSync } = require("../static/js/player-sync.js");

// Helpers — a server status and local <video> snapshot in perfect sync.
const playingServer = (t = 100) => ({ state: "PLAYING", current_time: t, duration: 600 });
const playingLocal = (t = 100) => ({
  currentTime: t, paused: false, ended: false, seeking: false, readyState: 4, error: null,
});

test("after user pauses, a stale PLAYING status does not un-pause the preview", () => {
  const sync = createPlayerSync();
  sync.notePause(10_000);
  // Preview already paused optimistically; server echo still says PLAYING.
  const r = sync.reconcile(playingServer(100), { ...playingLocal(100), paused: true }, 10_400);
  assert.equal(r.play, false, "must not mirror the stale PLAYING state");
  assert.equal(r.displayState, "PAUSED", "UI keeps showing the user's intent");
});

test("no pending action: preview mirrors server play/pause state", () => {
  const sync = createPlayerSync();
  const r1 = sync.reconcile(playingServer(100), { ...playingLocal(100), paused: true }, 10_000);
  assert.equal(r1.play, true, "server PLAYING + preview paused -> play preview");
  assert.equal(r1.pause, false);

  const r2 = sync.reconcile(
    { state: "PAUSED", current_time: 100, duration: 600 },
    playingLocal(100),
    12_000,
  );
  assert.equal(r2.pause, true, "server PAUSED + preview playing -> pause preview");
  assert.equal(r2.play, false);
});

test("pause grace expiry: if the device never pauses, the server wins again", () => {
  const sync = createPlayerSync();
  sync.notePause(10_000);
  const r = sync.reconcile(playingServer(120), { ...playingLocal(120), paused: true }, 13_000);
  assert.equal(r.displayState, "PLAYING");
  assert.equal(r.play, true, "mirroring resumes after the grace window");
});

test("after user plays, a stale PAUSED status does not pause the preview", () => {
  const sync = createPlayerSync();
  sync.notePlay(10_000);
  const r = sync.reconcile(
    { state: "PAUSED", current_time: 100, duration: 600 },
    playingLocal(100),
    10_400,
  );
  assert.equal(r.pause, false, "must not mirror the stale PAUSED state");
  assert.equal(r.displayState, "PLAYING");
});

test("server confirmation clears the pending intent; later genuine changes mirror again", () => {
  const sync = createPlayerSync();
  sync.notePause(10_000);
  // Device confirms the pause.
  const paused = { state: "PAUSED", current_time: 100, duration: 600 };
  sync.reconcile(paused, { ...playingLocal(100), paused: true }, 10_500);
  // Someone else resumes from another client — preview must follow immediately.
  const r = sync.reconcile(playingServer(100), { ...playingLocal(100), paused: true }, 11_000);
  assert.equal(r.play, true);
  assert.equal(r.displayState, "PLAYING");
});

test("after a user seek, stale server positions do not bounce the progress display", () => {
  const sync = createPlayerSync();
  sync.noteSeek(300, 10_000);
  // Stale status still reports the pre-seek position.
  const r = sync.reconcile(playingServer(100), playingLocal(300), 10_400);
  assert.equal(r.progressTime, 300, "display holds the seek target, not the stale position");
  assert.equal(r.snapTo, null, "preview must not be yanked back");
});

test("seek is confirmed once the server reports a position near the target", () => {
  const sync = createPlayerSync();
  sync.noteSeek(300, 10_000);
  sync.reconcile(playingServer(301), playingLocal(301), 11_000); // confirmation
  // Later statuses are applied normally again.
  const r = sync.reconcile(playingServer(310), playingLocal(310), 20_000);
  assert.equal(r.progressTime, 310);
});

test("a small forward skip is not falsely confirmed by a stale pre-seek status", () => {
  const sync = createPlayerSync();
  // Playing at ~105, user presses +5 -> target 110.
  sync.noteSeek(110, 10_000);
  // Periodic tick 1.5s later still extrapolates the PRE-seek timeline: ~106.5.
  const r = sync.reconcile(playingServer(106.5), playingLocal(110), 11_500);
  assert.equal(r.progressTime, 110, "stale position must not be treated as confirmation");
  // The device then actually executes the seek: ~110.5 at +2s.
  sync.reconcile(playingServer(110.5), playingLocal(110.5), 12_000);
  const after = sync.reconcile(playingServer(112), playingLocal(112), 13_500);
  assert.equal(after.progressTime, 112, "real confirmation resumes normal updates");
});

test("seek grace expiry: if the device never seeks, server position wins again", () => {
  const sync = createPlayerSync();
  sync.noteSeek(300, 10_000);
  const r = sync.reconcile(playingServer(105), playingLocal(300), 16_000);
  assert.equal(r.progressTime, 105);
});

test("drift beyond 3s snaps the preview to the server position", () => {
  const sync = createPlayerSync();
  const r = sync.reconcile(playingServer(100), playingLocal(90), 10_000);
  assert.equal(r.snapTo, 100);
});

test("drift snaps are rate-limited: no second snap right after one", () => {
  const sync = createPlayerSync();
  sync.reconcile(playingServer(100), playingLocal(90), 10_000); // snaps
  const r = sync.reconcile(playingServer(102), playingLocal(90), 12_000);
  assert.equal(r.snapTo, null, "preview needs time to settle after a snap");
});

test("no snap while the preview is not seekable (seeking / no metadata / error)", () => {
  const sync = createPlayerSync();
  const drifted = playingLocal(90);
  assert.equal(sync.reconcile(playingServer(100), { ...drifted, seeking: true }, 10_000).snapTo, null);
  assert.equal(sync.reconcile(playingServer(100), { ...drifted, readyState: 0 }, 30_000).snapTo, null);
  assert.equal(sync.reconcile(playingServer(100), { ...drifted, error: {} }, 50_000).snapTo, null);
});

test("cast with a start time: server position ~0 does not yank the preview back, even past the normal seek grace", () => {
  const sync = createPlayerSync();
  sync.noteCast(120, 10_000);
  // 6s in (device buffered slowly, deferred seek not executed yet), server still ~0.
  const r = sync.reconcile(playingServer(2), playingLocal(120), 16_000);
  assert.equal(r.snapTo, null);
  assert.equal(r.progressTime, 120);
});

test("cast start-time seek is confirmed once the device reports it", () => {
  const sync = createPlayerSync();
  sync.noteCast(120, 10_000);
  sync.reconcile(playingServer(121), playingLocal(121), 13_000); // confirmation
  const r = sync.reconcile(playingServer(130), playingLocal(130), 23_000);
  assert.equal(r.progressTime, 130);
});

test("transient PAUSED right after casting does not pause the preview", () => {
  const sync = createPlayerSync();
  sync.noteCast(0, 10_000);
  const r = sync.reconcile(
    { state: "PAUSED", current_time: 0, duration: 600 },
    playingLocal(0),
    12_000,
  );
  assert.equal(r.pause, false);
});

test("position(): extrapolates from the last server status while playing, frozen while paused", () => {
  const sync = createPlayerSync();
  assert.equal(sync.position(10_000), null, "unknown before any status");

  sync.reconcile(playingServer(100), playingLocal(100), 10_000);
  assert.equal(sync.position(12_000), 102, "playing -> advances with wall clock");

  sync.reconcile({ state: "PAUSED", current_time: 200, duration: 600 }, { ...playingLocal(200), paused: true }, 20_000);
  assert.equal(sync.position(25_000), 200, "paused -> does not advance");
});

test("position(): a pending seek target is the base, so rapid skips accumulate", () => {
  const sync = createPlayerSync();
  sync.reconcile(playingServer(100), playingLocal(97), 10_000);
  sync.noteSeek(110, 10_100); // user pressed +10 (from 100, not the drifted 97)
  assert.equal(sync.position(10_200), 110, "next +10 starts from 110, not a stale position");
});

test("never plays an ended or errored preview (play() would rewind / spin forever)", () => {
  const sync = createPlayerSync();
  const pausedLocal = { ...playingLocal(100), paused: true };
  assert.equal(sync.reconcile(playingServer(100), { ...pausedLocal, ended: true }, 10_000).play, false);
  assert.equal(sync.reconcile(playingServer(100), { ...pausedLocal, error: {} }, 20_000).play, false);
  assert.equal(sync.reconcile(playingServer(100), { ...pausedLocal, seeking: true }, 30_000).play, false);
});

test("transient IDLE right after casting must not tear down the preview (black-until-refresh bug)", () => {
  const sync = createPlayerSync();
  sync.noteCast(0, 10_000);
  // Receiver is still loading: /api/cast already returned, but the device
  // keeps reporting IDLE for a few seconds.
  assert.equal(sync.shouldClearOnIdle(11_500), false);
});

test("IDLE after the cast has been seen active is a real stop", () => {
  const sync = createPlayerSync();
  sync.noteCast(0, 10_000);
  sync.reconcile(playingServer(1), playingLocal(1), 13_000); // receiver started
  assert.equal(sync.shouldClearOnIdle(13_500), true);
});

test("IDLE with no recent cast is a real stop (cast stopped from another device)", () => {
  const sync = createPlayerSync();
  assert.equal(sync.shouldClearOnIdle(10_000), true);
});

test("cast that never starts: IDLE tears down once the grace expires", () => {
  const sync = createPlayerSync();
  sync.noteCast(0, 10_000);
  assert.equal(sync.shouldClearOnIdle(26_000), true);
});

test("a new cast re-arms the IDLE grace even after a previous cast was active", () => {
  const sync = createPlayerSync();
  sync.noteCast(0, 10_000);
  sync.reconcile(playingServer(5), playingLocal(5), 12_000);
  sync.noteCast(0, 50_000); // user casts the next video
  assert.equal(sync.shouldClearOnIdle(51_000), false, "new receiver load window");
});

test("steady-state playback: server progress is applied, no corrective actions", () => {
  const sync = createPlayerSync();
  const r = sync.reconcile(playingServer(100), playingLocal(100), 10_000);
  assert.equal(r.updateProgress, true);
  assert.equal(r.progressTime, 100);
  assert.equal(r.displayState, "PLAYING");
  assert.equal(r.snapTo, null);
  assert.equal(r.play, false);
  assert.equal(r.pause, false);
});
