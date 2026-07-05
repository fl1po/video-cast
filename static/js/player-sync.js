// player-sync.js — decision engine that reconciles Chromecast status (via SSE)
// with the local muted <video> preview. Pure logic, no DOM: the page feeds it
// server statuses + a snapshot of the video element and applies the returned
// actions. Loaded as a classic script in the browser (window.createPlayerSync)
// and via require() in node tests.
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createPlayerSync = factory().createPlayerSync;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var ACTION_GRACE_MS = 2000;
  var SEEK_GRACE_MS = 5000;
  var DRIFT_THRESHOLD_S = 3;
  // A fresh cast needs longer windows: the receiver may buffer for many
  // seconds before it starts playing and executes the deferred start-time seek.
  var CAST_SEEK_GRACE_MS = 15000;
  var CAST_STATE_GRACE_MS = 5000;

  function createPlayerSync() {
    // The user's most recent play/pause intent, held until the server confirms
    // it or the grace window expires (then the server wins again).
    var pendingState = null; // {state: "PLAYING"|"PAUSED", at: ms}
    // The user's most recent seek target, held the same way. The server has
    // "confirmed" once it reports a position near the target (allowing for
    // playback that advanced while the status was in flight).
    var pendingSeek = null; // {target: seconds, at: ms, graceMs: ms}
    var lastSnapAt = -Infinity; // rate-limits drift snaps
    var lastCastAt = -Infinity; // suppresses transient state mirroring after a cast
    var lastServer = null; // {time: seconds, playing: bool, at: ms} — last applied status

    function seekConfirmed(server, now) {
      // Compare against where playback SHOULD be if the seek executed: the
      // target plus whatever played since. A growing tolerance around the bare
      // target would eventually match a stale pre-seek status for small skips.
      var elapsed = (now - pendingSeek.at) / 1000;
      var expected = pendingSeek.target + (server.state === "PLAYING" ? elapsed : 0);
      return Math.abs(server.current_time - expected) <= DRIFT_THRESHOLD_S;
    }

    return {
      notePause(now) {
        pendingState = { state: "PAUSED", at: now };
      },
      notePlay(now) {
        pendingState = { state: "PLAYING", at: now };
      },
      noteSeek(target, now) {
        pendingSeek = { target: target, at: now, graceMs: SEEK_GRACE_MS };
      },
      noteCast(startTime, now) {
        lastCastAt = now;
        pendingState = null;
        pendingSeek = startTime > 0
          ? { target: startTime, at: now, graceMs: CAST_SEEK_GRACE_MS }
          : null;
      },

      // Best estimate of the true cast position right now: a pending seek
      // target beats the last server report; while playing, the report is
      // extrapolated with the wall clock (mirrors adjusted_current_time
      // server-side). Null until the first status arrives.
      position(now) {
        if (pendingSeek) return pendingSeek.target;
        if (!lastServer) return null;
        return lastServer.time + (lastServer.playing ? (now - lastServer.at) / 1000 : 0);
      },

      // server: {state, current_time, duration}
      // local:  {currentTime, paused, ended, seeking, readyState, error}
      // now:    ms timestamp (injected for testability)
      reconcile(server, local, now) {
        if (pendingState) {
          if (server.state === pendingState.state || now - pendingState.at > ACTION_GRACE_MS) {
            pendingState = null; // confirmed, or device ignored us — server wins
          }
        }
        if (pendingSeek) {
          if (seekConfirmed(server, now) || now - pendingSeek.at > pendingSeek.graceMs) {
            pendingSeek = null;
          }
        }
        if (!pendingSeek) {
          lastServer = { time: server.current_time, playing: server.state === "PLAYING", at: now };
        }
        var displayState = pendingState ? pendingState.state : server.state;
        // Drift correction: snap the preview when it disagrees with the
        // Chromecast by more than the threshold — never mid-seek, never while
        // a user seek is pending, and rate-limited so it can settle.
        var snapTo = null;
        var seekable = !local.seeking && local.readyState >= 1 && !local.error;
        if (
          !pendingSeek && seekable && server.duration > 0 &&
          now - lastSnapAt > SEEK_GRACE_MS &&
          Math.abs(local.currentTime - server.current_time) > DRIFT_THRESHOLD_S
        ) {
          snapTo = server.current_time;
          lastSnapAt = now;
        }
        // Mirror server play/pause onto the preview — but never against a
        // still-pending user action (that's the stale-echo flicker).
        var play = !pendingState && server.state === "PLAYING" && local.paused &&
          !local.ended && !local.seeking && !local.error;
        var pause = !pendingState && server.state === "PAUSED" && !local.paused &&
          now - lastCastAt > CAST_STATE_GRACE_MS;
        return {
          displayState: displayState,
          updateProgress: true,
          progressTime: pendingSeek ? pendingSeek.target : server.current_time,
          snapTo: snapTo,
          play: play,
          pause: pause,
        };
      },
    };
  }

  return { createPlayerSync };
});
