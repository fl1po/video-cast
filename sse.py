"""Status Broadcaster — fans the Cast Session's status out to SSE
subscribers. Wakes on device events (wake()) or periodically for time
updates: faster while playing, slower when idle."""
import json
import queue
import threading


class StatusBroadcaster:
    def __init__(self, get_status, poll_playing=2.0, poll_idle=5.0):
        self._get_status = get_status
        self._poll_playing = poll_playing
        self._poll_idle = poll_idle
        self._subscribers = []
        self._lock = threading.Lock()
        self._changed = threading.Event()
        self._last_state = "IDLE"

    def subscribe(self):
        q = queue.Queue(maxsize=5)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def wake(self):
        self._changed.set()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            timeout = self._poll_playing if self._last_state == "PLAYING" else self._poll_idle
            self._changed.wait(timeout=timeout)
            self._changed.clear()
            self.broadcast()

    def broadcast(self):
        data = self._get_status()
        self._last_state = data.get("state", "IDLE")
        msg = f"data: {json.dumps(data)}\n\n"
        with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    # Drop old messages if the client is slow
                    while not q.empty():
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            break
                    q.put_nowait(msg)
                except Exception:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)
