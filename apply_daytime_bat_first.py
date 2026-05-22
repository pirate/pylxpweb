"""Enable continuous daytime BAT_FIRST slots so the inverter charges the
battery from PV across the whole sunlight window — eliminates the
periodic export-to-grid spikes caused by gaps between sparse enabled
slots.

Diagnosis (2026-05-22): with FUNC_LSP_SELF_CONSUMPTION_EN = False, the
inverter only follows BAT_FIRST slots for PV→battery decisions. Outside
enabled slots it defaults to PV→grid. The previous config had isolated
slots 17 (08:00-08:30) and 19 (09:00-09:30) with gaps, so the inverter
oscillated between modes mid-morning. Slots 34/37/38 (afternoon/evening)
also conflicted with the forced-discharge window.

Changes:
  ENABLE  slots 18, 20–32   (covering 08:30-09:00 + 09:30-16:00)
  DISABLE slots 34, 37, 38  (overlap 16:00–21:00 forced-discharge window)

End state — BAT_FIRST slots enabled:
  3, 4, 5, 6        — overnight (legacy, unchanged)
  17, 18, …, 32     — continuous 08:00–16:00 daytime PV-charge
  (34, 37, 38 OFF)

After running this, run fix_battery_limits.py to confirm 0 drift and
snapshot_settings.py to refresh the JSON snapshot.

Usage: uv run python apply_daytime_bat_first.py
"""

import asyncio

from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

from _env import USERNAME, PASSWORD, BASE_URL


ENABLE_SLOTS  = [18] + list(range(20, 33))   # 18, 20..32 inclusive
DISABLE_SLOTS = [34, 37, 38]


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

        # Build list of (key, target) for all writes, in slot order
        changes = []
        for n in ENABLE_SLOTS:
            changes.append((f"FUNC_LSP_BAT_FIRST_{n}_EN", True))
        for n in DISABLE_SLOTS:
            changes.append((f"FUNC_LSP_BAT_FIRST_{n}_EN", False))

        keys = [k for k, _ in changes]
        before = await read_keys(client, serial, keys, fresh=True)
        print(f"BEFORE ({len(changes)} slots):")
        for k, target in changes:
            marker = "→" if str(before[k]) != str(target) else "·"
            print(f"  {marker} {k:<32} cur={before[k]}  target={target}")

        # Filter to only slots that actually need changing — minimize writes
        pending = [(k, t) for k, t in changes if str(before.get(k)) != str(t)]
        if not pending:
            print("\nNothing to do — already in target state.")
            return
        print(f"\nWriting {len(pending)} changes (with retry on REMOTE_SET_ERROR)...")
        for key, target in pending:
            for attempt in range(4):
                try:
                    result = await client.api.control.control_function(serial, key, bool(target))
                    ok = getattr(result, "success", None)
                    print(f"  {key:<32} -> {target}   "
                          f"{'OK' if ok else 'FAIL: ' + str(result)}")
                    break
                except Exception as e:
                    if "REMOTE_SET_ERROR" in str(e) and attempt < 3:
                        wait = 5 * (attempt + 1)
                        print(f"  {key:<32} -> retry in {wait}s ({e})")
                        await asyncio.sleep(wait)
                        continue
                    print(f"  {key:<32} -> FAILED: {e}")
                    break
            # Longer between-write delay to keep the inverter happy
            await asyncio.sleep(3)

        print("\nWaiting 5 s, then cache-bypass read...")
        await asyncio.sleep(5)
        after = await read_keys(client, serial, keys, fresh=True)

        print("\nAFTER:")
        all_ok = True
        for k, target in changes:
            cur = after[k]
            ok = str(cur) == str(target)
            mark = "✓" if ok else "✗ mismatch"
            print(f"  {k:<32} = {cur}   {mark}")
            if not ok:
                all_ok = False

        # Also check the 3 export-related flags didn't get side-effected
        guard_keys = [
            "FUNC_FEED_IN_GRID_EN",
            "FUNC_LSP_CHARGE_PRIORITY_EN",
            "HOLD_FEED_IN_GRID_POWER_PERCENT",
            "FUNC_LSP_SELF_CONSUMPTION_EN",
        ]
        guards = await read_keys(client, serial, guard_keys, fresh=True)
        print("\nGuard check (export flags should be unchanged):")
        for k in guard_keys:
            print(f"  {k:<36} = {guards[k]}")

        if all_ok:
            print("\nDone — continuous daytime BAT_FIRST schedule enabled.")
            print("PV should now charge the battery without oscillating to grid.")
        else:
            print("\nSome writes didn't stick — re-run or investigate.")


if __name__ == "__main__":
    asyncio.run(main())
