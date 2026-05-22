"""Re-enable FUNC_LSP_SELF_CONSUMPTION_EN (load-priority / self-consumption mode).

Was disabled previously to reduce CT-deadband chasing. We've since lowered
HOLD_EXPORT_LOCK_POWER to 10 W which addresses that noise directly, so we
can turn self-consumption back on. With it enabled (and the existing
FUNC_LSP_CHARGE_PRIORITY_EN = True) the inverter follows the normal
NEM 3.0–optimal cascade:

    PV → cover home load
       → charge battery (until HOLD_SYSTEM_CHARGE_SOC_LIMIT = 95%)
       → export excess to grid

Without it, excess PV goes straight to grid even when the battery isn't
full, leaving us with a partially-charged battery at sunset.

Usage: uv run python apply_self_consumption.py
"""

import asyncio

from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

from _env import USERNAME, PASSWORD, BASE_URL


CHANGES = [
    ("FUNC_LSP_SELF_CONSUMPTION_EN", "True"),
]


async def read_keys(client, serial, keys, *, fresh=False):
    if fresh:
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

        keys = [k for k, _ in CHANGES]
        before = await read_keys(client, serial, keys, fresh=True)
        print("BEFORE:")
        for k, _ in CHANGES:
            print(f"  {k:<36} = {before[k]}")

        print("\nWriting...")
        for key, target in CHANGES:
            # FUNC_ params use the /functionControl endpoint, not /write.
            # control_function takes a bool, not a string.
            enable = (str(target).lower() == "true")
            result = await client.api.control.control_function(serial, key, enable)
            ok = getattr(result, "success", None)
            print(f"  {key:<36} -> {target}   "
                  f"{'OK' if ok else 'FAIL: ' + str(result)}")

        print("\nWaiting 5 s, then cache-bypass read...")
        await asyncio.sleep(5)
        after = await read_keys(client, serial, keys, fresh=True)

        print("\nAFTER:")
        all_ok = True
        for k, target in CHANGES:
            cur = after[k]
            ok = str(cur) == str(target)
            mark = "✓" if ok else "✗ mismatch"
            print(f"  {k:<36} = {cur}   {mark}")
            if not ok:
                all_ok = False

        if all_ok:
            print("\nDone — self-consumption re-enabled.")
            print("PV should now charge the battery (up to 95%) before exporting.")
        else:
            print("\nWrite did not stick — check inverter logs.")


if __name__ == "__main__":
    asyncio.run(main())
