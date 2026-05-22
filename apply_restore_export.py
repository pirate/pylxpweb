"""Restore export-related settings that got side-effected when enabling
FUNC_LSP_SELF_CONSUMPTION_EN on 2026-05-22.

After flipping self-consumption to True, the inverter unexpectedly also
set:
    FUNC_LSP_CHARGE_PRIORITY_EN     True → False
    FUNC_FEED_IN_GRID_EN            True → False   (export disabled!)
    HOLD_FEED_IN_GRID_POWER_PERCENT    8 → 0       (export cap = 0)

The net effect: we can't export at all. This script restores all three
to the values they had before, leaving self-consumption ON.

Usage: uv run python apply_restore_export.py
"""

import asyncio

from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

from _env import USERNAME, PASSWORD, BASE_URL


# (key, target, is_function)
#   is_function=True  → use control_function (FUNC_ params, /functionControl)
#   is_function=False → use write_parameter   (HOLD_ params, /write)
CHANGES = [
    ("FUNC_FEED_IN_GRID_EN",            "True", True),   # restore export ability first
    ("HOLD_FEED_IN_GRID_POWER_PERCENT", "8",    False),  # restore 8 kW cap
    ("FUNC_LSP_CHARGE_PRIORITY_EN",     "True", True),   # restore PV→battery priority
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

        keys = [k for k, _, _ in CHANGES]
        before = await read_keys(client, serial, keys, fresh=True)
        print("BEFORE:")
        for k, _, _ in CHANGES:
            print(f"  {k:<40} = {before[k]}")

        print("\nWriting...")
        for key, target, is_fn in CHANGES:
            if is_fn:
                enable = (str(target).lower() == "true")
                result = await client.api.control.control_function(serial, key, enable)
            else:
                result = await client.api.control.write_parameter(serial, key, target)
            ok = getattr(result, "success", None)
            print(f"  {key:<40} -> {target}   "
                  f"{'OK' if ok else 'FAIL: ' + str(result)}")
            # Small delay between writes so the inverter doesn't reject the
            # next one as "busy".
            await asyncio.sleep(2)

        print("\nWaiting 5 s, then cache-bypass read...")
        await asyncio.sleep(5)
        after = await read_keys(client, serial, keys, fresh=True)

        print("\nAFTER:")
        all_ok = True
        for k, target, _ in CHANGES:
            cur = after[k]
            # Normalize for the comparison ("8" == "8.0")
            cur_n = str(cur).split(".")[0]
            ok = cur_n == str(target).split(".")[0]
            mark = "✓" if ok else "✗ mismatch"
            print(f"  {k:<40} = {cur}   {mark}")
            if not ok:
                all_ok = False

        if all_ok:
            print("\nDone — export settings restored.")
        else:
            print("\nOne or more writes didn't stick — check inverter logs.")


if __name__ == "__main__":
    asyncio.run(main())
