"""Bias the inverter's grid CT toward export so we never accidentally
leak ~30W of import. With HOLD_CT_POWER_OFFSET > 0 the inverter
interprets the CT reading as "more import than reality", so it pushes
slightly more output to compensate — net result is that real flow
sits slightly on the export side.

Starting value: 30 W. Combined with HOLD_EXPORT_LOCK_POWER = 10 W,
real grid flow should stay around 20-40 W export.

If sign is backwards (system biases toward MORE import) the user
should re-run with a negative value.

Usage: uv run python apply_ct_offset.py
"""

import asyncio

from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

from _env import USERNAME, PASSWORD, BASE_URL


CHANGES = [
    # Sign was guessed wrong initially: prior value was +40 and we were
    # still importing ~30W, so positive offset biases TOWARD import, not
    # export. Going -30 to bias the other direction.
    ("HOLD_CT_POWER_OFFSET", "-30"),
]


async def read(client, serial, keys):
    client.invalidate_cache_for_device(serial)
    params = await client.api.control.read_device_parameters_ranges(serial)
    return {k: params.get(k) for k in keys}


async def main():
    async with LuxpowerClient(
        username=USERNAME, password=PASSWORD, base_url=BASE_URL
    ) as client:
        stations = await Station.load_all(client)
        inverter = next(iter(stations[0].all_inverters))
        serial = inverter.serial_number
        print(f"Inverter: {inverter.model} ({serial})\n")

        keys = [k for k, _ in CHANGES] + ["HOLD_EXPORT_LOCK_POWER"]
        before = await read(client, serial, keys)
        print("BEFORE:")
        for k in keys:
            print(f"  {k:<32} = {before[k]}")

        print("\nWriting...")
        for key, target in CHANGES:
            result = await client.api.control.write_parameter(serial, key, target)
            ok = getattr(result, "success", None)
            print(f"  {key:<32} -> {target}   "
                  f"{'OK' if ok else 'FAIL: ' + str(result)}")

        await asyncio.sleep(5)
        after = await read(client, serial, keys)
        print("\nAFTER:")
        for k in keys:
            mark = "" if str(before[k]) == str(after[k]) else "  ← CHANGED"
            print(f"  {k:<32} = {after[k]}{mark}")

        print("\nWatch the live grid flow on /api/data over the next minute —")
        print("if it stabilizes near -30W (exporting) the bias is working.")
        print("If it goes to +30W (importing) the sign is flipped — re-run")
        print("with HOLD_CT_POWER_OFFSET = -30 instead.")


if __name__ == "__main__":
    asyncio.run(main())
