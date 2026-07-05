"""Control endpoints must not broadcast pychromecast's *cached* (stale)
status — the broadcast should happen when the device confirms the change.
The stale echo is what made the browser preview flicker and the progress
bar bounce on every pause/seek."""


def test_play_broadcasts_only_after_device_confirms(device, sse, http_api):
    device.media_controller.status.player_state = "PAUSED"

    status, _ = http_api("/api/play")
    assert status == 200
    assert "play" in device.media_controller.commands

    sse.assert_quiet(1.0)

    device.media_controller.confirm(player_state="PLAYING")
    msg = sse.next(timeout=2)
    assert msg["state"] == "PLAYING"


def test_seek_broadcasts_only_after_device_confirms_new_position(device, sse, http_api):
    status, _ = http_api("/api/seek", {"position": 300})
    assert status == 200
    assert ("seek", 300.0) in device.media_controller.commands

    # No echo of the stale pre-seek position (this was the progress-bar bounce).
    sse.assert_quiet(1.0)

    device.media_controller.confirm(current_time=300.0)
    msg = sse.next(timeout=2)
    assert msg["current_time"] == 300.0


def test_volume_broadcasts_only_after_device_confirms(device, sse, http_api):
    status, _ = http_api("/api/volume", {"level": 0.8})
    assert status == 200
    assert ("set_volume", 0.8) in device.commands

    sse.assert_quiet(1.0)

    device.confirm_volume(0.8)
    msg = sse.next(timeout=2)
    assert msg["volume"] == 0.8


def test_media_commands_request_a_status_refresh_from_the_device(device, sse, http_api):
    # Some devices are slow to push MEDIA_STATUS on their own; actively asking
    # (GET_STATUS) makes the confirmed broadcast arrive promptly without
    # reintroducing the stale echo.
    http_api("/api/pause")
    http_api("/api/play")
    http_api("/api/seek", {"position": 42})
    assert device.media_controller.commands.count("update_status") == 3


def test_pause_broadcasts_only_after_device_confirms(device, sse, http_api):
    status, _ = http_api("/api/pause")
    assert status == 200
    assert "pause" in device.media_controller.commands

    # No echo of the stale PLAYING status...
    sse.assert_quiet(1.0)

    # ...but the confirmed state is broadcast promptly.
    device.media_controller.confirm(player_state="PAUSED")
    msg = sse.next(timeout=2)
    assert msg["state"] == "PAUSED"
