"""The app routinely outlives its console: launched hidden (start.vbs), over
ssh, or from a terminal that later goes away, it keeps serving while its
stdout/stderr are dead — writes then raise OSError "[WinError 233] No
process is on the other end of the pipe".

yt-dlp reports extraction errors by WRITING to stderr before raising
DownloadError (YoutubeDL.trouble), even with quiet/no_warnings set. On a
dead stream that write explodes first, so the OSError replaces the real
error: the UI toast shows the pipe message and auto-resume gives up
silently. Routing yt-dlp output through a logging.Logger (the "logger"
opt) is dead-stdio-safe — logging swallows stream errors by design."""
import sys

import pytest
import yt_dlp

import app as videocast


class DeadStdio:
    """A stream whose console host is gone: every write raises WinError 233."""
    encoding = "utf-8"

    def write(self, s):
        raise OSError(22, "No process is on the other end of the pipe", None, 233)

    def flush(self):
        raise OSError(22, "No process is on the other end of the pipe", None, 233)

    def isatty(self):
        return False


def test_resolver_failure_surfaces_real_error_despite_dead_stdio(monkeypatch):
    # cookiesfrombrowser needs a Firefox profile on the test machine —
    # machine-dependent and irrelevant to the error-masking pattern under test.
    opts = {k: v for k, v in videocast.YT_DLP_OPTS.items() if k != "cookiesfrombrowser"}
    monkeypatch.setattr(videocast, "YT_DLP_OPTS", opts)
    monkeypatch.setattr(sys, "stdout", DeadStdio())
    monkeypatch.setattr(sys, "stderr", DeadStdio())

    # .invalid never resolves (RFC 6761): extraction fails fast with or
    # without a network, and the resolver must report THAT failure — not the
    # stdio corpse yt-dlp tried to print it to.
    with pytest.raises(yt_dlp.utils.DownloadError):
        videocast.resolve_with_ytdlp("https://nonexistent.invalid/video")
