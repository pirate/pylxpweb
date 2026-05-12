"""Mode-flag preserver (no-op against the current known-good config).

Originally written to disable charge-priority and enable self-consumption +
grid-CT. After further investigation, the *actual* lever that was blocking
battery exports turned out to be the EG4 'Export PV Only' setting
(`FUNC_PV_SELL_TO_GRID_EN`), not these flags. Charge-priority kept reverting
to True regardless and the inverter operates fine that way once Export-PV-Only
is False.

This script's CHANGES now exactly match the current live state, so re-running
it is a verification pass — no functional writes.
"""

import asyncio
from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

from _env import USERNAME, PASSWORD, BASE_URL

# Targets match current live (known-good) state. If you ever change one of
# these via the EG4 LCD/web UI, mirror the change here so this script remains
# a no-op against the desired state.
CHANGES = [
    ("FUNC_LSP_CHARGE_PRIORITY_EN", True),
    ("FUNC_LSP_SELF_CONSUMPTION_EN", True),
    ("FUNC_GRID_CT_CONNECTION_EN", True),
]


def fmt(v):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "ENABLED" if v else "disabled"
    s = str(v)
    if s.lower() in ("true", "1"):
        return "ENABLED"
    if s.lower() in ("false", "0"):
        return "disabled"
    return s


async def read_keys(client, serial, keys, fresh=False):
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
        before = await read_keys(client, serial, keys)
        print("BEFORE:")
        for k, _ in CHANGES:
            print(f"  {k:<32} {fmt(before[k])}")

        print("\nApplying changes...")
        for key, target in CHANGES:
            result = await client.api.control.control_function(serial, key, target)
            ok = getattr(result, "success", None)
            print(f"  {key:<32} -> {fmt(target):<8} "
                  f"{'OK' if ok else 'FAIL: ' + str(result)}")

        print("\nWaiting 5 s for inverter to settle, then bypassing cache...")
        await asyncio.sleep(5)

        after = await read_keys(client, serial, keys, fresh=True)
        print("\nAFTER:")
        for k, target in CHANGES:
            cur = after[k]
            cur_b = cur if isinstance(cur, bool) else str(cur).lower() in ("true", "1")
            ok = "✓" if cur_b == target else "✗ mismatch"
            print(f"  {k:<32} {fmt(cur):<8}  {ok}")


if __name__ == "__main__":
    asyncio.run(main())
