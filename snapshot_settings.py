"""Dump the current full inverter parameter set to inverter_settings_snapshot.json.

Read-only — never writes to the inverter. Run after any settings change so
the JSON snapshot matches reality and `fix_battery_limits.py` has an
accurate baseline to compare against.

Usage: uv run python snapshot_settings.py
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

from _env import USERNAME, PASSWORD, BASE_URL

OUT = Path(__file__).parent / "inverter_settings_snapshot.json"


def _jsonable(v):
    if isinstance(v, (bool, int, float, str)) or v is None:
        return v
    return str(v)


async def main():
    async with LuxpowerClient(
        username=USERNAME, password=PASSWORD, base_url=BASE_URL
    ) as client:
        stations = await Station.load_all(client)
        inverter = next(iter(stations[0].all_inverters))
        serial = inverter.serial_number

        client.invalidate_cache_for_device(serial)
        params = await client.api.control.read_device_parameters_ranges(serial)
        params_jsonable = {k: _jsonable(v) for k, v in params.items()}

        out = {
            "snapshot_time": datetime.now().isoformat(),
            "inverter_serial": serial,
            "inverter_model": inverter.model,
            "parameters": params_jsonable,
        }
        OUT.write_text(json.dumps(out, indent=2, sort_keys=True))
        print(f"Wrote {len(params_jsonable)} params to {OUT}")
        print(f"Snapshot time: {out['snapshot_time']}")


if __name__ == "__main__":
    asyncio.run(main())
