"""Enable FUNC_LSP_SELF_CONSUMPTION_EN and surface what the inverter
side-effects in response. The hypothesis (from a prior attempt): the
inverter treats FEED_IN_GRID_EN + SELF_CONSUMPTION_EN as mutually
exclusive export-control modes — turning self-consumption on auto-
disables the manual feed-in path. That's actually fine if behavior
is what we want (continuous PV→grid when battery is full).

Workflow:
  1. snapshot current state (4 export-related flags)
  2. enable SELF_CONSUMPTION_EN
  3. wait 5s, re-read
  4. print before/after diff
  5. user can manually verify behavior changes (PV actually exports)
     and decide whether to roll back via apply_restore_export.py

Usage: uv run python apply_enable_selfcons.py
"""

import asyncio

from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

from _env import USERNAME, PASSWORD, BASE_URL


WATCH_KEYS = [
    "FUNC_LSP_SELF_CONSUMPTION_EN",
    "FUNC_LSP_CHARGE_PRIORITY_EN",
    "FUNC_FEED_IN_GRID_EN",
    "HOLD_FEED_IN_GRID_POWER_PERCENT",
    "FUNC_FORCED_DISCHG_EN",
    "HOLD_SYSTEM_CHARGE_SOC_LIMIT",
]


async def read(client, serial, keys, *, fresh=True):
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

        before = await read(client, serial, WATCH_KEYS)
        print("BEFORE:")
        for k in WATCH_KEYS:
            print(f"  {k:<40} = {before[k]}")

        print("\nWriting FUNC_LSP_SELF_CONSUMPTION_EN = True ...")
        result = await client.api.control.control_function(
            serial, "FUNC_LSP_SELF_CONSUMPTION_EN", True
        )
        print(f"  result: {'OK' if getattr(result, 'success', False) else 'FAIL: ' + str(result)}")

        print("\nWaiting 5 s, then re-read...")
        await asyncio.sleep(5)
        after = await read(client, serial, WATCH_KEYS)

        print("\nAFTER (changed values flagged):")
        for k in WATCH_KEYS:
            b, a = before[k], after[k]
            mark = "  ← CHANGED" if str(b) != str(a) else ""
            print(f"  {k:<40} = {a}{mark}")

        sce = str(after.get("FUNC_LSP_SELF_CONSUMPTION_EN")) == "True"
        print(f"\nSelf-consumption mode is now: {'ENABLED ✓' if sce else 'still False ✗'}")
        if not sce:
            print("The inverter rejected the change. Stopping.")
            return

        # If FEED_IN was side-effected off, the inverter may now manage
        # feed-in internally via self-consumption logic. Watch the live
        # behavior for ~30s before deciding to restore anything.
        print("\nMonitoring live behavior for 30s — watch /api/data for")
        print("battery_charge_power, power_to_grid, pv_total_power.")
        print()
        print("Roll back via:  uv run python apply_restore_export.py")


if __name__ == "__main__":
    asyncio.run(main())
