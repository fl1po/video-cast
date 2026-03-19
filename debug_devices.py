import pychromecast
import time
import json

print("Discovering Chromecast devices...")
services, browser = pychromecast.discovery.discover_chromecasts()
time.sleep(5)
services, browser = pychromecast.discovery.discover_chromecasts()

print(f"\nFound {len(services)} device(s):")
for s in services:
    print(f"  - {s.friendly_name} | uuid={s.uuid} | host={s.host}")

pychromecast.discovery.stop_discovery(browser)

# Check state.json
try:
    with open("state.json") as f:
        state = json.load(f)
    print(f"\nstate.json:")
    print(f"  last_device_uuid: {state.get('last_device_uuid')}")
    print(f"  cached_devices: {json.dumps(state.get('cached_devices', []), indent=4)}")

    last_uuid = state.get("last_device_uuid", "")
    found_uuids = [str(s.uuid) for s in services]
    if last_uuid:
        if last_uuid in found_uuids:
            print(f"\n  -> Last device IS on the network")
        else:
            print(f"\n  -> Last device NOT found! UUID {last_uuid} not in {found_uuids}")
    else:
        print(f"\n  -> No last device saved")
except Exception as e:
    print(f"\nCould not read state.json: {e}")
