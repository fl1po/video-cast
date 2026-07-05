"""StateStore — the single state.json shared by the Cast Session and the
Device Registry. Each owner persists its own keys; the store merges them
into one file so the on-disk format stays what it has always been."""
import json
import threading


class StateStore:
    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        self._data = {}
        try:
            with open(path) as f:
                self._data = json.load(f)
        except Exception:
            pass

    def get(self, key, default=None):
        return self._data.get(key, default)

    def update(self, **values):
        with self._lock:
            self._data.update(values)
            try:
                with open(self._path, "w") as f:
                    json.dump(self._data, f)
            except Exception:
                pass
