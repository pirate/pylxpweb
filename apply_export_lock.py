"""Lower HOLD_EXPORT_LOCK_POWER (grid CT deadband) from 25.5 W → 10 W.

Context: the inverter ignores grid imports/exports smaller than this many
watts (treats them as "balanced" and reports pToUser/pToGrid as 0). At
the EG4 default of 25.5 W, small 10–25 W phantom imports never show up
on the dashboard and never get logged as events.

Lowering to 10 W means:
  - The dashboard will start showing small negative grid flow when the
    house draws a few tens of watts the inverter can't immediately
    cover (e.g. brief load surges, low-PV mornings).
  - Events log will pick up imports it previously ignored.
  - Slightly more relay clicking as the inverter chases smaller imbalances.
  - We accept the noise tradeoff because we want visibility into all
    grid-pull behavior.

Idempotent — re-running just writes 10 again. Safe to run any time.

Usage: uv run python apply_export_lock.py
"""

import asyncio

from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

from _env import USERNAME, PASSWORD, BASE_URL


CHANGES = [
    ("HOLD_EXPORT_LOCK_POWER", "10"),
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
            print(f"  {k:<32} = {before[k]}")

        print("\nWriting...")
        for key, target in CHANGES:
            result = await client.api.control.write_parameter(serial, key, target)
            ok = getattr(result, "success", None)
            print(f"  {key:<32} -> {target}   "
                  f"{'OK' if ok else 'FAIL: ' + str(result)}")

        print("\nWaiting 5 s, then cache-bypass read...")
        await asyncio.sleep(5)
        after = await read_keys(client, serial, keys, fresh=True)

        print("\nAFTER:")
        all_ok = True
        for k, target in CHANGES:
            cur = after[k]
            # Inverter may return "10" or "10.0" — both are fine.
            ok = str(cur).split(".")[0] == str(target)
            mark = "✓" if ok else "✗ mismatch"
            print(f"  {k:<32} = {cur}   {mark}")
            if not ok:
                all_ok = False

        if all_ok:
            print("\nDone — CT deadband now 10 W (was 25.5 W).")
            print("Small grid flow that was previously invisible will now")
            print("show up on the dashboard and in the import-events log.")
        else:
            print("\nWrite did not stick — check inverter logs.")


if __name__ == "__main__":
    asyncio.run(main())
