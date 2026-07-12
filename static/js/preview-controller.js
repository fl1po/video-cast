// preview-controller.js — the Preview Controller: a pure state machine that
// owns every preview decision — lifecycle (load / swap / teardown), the
// autoplay-blocked fallback, native-event forwarding, user intents, and the
// displayed playback state. The page is a dumb adapter: it reads <video>
// snapshots, feeds events in, and applies the returned actions (DOM writes
// and backend commands alike).
//
// The sync engine (player-sync.js) is an internal seam: reconciling server
// status against pending user intents lives there; the page never calls it.
//
// Actions vocabulary (any subset per call; apply in this order):
//   title          set the "Now playing" title
//   showSection    reveal the player section       hideSection  hide + reset title
//   setSrc/poster  load a preview URL (muted)      clearSrc     unload the preview
//   playWhenReady  tryPlay once the src can play   seekWhenReady seek once metadata loads
//   seekTo         programmatic seek (call noteProgrammaticSeek() before writing)
//   play           tryPlay now                     pause        pause the element
//   chip           show/hide the "Smooth preview" chip
//   displayState   playback state to display       statusText   status bar override
//   showControls   poke the controls auto-hide     progress     {time, duration}
//   volume         {level, muted} (page may ignore while the user is dragging)
//   command        {name, body} -> POST /api/<name>
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = factory(require("./player-sync.js").createPlayerSync);
  } else {
    root.createPreviewController = factory(root.createPlayerSync).createPreviewController;
  }
})(typeof self !== "undefined" ? self : this, function (createPlayerSync) {
  "use strict";

  // Resume-from-live threshold: below this the head start isn't worth a seek
  // (and is too small for the sync engine's drift snap to ever correct).
  var UNLOCK_DRIFT_S = 0.5;
  // A native pause event right after a seek is browser noise, not the user.
  var SEEK_PAUSE_SUPPRESS_MS = 2000;

  function createPreviewController(sync) {
    sync = sync || createPlayerSync();

    var state = "IDLE";      // displayed playback state — the single source of truth
    var streamUrl = "";      // preview URL currently loaded ("" when none)
    var localActive = false; // user cast this session — only then drive the <video>
                             // (a refreshed page must not compete for the CDN session)
    var casting = false;     // cast API call in flight — IDLE must not tear down
    var duration = 0;        // last known duration, for clamping and progress
    var seeking = false;     // preview element is mid-seek
    var lastSeekAt = -Infinity;
    var expectProgrammaticSeeked = false; // next seeked event is ours, not the user's
    // A fresh cast from position 0: the preview's own buffer often fills
    // faster than the Chromecast connects and starts, so autoplaying on
    // canplay gives it a head start too small for the drift snap to ever
    // catch. Held until the device's first confirmed PLAYING status, so the
    // preview's first frame starts from the real position instead.
    var pendingFreshPlay = false;

    function seekActions(pos, local, now) {
      sync.noteSeek(pos, now);
      var act = {
        progress: { time: pos, duration: duration },
        command: { name: "seek", body: { position: pos } },
      };
      if (!local.error) act.seekTo = pos;
      return act;
    }

    return {
      // ── status stream ─────────────────────────────────────────────────

      // server: full SSE status; local: <video> snapshot {currentTime,
      // paused, ended, seeking, readyState, error}; now: ms timestamp.
      handleStatus: function (server, local, now) {
        var act = {};
        if (server.connected && server.state !== "IDLE" && server.state !== "UNKNOWN") {
          var r = sync.reconcile(server, local, now);
          var prev = state;
          state = r.displayState;
          act.displayState = state;
          // Controls appear on the transition into PLAYING (SSE can beat the
          // element's own play event).
          if (state === "PLAYING" && prev !== "PLAYING") act.showControls = true;
          if (!seeking) {
            duration = server.duration || 0;
            act.progress = { time: r.progressTime, duration: duration };
          }
          act.volume = { level: server.volume, muted: server.muted };
          if (server.stream_url) {
            act.title = server.title || "";
            act.showSection = true;
          }
          // Load/swap the preview only when the user cast this session.
          if (localActive && server.stream_url && server.stream_url !== streamUrl) {
            streamUrl = server.stream_url;
            act.setSrc = server.stream_url;
            act.poster = server.thumbnail || "";
            if (server.state === "PLAYING") act.play = true;
          }
          // The engine's corrective actions (drift snap, mirror play/pause).
          if (localActive && streamUrl && !local.error) {
            if (pendingFreshPlay) {
              // Still BUFFERING (or similar) — keep withholding play until
              // the device is actually PLAYING, not just connected.
              if (state === "PLAYING") {
                pendingFreshPlay = false;
                act.seekTo = r.progressTime;
                act.play = true;
              }
            } else {
              if (r.snapTo !== null) act.seekTo = r.snapTo;
              if (r.play) act.play = true;
              else if (r.pause) act.pause = true;
            }
          }
        } else {
          state = server.state;
          act.displayState = state;
          act.statusText = server.connected ? "Idle" : "Not connected";
          // Cast stopped or ended (possibly from another device) — tear the
          // preview down on every open window, including ones that never
          // loaded a stream. The engine ignores the receiver's transient
          // IDLE while a fresh cast is still loading (the black-until-
          // refresh bug); the casting flag covers the API call itself.
          if ((server.state === "IDLE" || !server.connected) && !casting &&
              sync.shouldClearOnIdle(now)) {
            localActive = false;
            pendingFreshPlay = false;
            act.chip = false;
            if (streamUrl) {
              streamUrl = "";
              act.clearSrc = true;
            }
            act.hideSection = true;
          }
        }
        return act;
      },

      // Initial /api/state payload on page load: adopt active playback
      // (wire the preview, restore the position) and mirror the status.
      restore: function (playback, local, now) {
        if (!(playback && playback.connected &&
              playback.state !== "IDLE" && playback.state !== "UNKNOWN")) {
          return {};
        }
        var act = {};
        if (playback.stream_url) {
          localActive = true;
          // Same grace as a fresh cast: don't mirror transient states while
          // the preview loads and seeks to the restored position.
          sync.noteCast(0, now);
          streamUrl = playback.stream_url;
          act.setSrc = playback.stream_url;
          act.poster = playback.thumbnail || "";
          act.title = playback.title || "";
          act.showSection = true;
          act.showControls = true;
          if (playback.current_time > 0) act.seekWhenReady = playback.current_time;
        }
        var statusAct = this.handleStatus(playback, local, now);
        for (var k in statusAct) act[k] = statusAct[k];
        return act;
      },

      // ── cast lifecycle ────────────────────────────────────────────────

      castRequested: function (now) {
        casting = true;
        return { showSection: true };
      },

      // result: the /api/cast or /api/cast_file response.
      castStarted: function (result, now) {
        casting = false;
        // Note the cast even without a preview URL (m3u8): the post-cast
        // IDLE grace must apply to every fresh cast.
        sync.noteCast(result.start_time || 0, now);
        var act = {};
        if (result.stream_url) {
          localActive = true;
          streamUrl = result.stream_url;
          act.setSrc = result.stream_url;
          act.poster = result.thumbnail || "";
          act.title = result.title || "";
          act.showSection = true;
          act.showControls = true;
          if (result.start_time > 0) {
            act.playWhenReady = true;
            act.seekTo = result.start_time;
          } else {
            // Load the preview but don't autoplay yet — see pendingFreshPlay.
            pendingFreshPlay = true;
          }
        }
        return act;
      },

      castFailed: function (now) {
        casting = false;
        pendingFreshPlay = false;
        return { hideSection: !streamUrl };
      },

      // ── user intents ──────────────────────────────────────────────────

      userPlayPause: function (now) {
        if (state === "PLAYING") {
          sync.notePause(now);
          state = "PAUSED";
          return { displayState: "PAUSED", showControls: true, pause: true,
                   command: { name: "pause" } };
        }
        sync.notePlay(now);
        state = "PLAYING";
        return { displayState: "PLAYING", showControls: true, play: true,
                 command: { name: "play" } };
      },

      userSeekTo: function (pos, local, now) {
        return seekActions(pos, local, now);
      },

      userSeekBy: function (delta, local, now) {
        // Base on the cast's clock, not the preview's — the preview may
        // drift within the snap threshold.
        var cur = sync.position(now);
        if (cur === null || cur === undefined) cur = local.currentTime || 0;
        var pos = Math.max(0, Math.min(duration, cur + delta));
        return seekActions(pos, local, now);
      },

      userStop: function (now) {
        localActive = false;
        pendingFreshPlay = false;
        streamUrl = "";
        state = "IDLE";
        return {
          displayState: "IDLE", chip: false, clearSrc: true,
          hideSection: true, command: { name: "stop" },
        };
      },

      // ── autoplay-blocked fallback ─────────────────────────────────────

      // play() resolved. Playback begins where it was queued, not where the
      // cast is NOW — without the correction the head start becomes
      // permanent lag (too small for the drift snap).
      playStarted: function (local, now) {
        var act = { chip: false };
        var pos = sync.position(now);
        if (pos !== null && !local.seeking &&
            Math.abs(local.currentTime - pos) > UNLOCK_DRIFT_S) {
          act.seekTo = pos;
        }
        return act;
      },

      // play() was refused by the autoplay policy.
      playRefused: function (now) {
        return { chip: true };
      },

      // The element reports actual playback (any path).
      playing: function (now) {
        return { chip: false };
      },

      // A real user gesture anywhere on the page: the one reliable unlock
      // (Firefox grants activation per-gesture, so play() must be called
      // inside the gesture handler — apply the returned actions in it).
      gesture: function (local, now) {
        if (!localActive || !streamUrl || local.error) return {};
        if ((state === "PLAYING" || state === "BUFFERING") &&
            local.paused && !local.ended) {
          var act = { play: true };
          // Start buffering from the live cast position, not the last
          // stepped frame; playStarted corrects again once playback begins.
          var pos = sync.position(now);
          if (pos !== null && local.readyState >= 1 &&
              Math.abs(local.currentTime - pos) > UNLOCK_DRIFT_S) {
            act.seekTo = pos;
          }
          return act;
        }
        return {};
      },

      // Periodic tick while play() is refused: keep the paused preview
      // tracking the cast clock (~1fps slideshow — seeking a paused video
      // is allowed even when playback isn't).
      tick: function (local, now) {
        if (!localActive || !streamUrl || local.error) return {};
        if (!local.paused || local.seeking || local.ended || local.readyState < 1) return {};
        if (state !== "PLAYING") return {};
        var pos = sync.position(now);
        if (pos === null) return {};
        return { seekTo: pos };
      },

      // Tab woke up (sleeping tab / OS suspend may have paused the preview).
      visibilityRestored: function (local, now) {
        if (localActive && streamUrl && !local.error && state === "PLAYING" &&
            local.paused && !local.ended) {
          return { play: true };
        }
        return {};
      },

      // ── native <video> events (iOS fullscreen controls) ───────────────

      previewSeeking: function () {
        seeking = true;
      },

      previewSeeked: function (local, ctx, now) {
        seeking = false;
        lastSeekAt = now;
        if (expectProgrammaticSeeked) {
          expectProgrammaticSeeked = false; // our own seek — already sent
          return {};
        }
        if (!streamUrl) return {};
        // Only forward seeks made via native fullscreen controls (iOS).
        // Browser-internal seeks (e.g. rewind-to-0 when play() is called on
        // an ended element) must never reach the Chromecast.
        if (!ctx.fullscreen) return {};
        var pos = local.currentTime;
        sync.noteSeek(pos, now);
        return {
          progress: { time: pos, duration: duration },
          command: { name: "seek", body: { position: pos } },
        };
      },

      nativePause: function (ctx, now) {
        if (!ctx.fullscreen) return {};
        if (!streamUrl || seeking || expectProgrammaticSeeked) return {};
        if (now - lastSeekAt < SEEK_PAUSE_SUPPRESS_MS) return {}; // pause fired after seek
        if (state === "PAUSED" || state === "IDLE") return {};
        sync.notePause(now);
        state = "PAUSED";
        return { displayState: "PAUSED", showControls: true, command: { name: "pause" } };
      },

      nativePlay: function (ctx, now) {
        if (!streamUrl || seeking || expectProgrammaticSeeked) return {};
        if (state === "PLAYING") return {};
        state = "PLAYING";
        var act = { displayState: "PLAYING", showControls: true };
        // Only forward to the Chromecast from fullscreen (iOS native
        // controls); outside it, control goes through our own buttons.
        if (ctx.fullscreen) {
          sync.notePlay(now);
          act.command = { name: "play" };
        }
        return act;
      },

      // ── applier hook + read-only helpers for page chrome ──────────────

      // The applier calls this immediately before every programmatic
      // currentTime write, so the flag tracks actual writes exactly.
      noteProgrammaticSeek: function () {
        expectProgrammaticSeeked = true;
      },

      position: function (now) {
        return sync.position(now);
      },
      duration: function () {
        return duration;
      },
      displayState: function () {
        return state;
      },
      hasStream: function () {
        return !!streamUrl;
      },
    };
  }

  return { createPreviewController };
});
