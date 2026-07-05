"""Device Registry — knows which Chromecasts exist: scan, the persisted
device cache, and friendly-name lookup. It never connects to anything;
connecting is the Cast Session's job."""


class DeviceRegistry:
    def __init__(self, platform, store):
        self._platform = platform
        self._store = store
        self._cached = store.get("cached_devices", [])
        self.last_uuid = store.get("last_device_uuid", "")

    def cached(self):
        return self._cached

    def name_for(self, uuid):
        for d in self._cached:
            if d.get("uuid") == uuid:
                return d.get("friendly_name")
        return None

    def select(self, uuid):
        """Remember the user's device choice."""
        self.last_uuid = uuid
        self._persist()

    def scan(self):
        """Discover devices on the network and replace the cache."""
        self._cached = self._platform.discover()
        self._persist()
        return self._cached

    def startup_reconnect(self):
        """Refresh availability on startup (does NOT connect — connecting an
        idle device on boot puts it in a bad state)."""
        if not self.last_uuid or not self._cached:
            return
        try:
            devices = self.scan()
            if not any(d.get("uuid") == self.last_uuid for d in devices):
                self.last_uuid = ""
                self._persist()
        except Exception:
            pass

    def _persist(self):
        self._store.update(cached_devices=self._cached, last_device_uuid=self.last_uuid)
